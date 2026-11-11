/**
 * Evaluation helpers. A case is written as ordinary source code where each buggy line ends with a marker
 * like "@@BUG:security@@". The marker is stripped before the code is shown to the reviewer, so the answer
 * key (which line is buggy) is derived automatically and never leaks into the prompt.
 */

export interface EvalCase {
  id: string;
  /** Used as the PR title in the prompt. */
  title: string;
  filename: string;
  /** Source with markers still attached. */
  code: string;
}

export interface Bug {
  /** 1-based line in the file the reviewer sees. */
  line: number;
  category: string;
}

export interface ParsedCase {
  id: string;
  title: string;
  filename: string;
  /** Source with markers removed: exactly what the reviewer is given. */
  code: string;
  bugs: Bug[];
}

const MARKER = /\s*@@BUG:([a-z]+)@@\s*$/;

export function parseCase(c: EvalCase): ParsedCase {
  const bugs: Bug[] = [];
  const lines = c.code.split("\n").map((raw, i) => {
    const m = MARKER.exec(raw);
    if (m) bugs.push({ line: i + 1, category: m[1] });
    return raw.replace(MARKER, "");
  });
  return { id: c.id, title: c.title, filename: c.filename, code: lines.join("\n"), bugs };
}

/** The unified-diff patch GitHub would return for a brand new file. */
export function toPatch(code: string): string {
  const lines = code.split("\n");
  return `@@ -0,0 +1,${lines.length} @@\n` + lines.map((l) => `+${l}`).join("\n");
}

export interface Reported {
  path: string;
  line: number;
}

export interface CaseScore {
  id: string;
  bugs: number;
  /** Seeded bugs that at least one comment pointed at. */
  found: number;
  reported: number;
  /** Comments that pointed at a seeded bug. */
  truePositives: number;
  /** Comments that pointed at nothing seeded. */
  falsePositives: number;
}

/** A comment counts for a bug if it is on the same file and within `tolerance` lines of it. */
export function scoreCase(c: ParsedCase, comments: Reported[], tolerance = 2): CaseScore {
  const hits = (cm: Reported, b: Bug) => cm.path === c.filename && Math.abs(cm.line - b.line) <= tolerance;
  const truePositives = comments.filter((cm) => c.bugs.some((b) => hits(cm, b))).length;
  const found = c.bugs.filter((b) => comments.some((cm) => hits(cm, b))).length;
  return { id: c.id, bugs: c.bugs.length, found, reported: comments.length, truePositives, falsePositives: comments.length - truePositives };
}

export interface Summary {
  cases: number;
  buggyCases: number;
  cleanCases: number;
  bugs: number;
  found: number;
  /** found / bugs */
  recall: number | null;
  reported: number;
  truePositives: number;
  /** truePositives / reported (null when nothing was reported) */
  precision: number | null;
  /** Clean cases where the reviewer still commented. */
  cleanCasesFlagged: number;
  /** cleanCasesFlagged / cleanCases */
  cleanFlaggedRate: number | null;
}

const ratio = (a: number, b: number) => (b === 0 ? null : a / b);

export function summarize(scores: CaseScore[]): Summary {
  const sum = (f: (s: CaseScore) => number) => scores.reduce((t, s) => t + f(s), 0);
  const clean = scores.filter((s) => s.bugs === 0);
  const flagged = clean.filter((s) => s.reported > 0).length;
  const bugs = sum((s) => s.bugs);
  const found = sum((s) => s.found);
  const reported = sum((s) => s.reported);
  const truePositives = sum((s) => s.truePositives);
  return {
    cases: scores.length,
    buggyCases: scores.length - clean.length,
    cleanCases: clean.length,
    bugs,
    found,
    recall: ratio(found, bugs),
    reported,
    truePositives,
    precision: ratio(truePositives, reported),
    cleanCasesFlagged: flagged,
    cleanFlaggedRate: ratio(flagged, clean.length),
  };
}
