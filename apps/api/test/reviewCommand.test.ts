import { describe, expect, it } from "vitest";
import type { ReviewJob } from "../src/queue/reviewQueue.js";
import { toReviewJob } from "../src/services/webhookEvents.js";
import { parseCommand } from "../src/utils/command.js";

// resolveJob pulls in the GitHub module, which validates env vars at load time.
process.env.GITHUB_APP_ID = "1";
process.env.GITHUB_PRIVATE_KEY = "dummy";
process.env.GITHUB_WEBHOOK_SECRET = "dummy";

describe("parseCommand", () => {
  it("recognises /review on the first non-empty line, any case, with trailing text", () => {
    expect(parseCommand("/review")).toBe("review");
    expect(parseCommand("  /Review  ")).toBe("review");
    expect(parseCommand("\n\n/review please look again")).toBe("review");
  });

  it("ignores anything else", () => {
    expect(parseCommand("/reviewer")).toBeNull();
    expect(parseCommand("/review-all")).toBeNull();
    expect(parseCommand("please /review this")).toBeNull();
    expect(parseCommand("> /review")).toBeNull(); // quoted reply
    expect(parseCommand("looks good\n/review")).toBeNull(); // not the first line
    expect(parseCommand("")).toBeNull();
    expect(parseCommand(null)).toBeNull();
  });
});

const base = { installation: { id: 11, account: { login: "octo" } }, repository: { id: 99, full_name: "octo/app", owner: { login: "octo" } } };

describe("toReviewJob: pull_request events", () => {
  const pr = (over: object = {}) => ({
    ...base,
    action: "opened",
    pull_request: { number: 7, title: "T", draft: false, user: { login: "alice", type: "User" }, head: { sha: "abc" }, ...over },
  });

  it("queues opened/synchronize/reopened PRs with an idempotent job id", () => {
    for (const action of ["opened", "synchronize", "reopened"]) {
      const q = toReviewJob("pull_request", { ...pr(), action }, "d1")!;
      expect(q.jobId).toBe("99-7-abc");
      expect(q.job).toMatchObject({ trigger: "pull_request", prNumber: 7, headSha: "abc", prAuthor: "alice", repoId: 99, accountLogin: "octo" });
    }
  });

  it("ignores other actions, drafts and bot-authored PRs", () => {
    expect(toReviewJob("pull_request", { ...pr(), action: "closed" }, "d")).toBeNull();
    expect(toReviewJob("pull_request", pr({ draft: true }), "d")).toBeNull();
    expect(toReviewJob("pull_request", pr({ user: { login: "dependabot[bot]", type: "Bot" } }), "d")).toBeNull();
  });
});

describe("toReviewJob: /review comments", () => {
  const comment = (over: object = {}, issue: object = {}) => ({
    ...base,
    action: "created",
    issue: { number: 7, state: "open", pull_request: {}, ...issue },
    comment: { id: 555, body: "/review", user: { type: "User" }, author_association: "OWNER", ...over },
  });

  it("queues a command job without PR details, and with a per-comment job id", () => {
    const q = toReviewJob("issue_comment", comment(), "d")!;
    expect(q.jobId).toBe("99-7-cmd-555");
    expect(q.job).toMatchObject({ trigger: "command", prNumber: 7, commentId: 555 });
    expect(q.job.headSha).toBeUndefined(); // the worker looks it up
  });

  it("allows owners, members and collaborators only", () => {
    for (const a of ["OWNER", "MEMBER", "COLLABORATOR"]) expect(toReviewJob("issue_comment", comment({ author_association: a }), "d")).not.toBeNull();
    for (const a of ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", undefined]) expect(toReviewJob("issue_comment", comment({ author_association: a }), "d")).toBeNull();
  });

  it("ignores bots, edits, non-command text, plain issues and closed PRs", () => {
    expect(toReviewJob("issue_comment", comment({ user: { type: "Bot" } }), "d")).toBeNull();
    expect(toReviewJob("issue_comment", { ...comment(), action: "edited" }, "d")).toBeNull();
    expect(toReviewJob("issue_comment", comment({ body: "thanks!" }), "d")).toBeNull();
    expect(toReviewJob("issue_comment", comment({}, { pull_request: undefined }), "d")).toBeNull();
    expect(toReviewJob("issue_comment", comment({}, { state: "closed" }), "d")).toBeNull();
  });

  it("ignores unrelated events and payloads without an installation", () => {
    expect(toReviewJob("push", comment(), "d")).toBeNull();
    expect(toReviewJob("issue_comment", { ...comment(), installation: undefined }, "d")).toBeNull();
  });
});

describe("resolveJob", () => {
  it("looks up head SHA, title and author for a command job, and leaves complete jobs alone", async () => {
    const { resolveJob } = await import("../src/services/resolveJob.js");
    let calls = 0;
    const octokit = {
      request: async () => {
        calls++;
        return { data: { title: "Real title", body: "", draft: false, head: { sha: "sha1" }, base: { ref: "main" }, user: { login: "bob" } } };
      },
    } as never;
    const job: ReviewJob = { deliveryId: "d", installationId: 1, accountLogin: "o", repoId: 1, repo: "o/r", prNumber: 3, trigger: "command", commentId: 9 };
    expect(await resolveJob(octokit, job)).toMatchObject({ headSha: "sha1", prTitle: "Real title", prAuthor: "bob", commentId: 9 });
    expect(calls).toBe(1);

    const full = { ...job, trigger: "pull_request" as const, headSha: "x", prTitle: "t", prAuthor: "a" };
    expect(await resolveJob(octokit, full)).toBe(full);
    expect(calls).toBe(1); // no extra GitHub call
  });
});
