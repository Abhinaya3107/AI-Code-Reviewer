import pino from "pino";
import { beforeAll, describe, expect, it } from "vitest";
import type { LlmClient } from "../src/services/llm.js";
import { formatComment } from "../src/utils/commentFormat.js";

// The pipeline imports modules that read env vars at load time, so set dummies first.
process.env.GITHUB_APP_ID = "1";
process.env.GITHUB_PRIVATE_KEY = "dummy";
process.env.GITHUB_WEBHOOK_SECRET = "dummy";

let reviewPullRequest: typeof import("../src/services/reviewPipeline.js").reviewPullRequest;
beforeAll(async () => {
  ({ reviewPullRequest } = await import("../src/services/reviewPipeline.js"));
});

const log = pino({ level: "silent" });
const REF = { owner: "o", repo: "r", prNumber: 7 };

const addedFilePatch = (lines: string[]) => `@@ -0,0 +1,${lines.length} @@\n` + lines.map((l) => `+${l}`).join("\n");

interface World {
  files: { filename: string; status: string; patch?: string }[];
  config?: string; // contents of .aireview.yml on the base branch
  existingBotComments?: { path: string; body: string; user: { type: string } }[];
  draft?: boolean;
}

/** A fake GitHub: answers the exact requests the pipeline makes and records the review it posts. */
function fakeGithub(world: World) {
  const calls: { route: string; params: Record<string, unknown> }[] = [];
  let posted: { body: string; comments: { path: string; line: number; body: string }[] } | undefined;
  const octokit = {
    request: async (route: string, params: Record<string, unknown>) => {
      calls.push({ route, params });
      if (route === "GET /repos/{owner}/{repo}/pulls/{pull_number}")
        return { data: { title: "Test PR", body: "desc", draft: world.draft ?? false, head: { sha: "abc123" }, base: { ref: "main" } } };
      if (route === "GET /repos/{owner}/{repo}/contents/{path}") {
        if (world.config === undefined) throw Object.assign(new Error("Not Found"), { status: 404 });
        return { data: { type: "file", content: Buffer.from(world.config).toString("base64") } };
      }
      if (route === "GET /repos/{owner}/{repo}/pulls/{pull_number}/files") return { data: params.page === 1 ? world.files : [] };
      if (route === "GET /repos/{owner}/{repo}/pulls/{pull_number}/comments")
        return { data: params.page === 1 ? (world.existingBotComments ?? []) : [] };
      if (route === "POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews") {
        posted = { body: params.body as string, comments: params.comments as never };
        return { data: {} };
      }
      throw new Error(`unexpected request: ${route}`);
    },
  };
  return { octokit: octokit as never, calls, getPosted: () => posted };
}

/** A scripted LLM that records every prompt it receives. */
function scriptedLlm(comments: object[] = [], overall_risk = "low") {
  const seen: { system: string; user: string }[] = [];
  const llm: LlmClient = {
    model: "scripted",
    review: async (args) => {
      seen.push(args);
      return JSON.stringify({ summary: "ok", overall_risk, comments });
    },
  };
  return { llm, seen };
}

describe("pipeline: secret redaction", () => {
  it("never sends secrets to the LLM and posts a critical comment on the line", async () => {
    const gh = fakeGithub({
      files: [
        {
          filename: "src/a.ts",
          status: "added",
          patch: addedFilePatch(['const DB_PASSWORD = "hunter2-not-a-real-one";', 'const awsKey = "AKIAIOSFODNN7EXAMPLE";', "export const ok = 1;"]),
        },
      ],
    });
    const { llm, seen } = scriptedLlm();
    const out = await reviewPullRequest(gh.octokit, llm, REF, log);

    const sent = seen.map((s) => s.user).join("\n");
    expect(sent).not.toContain("hunter2-not-a-real-one");
    expect(sent).not.toContain("AKIAIOSFODNN7EXAMPLE");
    expect(sent).toContain("[REDACTED:");
    expect(sent).toContain("export const ok = 1;");

    const review = gh.getPosted()!;
    expect(out.status).toBe("completed");
    expect(review.comments.map((c) => c.line)).toEqual([1, 2]);
    expect(review.comments[0].body).toContain("Critical | Security");
    expect(review.comments[0].body).toContain("Revoke and rotate");
    expect(review.body).toContain("2 possible secret(s) were detected and masked");
    // The review itself must not contain the secret either.
    expect(JSON.stringify(review)).not.toContain("hunter2-not-a-real-one");
  });
});

