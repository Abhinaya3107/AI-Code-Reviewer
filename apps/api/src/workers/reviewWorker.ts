import { Worker } from "bullmq";
import pino from "pino";
import { config } from "../config.js";
import { db } from "../db/client.js";
import { connection, REVIEW_QUEUE, type ReviewJob } from "../queue/reviewQueue.js";
import { getInstallationOctokit, reactToComment } from "../services/github.js";
import { createLlm } from "../services/llm.js";
import { resolveJob } from "../services/resolveJob.js";
import { reviewPullRequest, type ReviewOutcome } from "../services/reviewPipeline.js";
import { beginReview, failReview, finishReview } from "../services/reviewStore.js";

const log = pino();
const llm = createLlm({ provider: config.LLM_PROVIDER, apiKey: config.LLM_API_KEY, model: config.LLM_MODEL });

const worker = new Worker<ReviewJob>(
  REVIEW_QUEUE,
  async (job) => {
    const { installationId, repo, prNumber } = job.data;
    const [owner, name] = repo.split("/");
    const started = Date.now();

    const octokit = await getInstallationOctokit(installationId);
    const data = await resolveJob(octokit, job.data);
    // Tell the person who typed "/review" that it was picked up.
    if (data.commentId) await reactToComment(octokit, { owner, repo: name }, data.commentId, "eyes");

    const began = db ? await beginReview(db, data) : null;

    let outcome: ReviewOutcome;
    try {
      if (began && !began.repositoryEnabled) {
        outcome = { status: "skipped", reason: "repository disabled" };
      } else {
        outcome = await reviewPullRequest(octokit, llm, { owner, repo: name, prNumber }, log);
      }
    } catch (err) {
      if (db && began) {
        const final = job.attemptsMade + 1 >= (job.opts.attempts ?? 1);
        await failReview(db, began.reviewId, err, final).catch((e) => log.error({ err: String(e) }, "could not record failure"));
      }
      throw err;
    }

    if (db && began) await finishReview(db, began, outcome, Date.now() - started, { append: data.trigger === "command" });
    const { comments: _omit, ...summary } = outcome; // keep log lines small
    log.info({ repo, prNumber, trigger: data.trigger, stored: Boolean(db), ...summary }, "review finished");
  },
  { connection, concurrency: 2 },
);

worker.on("failed", (job, err) => log.error({ jobId: job?.id, err: err.message }, "review job failed"));
log.info(`review worker started (llm: ${llm.model}, database: ${db ? "on" : "off"})`);
