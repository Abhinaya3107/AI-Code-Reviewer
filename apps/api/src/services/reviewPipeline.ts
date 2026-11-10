import type { Logger } from "pino";
import { chunkByBudget } from "../utils/chunk.js";
import { commentKey } from "../utils/commentFormat.js";
import { parsePatch } from "../utils/diffParser.js";
import { selectFiles } from "../utils/fileFilter.js";
import { globToRegex } from "../utils/glob.js";
import { redactPatch } from "../utils/redact.js";
import { validateComments } from "../utils/validateComments.js";
import {
  fetchPostedCommentKeys,
  fetchPullFiles,
  fetchPullRequest,
  fetchRepoFile,
  postReview,
  type InstallationOctokit,
  type PrRef,
} from "./github.js";
import type { LlmClient } from "./llm.js";
import { buildSystemPrompt, buildUserPrompt } from "./prompt.js";
import { CONFIG_PATH, DEFAULT_CONFIG, parseRepoConfig } from "./repoConfig.js";
import { reviewResultSchema, type ReviewComment, type ReviewResult } from "./reviewSchema.js";

const MAX_CHUNK_CHARS = 60_000; // ~15k tokens of diff per LLM call
const MAX_TOTAL_CHARS = 240_000; // ~60k tokens per PR: beyond this, lower-priority files are skipped
const RISK_ORDER = ["low", "medium", "high"] as const;

/** Calls the LLM and validates the JSON. On a parse/schema failure, retries once with the error. */
async function reviewChunk(llm: LlmClient, system: string, user: string): Promise<ReviewResult> {
  let prompt = user;
  let lastError = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    const raw = await llm.review({ system, user: prompt });
    try {
      const json = JSON.parse(raw.replace(/^```(?:json)?\s*|\s*```$/g, "").trim());
      return reviewResultSchema.parse(json);
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
      prompt = `${user}\n\nYour previous reply was invalid: ${lastError.slice(0, 500)}\nReturn ONLY valid JSON matching the schema.`;
    }
  }
  throw new Error(`LLM returned invalid output twice: ${lastError.slice(0, 300)}`);
}

export interface ReviewOutcome {
  status: "completed" | "skipped";
  reason?: string;
  commentsPosted?: number;
  droppedComments?: number;
}

export async function reviewPullRequest(
  octokit: InstallationOctokit,
  llm: LlmClient,
  ref: PrRef,
  log: Logger,
): Promise<ReviewOutcome> {
  const pr = await fetchPullRequest(octokit, ref);
  if (pr.draft) return { status: "skipped", reason: "draft" };

  // Config is read from the BASE branch, not the PR head: otherwise a PR author could rewrite the
  // reviewer's instructions in the very PR being reviewed.
  const configText = await fetchRepoFile(octokit, ref, CONFIG_PATH, pr.baseRef);
  const { config: cfg, error: configError } = configText ? parseRepoConfig(configText) : { config: DEFAULT_CONFIG, error: undefined };
  if (!cfg.enabled) return { status: "skipped", reason: `disabled in ${CONFIG_PATH}` };

  const allFiles = await fetchPullFiles(octokit, ref);
  const files = selectFiles(allFiles, cfg.ignore_paths.map(globToRegex));
  if (files.length === 0) return { status: "skipped", reason: "no reviewable files" };

  const validLinesByPath = new Map<string, Set<number>>();
  const secretComments: ReviewComment[] = [];
  const parsed: { filename: string; annotated: string }[] = [];
  const skippedFiles: string[] = [];
  let totalChars = 0;

  for (const f of files) {
    // Secrets are masked BEFORE anything is sent to the LLM or logged.
    const redacted = redactPatch(f.patch!);
    const p = parsePatch(redacted.patch);
    const size = Math.min(p.annotated.length, MAX_CHUNK_CHARS);
    if (totalChars + size > MAX_TOTAL_CHARS) {
      skippedFiles.push(f.filename);
      continue;
    }
    totalChars += size;
    validLinesByPath.set(f.filename, p.validLines);
    parsed.push({ filename: f.filename, annotated: p.annotated });
    for (const s of redacted.findings) {
      secretComments.push({
        path: f.filename,
        line: s.line,
        severity: "critical",
        category: "security",
        message: `Possible secret (${s.type}) added here. It was masked before this review, but it is now in the git history. Revoke and rotate it, and load it from an environment variable or secret manager instead.`,
      });
    }
  }

  const system = buildSystemPrompt(cfg);
  const results: ReviewResult[] = [];
  for (const chunk of chunkByBudget(parsed, MAX_CHUNK_CHARS)) {
    results.push(await reviewChunk(llm, system, buildUserPrompt(pr, chunk)));
  }

  const risk = results.reduce(
    (max, r) => (RISK_ORDER.indexOf(r.overall_risk) > RISK_ORDER.indexOf(max) ? r.overall_risk : max),
    "low" as ReviewResult["overall_risk"],
  );
  const summary = results.map((r) => r.summary).join(" ");
  const proposed = [...secretComments, ...results.flatMap((r) => r.comments)];

  // Skip what this bot already said on an earlier commit of the same PR.
  let posted = new Set<string>();
  try {
    posted = await fetchPostedCommentKeys(octokit, ref);
  } catch (err) {
    log.warn({ err: err instanceof Error ? err.message : String(err) }, "could not load earlier comments; not de-duplicating");
  }
  const fresh = proposed.filter((c) => !posted.has(commentKey(c.path, c.message)));
  const alreadyPosted = proposed.length - fresh.length;

  const outcome = validateComments(fresh, validLinesByPath, {
    maxComments: cfg.max_comments,
    minSeverity: cfg.severity_threshold,
  });
  const dropped = alreadyPosted + outcome.droppedBelowThreshold + outcome.droppedInvalidLine + outcome.droppedDuplicate + outcome.droppedOverCap;
  log.info(
    {
      files: parsed.length,
      skippedFiles: skippedFiles.length,
      approxTokens: Math.round(totalChars / 4),
      secrets: secretComments.length,
      proposed: proposed.length,
      kept: outcome.kept.length,
      alreadyPosted,
      droppedBelowThreshold: outcome.droppedBelowThreshold,
      droppedInvalidLine: outcome.droppedInvalidLine,
      droppedDuplicate: outcome.droppedDuplicate,
      droppedOverCap: outcome.droppedOverCap,
    },
    "review prepared",
  );

  const notes: string[] = [];
  if (secretComments.length) notes.push(`${secretComments.length} possible secret(s) were detected and masked before analysis.`);
  if (alreadyPosted) notes.push(`${alreadyPosted} comment(s) already posted on an earlier commit were not repeated.`);
  if (outcome.droppedInvalidLine) notes.push(`${outcome.droppedInvalidLine} comment(s) were skipped because they pointed at lines outside the diff.`);
  if (skippedFiles.length) notes.push(`PR is large: ${skippedFiles.length} lower-priority file(s) were not reviewed.`);
  if (configError) notes.push(`Ignored invalid ${CONFIG_PATH} (${configError}); defaults used.`);

  const body =
    `## AI Code Review\n\n${summary}\n\n**Overall risk:** ${risk}\n\n` +
    `_Reviewed ${parsed.length} file(s) with \`${llm.model}\`, ${outcome.kept.length} new inline comment(s)._` +
    (notes.length ? `\n\n${notes.map((n) => `- ${n}`).join("\n")}` : "");
  await postReview(octokit, ref, { headSha: pr.headSha, body, comments: outcome.kept });

  return { status: "completed", commentsPosted: outcome.kept.length, droppedComments: dropped };
}
