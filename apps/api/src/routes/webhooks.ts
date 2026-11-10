import express, { Router } from "express";
import pino from "pino";
import { config } from "../config.js";
import { reviewQueue } from "../queue/reviewQueue.js";
import { verifySignature } from "../utils/verifySignature.js";

const log = pino();
export const webhooksRouter = Router();

const REVIEW_ACTIONS = ["opened", "synchronize", "reopened"];

webhooksRouter.post("/webhooks/github", express.raw({ type: "application/json" }), async (req, res) => {
  const sig = req.headers["x-hub-signature-256"] as string | undefined;
  const deliveryId = String(req.headers["x-github-delivery"] ?? "");
  if (!Buffer.isBuffer(req.body) || !verifySignature(req.body, sig, config.GITHUB_WEBHOOK_SECRET)) {
    log.warn({ deliveryId, hasSignature: Boolean(sig) }, "webhook rejected: invalid signature (secret mismatch?)");
    return res.sendStatus(401);
  }

  const event = req.headers["x-github-event"];
  const payload = JSON.parse(req.body.toString("utf8"));
  log.info({ deliveryId, event, action: payload.action }, "webhook received");

  const pr = payload.pull_request;
  if (
    event === "pull_request" &&
    REVIEW_ACTIONS.includes(payload.action) &&
    !pr.draft &&
    pr.user?.type !== "Bot"
  ) {
    await reviewQueue.add(
      "review-pr",
      {
        deliveryId,
        installationId: payload.installation.id,
        accountLogin: payload.installation.account?.login ?? payload.repository.owner.login,
        repoId: payload.repository.id,
        repo: payload.repository.full_name,
        prNumber: pr.number,
        prTitle: String(pr.title ?? "").slice(0, 300),
        prAuthor: pr.user?.login ?? "unknown",
        headSha: pr.head.sha,
      },
      // Same repo+PR+sha => same job id => GitHub redeliveries are no-ops.
      { jobId: `${payload.repository.id}-${pr.number}-${pr.head.sha}` },
    );
  }
  res.sendStatus(202);
});
