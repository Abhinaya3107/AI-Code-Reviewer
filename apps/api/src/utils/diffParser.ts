export interface ParsedPatch {
  /** New-file line numbers GitHub allows inline comments on (added + context lines, RIGHT side). */
  validLines: Set<number>;
  /** The patch with new-file line numbers shown, so the LLM can cite exact lines. */
  annotated: string;
}

const HUNK = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/**
 * Parses a unified-diff patch as returned by GET /pulls/{n}/files.
 * Output format of `annotated`:
 *    "   42 + added line"    (added, commentable)
 *    "   43   context line"  (unchanged, commentable)
 *    "      - removed line"  (deleted, not commentable)
 */
export function parsePatch(patch: string): ParsedPatch {
  const validLines = new Set<number>();
  const out: string[] = [];
  let newLine = 0;
  let inHunk = false;

  const lines = patch.split("\n");
  if (lines[lines.length - 1] === "") lines.pop(); // trailing newline, not a context line

  for (const raw of lines) {
    const hunk = HUNK.exec(raw);
    if (hunk) {
      newLine = parseInt(hunk[1], 10);
      inHunk = true;
      out.push(raw);
      continue;
    }
    if (!inHunk) continue;

    const marker = raw[0];
    const text = raw.slice(1);
    if (marker === "+") {
      validLines.add(newLine);
      out.push(`${String(newLine).padStart(5)} + ${text}`);
      newLine++;
    } else if (marker === "-") {
      out.push(`${" ".repeat(5)} - ${text}`);
    } else if (marker === " " || raw === "") {
      // An empty raw line inside a hunk is a blank context line whose leading space was trimmed.
      validLines.add(newLine);
      out.push(`${String(newLine).padStart(5)}   ${text}`);
      newLine++;
    }
    // "\ No newline at end of file" and anything else: ignored
  }

  return { validLines, annotated: out.join("\n") };
}
