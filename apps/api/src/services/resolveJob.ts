import type { ResolvedReviewJob, ReviewJob } from "../queue/reviewQueue.js";
import { fetchPullRequest, type InstallationOctokit } from "./github.js";

/** Fills in the PR details a "/review" comment event does not carry. PR events already have them. */
export async function resolveJob(octokit: InstallationOctokit, job: ReviewJob): Promise<ResolvedReviewJob> {
  if (job.headSha && job.prTitle !== undefined && job.prAuthor) return job as ResolvedReviewJob;
  const [owner, repo] = job.repo.split("/");
  const pr = await fetchPullRequest(octokit, { owner, repo, prNumber: job.prNumber });
  return { ...job, headSha: pr.headSha, prTitle: String(pr.title).slice(0, 300), prAuthor: pr.author };
}
