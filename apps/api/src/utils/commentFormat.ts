import type { ReviewComment } from "../services/reviewSchema.js";

const SEVERITY_LABEL = { critical: "Critical", warning: "Warning", suggestion: "Suggestion", nitpick: "Nitpick" } as const;

export function formatComment(c: ReviewComment): string {
  const head = `**${SEVERITY_LABEL[c.severity]} | ${c.category[0].toUpperCase()}${c.category.slice(1)}**`;
  const fix = c.suggested_fix ? `\n\n\`\`\`suggestion\n${c.suggested_fix}\n\`\`\`` : "";
  return `${head}\n\n${c.message}${fix}`;
}

const normalize = (s: string) => s.toLowerCase().replace(/\s+/g, " ").trim();

/** Identity of a comment across commits. Line numbers are excluded because they shift between pushes. */
export const commentKey = (path: string, message: string) => `${path}|${normalize(message)}`;

/** Recovers the key from a comment body that formatComment() posted earlier. */
export function keyFromPostedBody(path: string, body: string): string {
  const withoutHeader = body.split("\n").slice(1).join("\n");
  return commentKey(path, withoutHeader.split("```suggestion")[0]);
}
