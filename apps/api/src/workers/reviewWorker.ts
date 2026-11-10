import { Worker } from "bullmq";
import pino from "pino";
import { config } from "../config.js";
import { connection, REVIEW_QUEUE, type ReviewJob } from "../queue/reviewQueue.js";
import { getInstallationOctokit } from "../services/github.js";
import { createLlm } from "../services/llm.js";
import { reviewPullRequest } from "../services/reviewPipeline.js";

const log = pino();
const llm = createLlm({ provider: config.LLM_PROVIDER, apiKey: config.LLM_API_KEY, model: config.LLM_MODEL });

const worker = new Worker<ReviewJob>(
  REVIEW_QUEUE,
  async (job) => {
    const { installationId, repo, prNumber } = job.data;
    const [owner, name] = repo.split("/");
    const octokit = await getInstallationOctokit(installationId);
    const outcome = await reviewPullRequest(octokit, llm, { owner, repo: name, prNumber }, log);
    log.info({ repo, prNumber, ...outcome }, "review finished");
  },
  { connection, concurrency: 2 },
);

worker.on("failed", (job, err) => log.error({ jobId: job?.id, err: err.message }, "review job failed"));
log.info(`review worker started (llm: ${llm.model})`);
