/**
 * A minimal fake GitHub so the real review pipeline can run on a synthetic PR. Only the requests the
 * pipeline makes are answered. Nothing here touches the network.
 */
export function fakeGithubForCase(opts: { title: string; filename: string; patch: string }) {
  let posted: { body: string; comments: { path: string; line: number; body: string }[] } | undefined;
  const octokit = {
    request: async (route: string, params: Record<string, unknown>) => {
      if (route === "GET /repos/{owner}/{repo}/pulls/{pull_number}")
        return { data: { title: opts.title, body: "", draft: false, head: { sha: "eval" }, base: { ref: "main" }, user: { login: "eval" } } };
      if (route === "GET /repos/{owner}/{repo}/contents/{path}") throw Object.assign(new Error("Not Found"), { status: 404 });
      if (route === "GET /repos/{owner}/{repo}/pulls/{pull_number}/files")
        return { data: params.page === 1 ? [{ filename: opts.filename, status: "added", patch: opts.patch }] : [] };
      if (route === "GET /repos/{owner}/{repo}/pulls/{pull_number}/comments") return { data: [] };
      if (route === "POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews") {
        posted = { body: params.body as string, comments: params.comments as never };
        return { data: {} };
      }
      throw new Error(`eval fake GitHub: unexpected request ${route}`);
    },
  };
  return { octokit: octokit as never, getPosted: () => posted };
}
