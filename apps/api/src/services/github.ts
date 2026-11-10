import { App } from "@octokit/app";
import { config } from "../config.js";
import type { ChangedFile } from "../utils/fileFilter.js";
import type { ReviewComment } from "./reviewSchema.js";

export const githubApp = new App({
  appId: config.GITHUB_APP_ID,
  privateKey: config.GITHUB_PRIVATE_KEY.replace(/\\n/g, "\n"),
  webhooks: { secret: config.GITHUB_WEBHOOK_SECRET },
});

/** Octokit authenticated as the installation (token caching/refresh handled by the library). */
export const getInstallationOctokit = (installationId: number) =>
  githubApp.getInstallationOctokit(installationId);

export type InstallationOctokit = Awaited<ReturnType<typeof getInstallationOctokit>>;

export interface PrRef {
  owner: string;
  repo: string;
  prNumber: number;
}

export async function fetchPullRequest(octokit: InstallationOctokit, { owner, repo, prNumber }: PrRef) {
  const res = await octokit.request("GET /repos/{owner}/{repo}/pulls/{pull_number}", {
    owner,
    repo,
    pull_number: prNumber,
  });
  return { title: res.data.title, body: res.data.body, draft: res.data.draft ?? false, headSha: res.data.head.sha };
}

/** All changed files, following pagination (100 per page, GitHub caps a PR at 3000 files). */
export async function fetchPullFiles(octokit: InstallationOctokit, { owner, repo, prNumber }: PrRef) {
  const files: ChangedFile[] = [];
  for (let page = 1; page <= 30; page++) {
    const res = await octokit.request("GET /repos/{owner}/{repo}/pulls/{pull_number}/files", {
      owner,
      repo,
      pull_number: prNumber,
      per_page: 100,
      page,
    });
    for (const f of res.data) files.push({ filename: f.filename, status: f.status, patch: f.patch });
    if (res.data.length < 100) break;
  }
  return files;
}

const SEVERITY_LABEL = { critical: "Critical", warning: "Warning", suggestion: "Suggestion", nitpick: "Nitpick" } as const;

export function formatComment(c: ReviewComment): string {
  const head = `**${SEVERITY_LABEL[c.severity]} | ${c.category[0].toUpperCase()}${c.category.slice(1)}**`;
  const fix = c.suggested_fix ? `\n\n\`\`\`suggestion\n${c.suggested_fix}\n\`\`\`` : "";
  return `${head}\n\n${c.message}${fix}`;
}

/** One review = one notification. Event is always COMMENT: the bot never approves or blocks a PR. */
export async function postReview(
  octokit: InstallationOctokit,
  { owner, repo, prNumber }: PrRef,
  args: { headSha: string; body: string; comments: ReviewComment[] },
) {
  await octokit.request("POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews", {
    owner,
    repo,
    pull_number: prNumber,
    commit_id: args.headSha,
    event: "COMMENT",
    body: args.body,
    comments: args.comments.map((c) => ({ path: c.path, line: c.line, side: "RIGHT" as const, body: formatComment(c) })),
  });
}
