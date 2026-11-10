import { describe, expect, it } from "vitest";
import type { ReviewJob } from "../src/queue/reviewQueue.js";
import type { ReviewOutcome } from "../src/services/reviewPipeline.js";
import { beginReview, failReview, finishReview, fingerprintOf } from "../src/services/reviewStore.js";
import { estimateCostUsd } from "../src/utils/cost.js";

const job: ReviewJob = {
  deliveryId: "d",
  installationId: 11,
  accountLogin: "octo",
  repoId: 1442699472, // larger than 2^30: must survive as BigInt
  repo: "octo/app",
  prNumber: 7,
  prTitle: "Add thing",
  prAuthor: "alice",
  headSha: "abc123",
};

/** Records every call so tests can assert what would be written, without needing a database. */
function fakeDb(opts: { repositoryEnabled?: boolean; installationRow?: boolean } = {}) {
  const calls: { op: string; args: any }[] = [];
  const rec = (op: string, result: unknown = {}) => async (args: unknown) => {
    calls.push({ op, args });
    return result;
  };
  const db: any = {
    installation: { upsert: rec("installation.upsert", { id: 1 }), findUnique: rec("installation.find", opts.installationRow === false ? null : { id: 1 }) },
    repository: { upsert: rec("repository.upsert", { id: 2, enabled: opts.repositoryEnabled ?? true }) },
    pullRequest: { upsert: rec("pullRequest.upsert", { id: 3 }) },
    review: { upsert: rec("review.upsert", { id: 4 }), update: rec("review.update") },
    reviewComment: { deleteMany: rec("comment.deleteMany"), createMany: rec("comment.createMany") },
    usageEvent: { create: rec("usage.create") },
  };
  db.$transaction = async (fn: (tx: unknown) => unknown) => fn(db);
  return { db, calls, ops: () => calls.map((c) => c.op), find: (op: string) => calls.find((c) => c.op === op)?.args };
}

describe("beginReview", () => {
  it("upserts installation, repository, pull request and review in order, keyed for idempotency", async () => {
    const f = fakeDb();
    const r = await beginReview(f.db, job);
    expect(r).toEqual({ reviewId: 4, installationDbId: 1, repositoryEnabled: true });
    expect(f.ops()).toEqual(["installation.upsert", "repository.upsert", "pullRequest.upsert", "review.upsert"]);
    expect(f.find("repository.upsert").where.githubRepoId).toBe(1442699472n);
    expect(f.find("review.upsert").where).toEqual({ pullRequestId_headSha: { pullRequestId: 3, headSha: "abc123" } });
    expect(f.find("review.upsert").update.attempts).toEqual({ increment: 1 }); // a retry reuses the row
  });

  it("reports a disabled repository", async () => {
    const r = await beginReview(fakeDb({ repositoryEnabled: false }).db, job);
    expect(r.repositoryEnabled).toBe(false);
  });
});

describe("finishReview", () => {
  const outcome: ReviewOutcome = {
    status: "completed",
    summary: "s",
    risk: "medium",
    model: "gemini-2.5-flash",
    filesReviewed: 2,
    usage: { inputTokens: 1000, outputTokens: 200 },
    comments: [{ path: "a.ts", line: 3, severity: "warning", category: "bug", message: "Missing await.", suggested_fix: "await x();" }],
  };

  const began = { reviewId: 4, installationDbId: 1 };

  it("writes everything in ONE request: tokens, cost, replaced comments and a monthly usage event", async () => {
    const f = fakeDb();
    await finishReview(f.db, began, outcome, 1234);
    expect(f.ops()).toEqual(["review.update"]); // a single round trip, no interactive transaction
    const { data, where } = f.find("review.update");
    expect(where).toEqual({ id: 4 });
    expect(data).toMatchObject({ status: "completed", overallRisk: "medium", inputTokens: 1000, outputTokens: 200, durationMs: 1234, errorMessage: null });
    expect(data.costUsd).toBeCloseTo(estimateCostUsd("gemini-2.5-flash", 1000, 200)!, 10);
    expect(data.comments.deleteMany).toEqual({}); // re-running replaces old comments
    const row = data.comments.create[0];
    expect(row).toMatchObject({ path: "a.ts", line: 3, suggestedFix: "await x();" });
    expect(row.fingerprint).toBe(fingerprintOf("a.ts", "Missing await."));
    expect(data.usageEvents.create).toMatchObject({ installationId: 1, tokens: 1200 });
    expect(data.usageEvents.create.month).toMatch(/^\d{4}-\d{2}$/);
  });

  it("stores skipped reviews with their reason and no comments or usage", async () => {
    const f = fakeDb();
    await finishReview(f.db, began, { status: "skipped", reason: "draft" }, 5);
    expect(f.ops()).toEqual(["review.update"]);
    const { data } = f.find("review.update");
    expect(data).toMatchObject({ status: "skipped", skipReason: "draft" });
    expect(data.comments).toBeUndefined();
  });

  it("creates no usage event when the provider reported no tokens", async () => {
    const f = fakeDb();
    await finishReview(f.db, began, { ...outcome, usage: { inputTokens: 0, outputTokens: 0 } }, 5);
    expect(f.find("review.update").data.usageEvents.create).toBeUndefined();
  });
});

describe("failReview", () => {
  it("marks failed only on the final attempt, otherwise puts it back to queued", async () => {
    const a = fakeDb();
    await failReview(a.db, 4, new Error("boom"), false);
    expect(a.find("review.update").data).toMatchObject({ status: "queued", errorMessage: "boom" });
    const b = fakeDb();
    await failReview(b.db, 4, new Error("boom"), true);
    expect(b.find("review.update").data).toMatchObject({ status: "failed", errorMessage: "boom" });
    expect(b.find("review.update").data.finishedAt).toBeInstanceOf(Date);
  });
});

describe("estimateCostUsd", () => {
  it("is null for models without a known price", () => {
    expect(estimateCostUsd("fake-llm", 1000, 1000)).toBeNull();
    expect(estimateCostUsd("gemini-2.5-flash", 1_000_000, 0)).toBeCloseTo(0.3, 6);
  });
});
