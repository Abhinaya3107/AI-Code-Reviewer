import { App } from "@octokit/app";
import { config } from "../config.js";
import { formatComment, keyFromPostedBody } from "../utils/commentFormat.js";
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
  return {
    title: res.data.title,
    body: res.data.body,
    draft: res.data.draft ?? false,
    headSha: res.data.head.sha,
    baseRef: res.data.base.ref,
  };
}

/** Reads a text file from the repo at a ref. Returns null if it does not exist. */
export async function fetchRepoFile(
  octokit: InstallationOctokit,
  { owner, repo }: PrRef,
  path: string,
  ref: string,
): Promise<string | null> {
  try {
    const res = await octokit.request("GET /repos/{owner}/{repo}/contents/{path}", { owner, repo, path, ref });
    const data = res.data;
    if (Array.isArray(data) || data.type !== "file" || !("content" in data)) return null;
    return Buffer.from(data.content, "base64").toString("utf8");
  } catch (err) {
    if ((err as { status?: number }).status === 404) return null;
    throw err;
  }
}

/** Keys of review comments this bot already posted on the PR, so a new push does not repeat them. */
export async function fetchPostedCommentKeys(octokit: InstallationOctokit, { owner, repo, prNumber }: PrRef) {
  const keys = new Set<string>();
  for (let page = 1; page <= 10; page++) {
    const res = await octokit.request("GET /repos/{owner}/{repo}/pulls/{pull_number}/comments", {
      owner,
      repo,
      pull_number: prNumber,
      per_page: 100,
      page,
    });
    for (const c of res.data) if (c.user?.type === "Bot") keys.add(keyFromPostedBody(c.path, c.body));
    if (res.data.length < 100) break;
  }
  return keys;
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
