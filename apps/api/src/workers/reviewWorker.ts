import { Worker } from "bullmq";
import pino from "pino";
import { connection, REVIEW_QUEUE, type ReviewJob } from "../queue/reviewQueue.js";
import { getInstallationOctokit } from "../services/github.js";

const log = pino();

// Week 1: prove the full loop by posting a hardcoded comment. Replaced by the real review pipeline in Week 2.
const worker = new Worker<ReviewJob>(
  REVIEW_QUEUE,
  async (job) => {
    const { installationId, repo, prNumber, headSha } = job.data;
    const [owner, name] = repo.split("/");
    const octokit = await getInstallationOctokit(installationId);
    await octokit.request("POST /repos/{owner}/{repo}/issues/{issue_number}/comments", {
      owner,
      repo: name,
      issue_number: prNumber,
      body: `AI Code Reviewer saw commit \`${headSha.slice(0, 7)}\`. Real review coming soon.`,
    });
    log.info({ repo, prNumber, headSha }, "posted placeholder comment");
  },
  { connection, concurrency: 2 },
);

worker.on("failed", (job, err) => log.error({ jobId: job?.id, err: err.message }, "review job failed"));
