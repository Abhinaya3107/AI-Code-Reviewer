export type Command = "review";

/**
 * Reads a slash command from a PR comment. Only the FIRST non-empty line counts, so a quoted reply
 * ("> /review") or a command buried in a paragraph does nothing, and "/reviewer" is not "/review".
 */
export function parseCommand(body: string | null | undefined): Command | null {
  const first = (body ?? "").split("\n").find((l) => l.trim() !== "");
  if (!first) return null;
  return /^\/review\b(?![-\w])/i.test(first.trim()) ? "review" : null;
}