describe("pipeline: repeat-comment skipping", () => {
  const patch = addedFilePatch(["let a = 1;", "let b = 2;", "let c = 3;"]);
  const llmComment = { path: "src/a.ts", line: 2, severity: "warning", category: "bug", message: "Variable b is never used." };

  it("does not repost a comment the bot already made on an earlier commit (even if its line moved)", async () => {
    const earlier = formatComment({ ...llmComment, line: 9 } as never);
    const gh = fakeGithub({
      files: [{ filename: "src/a.ts", status: "modified", patch }],
      existingBotComments: [{ path: "src/a.ts", body: earlier, user: { type: "Bot" } }],
    });
    const { llm } = scriptedLlm([llmComment]);
    const out = await reviewPullRequest(gh.octokit, llm, REF, log);

    expect(gh.getPosted()!.comments).toHaveLength(0);
    expect(gh.getPosted()!.body).toContain("1 comment(s) already posted on an earlier commit were not repeated");
    expect(out.commentsPosted).toBe(0);
  });

  it("still posts new comments, and ignores identical text written by a human", async () => {
    const gh = fakeGithub({
      files: [{ filename: "src/a.ts", status: "modified", patch }],
      existingBotComments: [{ path: "src/a.ts", body: formatComment(llmComment as never), user: { type: "User" } }],
    });
    const { llm } = scriptedLlm([llmComment]);
    await reviewPullRequest(gh.octokit, llm, REF, log);
    expect(gh.getPosted()!.comments).toHaveLength(1);
  });
});

describe("pipeline: .aireview.yml", () => {
  const patch = addedFilePatch(["a", "b", "c", "d"]);
  const mk = (line: number, severity: string) => ({ path: "src/a.ts", line, severity, category: "bug", message: `issue ${line}` });

  it("reads the config from the BASE branch, then applies threshold, cap, ignore paths and guidance", async () => {
    const gh = fakeGithub({
      config: "severity_threshold: warning\nmax_comments: 1\nignore_paths:\n  - 'docs/**'\nfocus: [security]\ncustom_instructions: Prefer Result types.\n",
      files: [
        { filename: "src/a.ts", status: "added", patch },
        { filename: "docs/guide.ts", status: "added", patch: addedFilePatch(["IGNORED_FILE_CONTENT"]) },
      ],
    });
    const { llm, seen } = scriptedLlm([mk(1, "nitpick"), mk(2, "warning"), mk(3, "critical"), mk(4, "suggestion")]);
    await reviewPullRequest(gh.octokit, llm, REF, log);

    const cfgCall = gh.calls.find((c) => c.route === "GET /repos/{owner}/{repo}/contents/{path}")!;
    expect(cfgCall.params).toMatchObject({ path: ".aireview.yml", ref: "main" });

    expect(seen.map((s) => s.user).join("")).not.toContain("IGNORED_FILE_CONTENT");
    expect(seen[0].system).toContain("Prioritise these categories: security");
    expect(seen[0].system).toContain("Prefer Result types.");

    // nitpick and suggestion are hidden; of critical + warning only the most severe is kept (cap = 1)
    const review = gh.getPosted()!;
    expect(review.comments).toHaveLength(1);
    expect(review.comments[0].line).toBe(3);
  });

  it("falls back to defaults and says so when the config is invalid", async () => {
    const gh = fakeGithub({ config: "severity_threshold: loud", files: [{ filename: "src/a.ts", status: "added", patch }] });
    const { llm } = scriptedLlm([mk(1, "nitpick")]);
    await reviewPullRequest(gh.octokit, llm, REF, log);
    expect(gh.getPosted()!.comments).toHaveLength(1); // defaults keep nitpicks
    expect(gh.getPosted()!.body).toContain("Ignored invalid .aireview.yml");
  });

  it("skips the review entirely when disabled", async () => {
    const gh = fakeGithub({ config: "enabled: false", files: [{ filename: "src/a.ts", status: "added", patch }] });
    const { llm, seen } = scriptedLlm();
    const out = await reviewPullRequest(gh.octokit, llm, REF, log);
    expect(out).toMatchObject({ status: "skipped", reason: expect.stringContaining("disabled") });
    expect(seen).toHaveLength(0);
    expect(gh.getPosted()).toBeUndefined();
  });
});

describe("pipeline: skipping and size guard", () => {
  it("skips drafts and PRs with nothing reviewable, without calling the LLM", async () => {
    const { llm, seen } = scriptedLlm();
    const draft = fakeGithub({ draft: true, files: [] });
    expect((await reviewPullRequest(draft.octokit, llm, REF, log)).reason).toBe("draft");
    const lock = fakeGithub({ files: [{ filename: "package-lock.json", status: "modified", patch: addedFilePatch(["x"]) }] });
    expect((await reviewPullRequest(lock.octokit, llm, REF, log)).reason).toBe("no reviewable files");
    expect(seen).toHaveLength(0);
  });

  it("caps a huge PR, reviews higher-priority files first, and says what was skipped", async () => {
    const bigPatch = addedFilePatch(Array.from({ length: 1000 }, () => "x".repeat(50)));
    const files = [
      { filename: "README.md", status: "added", patch: bigPatch }, // docs: lowest priority
      ...Array.from({ length: 6 }, (_, i) => ({ filename: `src/f${i}.ts`, status: "added", patch: bigPatch })),
    ];
    const gh = fakeGithub({ files });
    const { llm, seen } = scriptedLlm();
    await reviewPullRequest(gh.octokit, llm, REF, log);

    const sent = seen.map((s) => s.user).join("\n");
    expect(sent).toContain("### File: src/f0.ts");
    expect(sent).not.toContain("### File: README.md"); // docs dropped first
    expect(gh.getPosted()!.body).toMatch(/PR is large: \d+ lower-priority file\(s\) were not reviewed/);
  });
});
