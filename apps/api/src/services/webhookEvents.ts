import type { ReviewJob } from "../queue/reviewQueue.js";
import { parseCommand } from "../utils/command.js";

const REVIEW_ACTIONS = ["opened", "synchronize", "reopened"];
/** Only people with write-ish access may trigger a (paid) review by comment. */
const TRUSTED_ASSOCIATIONS = ["OWNER", "MEMBER", "COLLABORATOR"];

export interface QueuedJob {
  job: ReviewJob;
  /** Same id => same job. Makes GitHub redeliveries harmless. */
  jobId: string;
}

/**
 * Decides whether a GitHub webhook should start a review. Pure function: no network, no queue.
 * Returns null for everything we ignore.
 */
export function toReviewJob(event: string | undefined, payload: any, deliveryId: string): QueuedJob | null {
  const installationId = payload?.installation?.id;
  const repository = payload?.repository;
  if (!installationId || !repository) return null;

  const base = {
    deliveryId,
    installationId,
    accountLogin: payload.installation.account?.login ?? repository.owner?.login ?? "unknown",
    repoId: repository.id,
    repo: repository.full_name,
  };

  if (event === "pull_request") {
    const pr = payload.pull_request;
    if (!REVIEW_ACTIONS.includes(payload.action) || pr.draft || pr.user?.type === "Bot") return null;
    return {
      jobId: `${repository.id}-${pr.number}-${pr.head.sha}`,
      job: {
        ...base,
        trigger: "pull_request",
        prNumber: pr.number,
        prTitle: String(pr.title ?? "").slice(0, 300),
        prAuthor: pr.user?.login ?? "unknown",
        headSha: pr.head.sha,
      },
    };
  }

  if (event === "issue_comment") {
    const { issue, comment } = payload;
    if (payload.action !== "created") return null;
    if (!issue?.pull_request || issue.state !== "open") return null; // plain issues and closed PRs
    if (comment?.user?.type === "Bot") return null; // never react to bots (including ourselves)
    if (!TRUSTED_ASSOCIATIONS.includes(comment?.author_association)) return null;
    if (parseCommand(comment?.body) !== "review") return null;
    return {
      // One job per comment, so each /review runs, but a redelivery of the same comment does not.
      jobId: `${repository.id}-${issue.number}-cmd-${comment.id}`,
      job: { ...base, trigger: "command", prNumber: issue.number, commentId: comment.id },
    };
  }

  return null;
}
