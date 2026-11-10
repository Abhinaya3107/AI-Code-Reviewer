import crypto from "node:crypto";
import { SEVERITIES, type ReviewComment } from "../services/reviewSchema.js";

export interface ValidateOptions {
  maxComments: number;
  /** Lowest severity to keep (e.g. "warning" drops suggestions and nitpicks). Default: keep all. */
  minSeverity?: ReviewComment["severity"];
}

export interface ValidateOutcome {
  kept: ReviewComment[];
  droppedBelowThreshold: number;
  droppedInvalidLine: number;
  droppedDuplicate: number;
  droppedOverCap: number;
}

export const fingerprint = (c: Pick<ReviewComment, "path" | "line" | "message">) =>
  crypto.createHash("sha256").update(`${c.path}:${c.line}:${c.message}`).digest("hex").slice(0, 16);

/**
 * Keeps only comments GitHub will accept: the path must be a reviewed file and the line must be
 * inside the diff. Removes duplicates, ranks by severity, and caps the total.
 */
export function validateComments(
  comments: ReviewComment[],
  validLinesByPath: Map<string, Set<number>>,
  { maxComments, minSeverity = "nitpick" }: ValidateOptions,
): ValidateOutcome {
  let droppedInvalidLine = 0;
  let droppedDuplicate = 0;
  let droppedBelowThreshold = 0;
  const seen = new Set<string>();
  const valid: ReviewComment[] = [];
  const limit = SEVERITIES.indexOf(minSeverity);

  for (const c of comments) {
    if (SEVERITIES.indexOf(c.severity) > limit) {
      droppedBelowThreshold++;
      continue;
    }
    if (!validLinesByPath.get(c.path)?.has(c.line)) {
      droppedInvalidLine++;
      continue;
    }
    const fp = fingerprint(c);
    if (seen.has(fp)) {
      droppedDuplicate++;
      continue;
    }
    seen.add(fp);
    valid.push(c);
  }

  valid.sort((a, b) => SEVERITIES.indexOf(a.severity) - SEVERITIES.indexOf(b.severity));
  const kept = valid.slice(0, maxComments);
  return { kept, droppedBelowThreshold, droppedInvalidLine, droppedDuplicate, droppedOverCap: valid.length - kept.length };
}
