import type { Logger } from "pino";
import { chunkByBudget } from "../utils/chunk.js";
import { parsePatch } from "../utils/diffParser.js";
import { selectFiles } from "../utils/fileFilter.js";
import { validateComments } from "../utils/validateComments.js";
import { fetchPullFiles, fetchPullRequest, postReview, type InstallationOctokit, type PrRef } from "./github.js";
import type { LlmClient } from "./llm.js";
import { buildUserPrompt, SYSTEM_PROMPT } from "./prompt.js";
import { reviewResultSchema, type ReviewResult } from "./reviewSchema.js";

const MAX_CHUNK_CHARS = 60_000; // ~15k tokens of diff per LLM call
const MAX_COMMENTS = 15;
const RISK_ORDER = ["low", "medium", "high"] as const;

/** Calls the LLM and validates the JSON. On a parse/schema failure, retries once with the error. */
async function reviewChunk(llm: LlmClient, user: string): Promise<ReviewResult> {
  let prompt = user;
  let lastError = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    const raw = await llm.review({ system: SYSTEM_PROMPT, user: prompt });
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

  const allFiles = await fetchPullFiles(octokit, ref);
  const files = selectFiles(allFiles);
  if (files.length === 0) return { status: "skipped", reason: "no reviewable files" };

  const validLinesByPath = new Map<string, Set<number>>();
  const parsed = files.map((f) => {
    const p = parsePatch(f.patch!);
    validLinesByPath.set(f.filename, p.validLines);
    return { filename: f.filename, annotated: p.annotated };
  });

  const results: ReviewResult[] = [];
  for (const chunk of chunkByBudget(parsed, MAX_CHUNK_CHARS)) {
    results.push(await reviewChunk(llm, buildUserPrompt(pr, chunk)));
  }

  const merged = {
    summary: results.map((r) => r.summary).join(" "),
    risk: results.reduce((max, r) => (RISK_ORDER.indexOf(r.overall_risk) > RISK_ORDER.indexOf(max) ? r.overall_risk : max), "low" as ReviewResult["overall_risk"]),
    comments: results.flatMap((r) => r.comments),
  };

  const outcome = validateComments(merged.comments, validLinesByPath, { maxComments: MAX_COMMENTS });
  const dropped = outcome.droppedInvalidLine + outcome.droppedDuplicate + outcome.droppedOverCap;
  log.info(
    {
      files: files.length,
      proposed: merged.comments.length,
      kept: outcome.kept.length,
      droppedInvalidLine: outcome.droppedInvalidLine,
      droppedDuplicate: outcome.droppedDuplicate,
      droppedOverCap: outcome.droppedOverCap,
    },
    "review prepared",
  );

  const body = `## AI Code Review\n\n${merged.summary}\n\n**Overall risk:** ${merged.risk}\n\n_Reviewed ${files.length} file(s) with \`${llm.model}\`._`;
  await postReview(octokit, ref, { headSha: pr.headSha, body, comments: outcome.kept });

  return { status: "completed", commentsPosted: outcome.kept.length, droppedComments: dropped };
}
