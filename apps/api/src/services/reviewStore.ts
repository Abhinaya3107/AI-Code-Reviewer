import type { PrismaClient } from "@prisma/client";
import type { ReviewJob } from "../queue/reviewQueue.js";
import { commentKey } from "../utils/commentFormat.js";
import { estimateCostUsd } from "../utils/cost.js";
import type { ReviewOutcome } from "./reviewPipeline.js";
import crypto from "node:crypto";

/** Stable id for "the same finding", used to recognise repeats across commits. */
export const fingerprintOf = (path: string, message: string) =>
  crypto.createHash("sha256").update(commentKey(path, message)).digest("hex").slice(0, 32);

export interface BeganReview {
  reviewId: number;
  /** False when the repository was switched off (e.g. from the dashboard). */
  repositoryEnabled: boolean;
}

/** Records that a review is starting. Safe to call again on a retry: one row per (PR, commit). */
export async function beginReview(db: PrismaClient, job: ReviewJob): Promise<BeganReview> {
  const installation = await db.installation.upsert({
    where: { githubInstallationId: BigInt(job.installationId) },
    create: { githubInstallationId: BigInt(job.installationId), accountLogin: job.accountLogin },
    update: { accountLogin: job.accountLogin },
  });
  const repository = await db.repository.upsert({
    where: { githubRepoId: BigInt(job.repoId) },
    create: { githubRepoId: BigInt(job.repoId), fullName: job.repo, installationId: installation.id },
    update: { fullName: job.repo },
  });
  const pullRequest = await db.pullRequest.upsert({
    where: { repositoryId_number: { repositoryId: repository.id, number: job.prNumber } },
    create: { repositoryId: repository.id, number: job.prNumber, title: job.prTitle, author: job.prAuthor },
    update: { title: job.prTitle, author: job.prAuthor },
  });
  const now = new Date();
  const review = await db.review.upsert({
    where: { pullRequestId_headSha: { pullRequestId: pullRequest.id, headSha: job.headSha } },
    create: { pullRequestId: pullRequest.id, headSha: job.headSha, status: "running", startedAt: now, attempts: 1 },
    update: { status: "running", startedAt: now, errorMessage: null, attempts: { increment: 1 } },
  });
  return { reviewId: review.id, repositoryEnabled: repository.enabled };
}

/** Stores the result. Re-running the same review replaces its comments instead of duplicating them. */
export async function finishReview(
  db: PrismaClient,
  reviewId: number,
  installationId: number,
  outcome: ReviewOutcome,
  durationMs: number,
): Promise<void> {
  const finishedAt = new Date();

  if (outcome.status === "skipped") {
    await db.review.update({
      where: { id: reviewId },
      data: { status: "skipped", skipReason: outcome.reason ?? null, durationMs, finishedAt },
    });
    return;
  }

  const inputTokens = outcome.usage?.inputTokens ?? 0;
  const outputTokens = outcome.usage?.outputTokens ?? 0;
  const costUsd = outcome.model ? estimateCostUsd(outcome.model, inputTokens, outputTokens) : null;

  await db.$transaction(async (tx) => {
    await tx.review.update({
      where: { id: reviewId },
      data: {
        status: "completed",
        summary: outcome.summary ?? null,
        overallRisk: outcome.risk ?? null,
        model: outcome.model ?? null,
        inputTokens,
        outputTokens,
        costUsd,
        durationMs,
        filesReviewed: outcome.filesReviewed ?? null,
        finishedAt,
      },
    });
    await tx.reviewComment.deleteMany({ where: { reviewId } });
    if (outcome.comments?.length) {
      await tx.reviewComment.createMany({
        data: outcome.comments.map((c) => ({
          reviewId,
          path: c.path,
          line: c.line,
          severity: c.severity,
          category: c.category,
          message: c.message,
          suggestedFix: c.suggested_fix ?? null,
          fingerprint: fingerprintOf(c.path, c.message),
        })),
      });
    }
    if (inputTokens + outputTokens > 0) {
      const installation = await tx.installation.findUnique({ where: { githubInstallationId: BigInt(installationId) } });
      if (installation) {
        await tx.usageEvent.create({
          data: {
            installationId: installation.id,
            reviewId,
            month: finishedAt.toISOString().slice(0, 7),
            tokens: inputTokens + outputTokens,
            costUsd,
          },
        });
      }
    }
  });
}

/** `final` is true on the last retry: only then is the review marked failed (otherwise it is queued again). */
export async function failReview(db: PrismaClient, reviewId: number, error: unknown, final: boolean): Promise<void> {
  const message = (error instanceof Error ? error.message : String(error)).slice(0, 500);
  await db.review.update({
    where: { id: reviewId },
    data: { status: final ? "failed" : "queued", errorMessage: message, ...(final ? { finishedAt: new Date() } : {}) },
  });
}
