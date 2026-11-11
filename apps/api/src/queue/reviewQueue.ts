import { Queue } from "bullmq";
import { Redis } from "ioredis";
import { config } from "../config.js";

export interface ReviewJob {
  deliveryId: string;
  installationId: number;
  accountLogin: string;
  repoId: number;
  repo: string; // owner/name
  prNumber: number;
  /** What caused this job: a PR event, or someone commenting "/review". */
  trigger: "pull_request" | "command";
  /** Known for pull_request events. A comment event does not carry them, so the worker looks them up. */
  headSha?: string;
  prTitle?: string;
  prAuthor?: string;
  /** The "/review" comment, so the worker can acknowledge it. */
  commentId?: number;
}

/** A job whose PR details are all known. */
export type ResolvedReviewJob = ReviewJob & { headSha: string; prTitle: string; prAuthor: string };

export const REVIEW_QUEUE = "review-pr";

export const connection = new Redis(config.REDIS_URL, { maxRetriesPerRequest: null });

export const reviewQueue = new Queue<ReviewJob>(REVIEW_QUEUE, {
  connection,
  defaultJobOptions: {
    attempts: 3,
    backoff: { type: "exponential", delay: 5000 },
    removeOnComplete: 1000,
  },
});
