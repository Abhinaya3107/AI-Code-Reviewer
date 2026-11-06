import express, { Router } from "express";
import { config } from "../config.js";
import { reviewQueue } from "../queue/reviewQueue.js";
import { verifySignature } from "../utils/verifySignature.js";

export const webhooksRouter = Router();

const REVIEW_ACTIONS = ["opened", "synchronize", "reopened"];

webhooksRouter.post("/webhooks/github", express.raw({ type: "application/json" }), async (req, res) => {
  const sig = req.headers["x-hub-signature-256"] as string | undefined;
  if (!Buffer.isBuffer(req.body) || !verifySignature(req.body, sig, config.GITHUB_WEBHOOK_SECRET)) {
    return res.sendStatus(401);
  }

  const event = req.headers["x-github-event"];
  const deliveryId = String(req.headers["x-github-delivery"] ?? "");
  const payload = JSON.parse(req.body.toString("utf8"));

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
        repo: payload.repository.full_name,
        prNumber: pr.number,
        headSha: pr.head.sha,
      },
      // Same repo+PR+sha => same job id => GitHub redeliveries are no-ops.
      { jobId: `${payload.repository.id}-${pr.number}-${pr.head.sha}` },
    );
  }
  res.sendStatus(202);
});
