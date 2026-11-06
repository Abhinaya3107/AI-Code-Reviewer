import { Queue } from "bullmq";
import { Redis } from "ioredis";
import { config } from "../config.js";

export interface ReviewJob {
  deliveryId: string;
  installationId: number;
  repo: string; // owner/name
  prNumber: number;
  headSha: string;
}

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
