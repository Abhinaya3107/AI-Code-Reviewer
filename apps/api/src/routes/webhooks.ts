import express, { Router } from "express";
import pino from "pino";
import { config } from "../config.js";
import { reviewQueue } from "../queue/reviewQueue.js";
import { toReviewJob } from "../services/webhookEvents.js";
import { verifySignature } from "../utils/verifySignature.js";

const log = pino();
export const webhooksRouter = Router();

webhooksRouter.post("/webhooks/github", express.raw({ type: "application/json" }), async (req, res) => {
  const sig = req.headers["x-hub-signature-256"] as string | undefined;
  const deliveryId = String(req.headers["x-github-delivery"] ?? "");
  if (!Buffer.isBuffer(req.body) || !verifySignature(req.body, sig, config.GITHUB_WEBHOOK_SECRET)) {
    log.warn({ deliveryId, hasSignature: Boolean(sig) }, "webhook rejected: invalid signature (secret mismatch?)");
    return res.sendStatus(401);
  }

  const event = req.headers["x-github-event"] as string | undefined;
  const payload = JSON.parse(req.body.toString("utf8"));
  log.info({ deliveryId, event, action: payload.action }, "webhook received");

  const queued = toReviewJob(event, payload, deliveryId);
  if (queued) {
    await reviewQueue.add("review-pr", queued.job, { jobId: queued.jobId });
    log.info({ deliveryId, trigger: queued.job.trigger, repo: queued.job.repo, pr: queued.job.prNumber }, "review queued");
  }
  res.sendStatus(202);
});
