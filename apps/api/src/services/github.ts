import { App } from "@octokit/app";
import { config } from "../config.js";

export const githubApp = new App({
  appId: config.GITHUB_APP_ID,
  privateKey: config.GITHUB_PRIVATE_KEY.replace(/\\n/g, "\n"),
  webhooks: { secret: config.GITHUB_WEBHOOK_SECRET },
});

/** Octokit authenticated as the installation (token caching/refresh handled by the library). */
export const getInstallationOctokit = (installationId: number) =>
  githubApp.getInstallationOctokit(installationId);
