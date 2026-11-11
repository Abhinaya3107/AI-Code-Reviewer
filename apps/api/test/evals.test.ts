import { describe, expect, it } from "vitest";
import { DATASET } from "../../../evals/dataset.js";
import { parseCase, scoreCase, summarize, toPatch } from "../../../evals/score.js";
import { parsePatch } from "../src/utils/diffParser.js";

describe("eval dataset integrity", () => {
  const parsed = DATASET.map(parseCase);

  it("has unique ids, enough cases, and both buggy and clean ones", () => {
    expect(new Set(DATASET.map((c) => c.id)).size).toBe(DATASET.length);
    expect(DATASET.length).toBeGreaterThanOrEqual(20);
    expect(parsed.filter((p) => p.bugs.length > 0).length).toBeGreaterThanOrEqual(12);
    expect(parsed.filter((p) => p.bugs.length === 0).length).toBeGreaterThanOrEqual(6);
  });

  it("never leaks the answer key: no markers remain in the code the reviewer sees", () => {
    for (const p of parsed) {
      expect(p.code).not.toContain("@@BUG");
      expect(p.code).not.toMatch(/\bBUG\b/);
    }
  });

  it("points every seeded bug at a real, commentable line of its file", () => {
    for (const p of parsed) {
      const { validLines } = parsePatch(toPatch(p.code));
      for (const b of p.bugs) {
        expect(validLines.has(b.line), `${p.id} line ${b.line}`).toBe(true);
        expect(p.code.split("\n")[b.line - 1].trim().length, `${p.id} line ${b.line} is blank`).toBeGreaterThan(0);
      }
    }
  });
});

describe("scoring", () => {
  const c = parseCase({ id: "x", title: "t", filename: "a.ts", code: "a\nb @@BUG:bug@@\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl @@BUG:security@@" });

  it("derives the bug lines from the markers", () => {
    expect(c.bugs).toEqual([{ line: 2, category: "bug" }, { line: 12, category: "security" }]);
    expect(c.code).toBe("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl");
  });

  it("counts comments within 2 lines as hits, others as false alarms, and bugs once", () => {
    const s = scoreCase(c, [
      { path: "a.ts", line: 3 }, // within tolerance of line 2
      { path: "a.ts", line: 2 }, // second comment on the same bug: still a true positive, bug found once
      { path: "a.ts", line: 6 }, // 4 lines from one bug and 6 from the other: matches nothing
      { path: "other.ts", line: 12 }, // right line, wrong file
    ]);
    expect(s).toMatchObject({ bugs: 2, found: 1, reported: 4, truePositives: 2, falsePositives: 2 });
  });

  it("summarises recall, precision and clean-file false alarms", () => {
    const clean = parseCase({ id: "c", title: "t", filename: "a.ts", code: "a\nb" });
    const sum = summarize([scoreCase(c, [{ path: "a.ts", line: 2 }]), scoreCase(clean, [{ path: "a.ts", line: 1 }]), scoreCase(clean, [])]);
    expect(sum).toMatchObject({ cases: 3, buggyCases: 1, cleanCases: 2, bugs: 2, found: 1, recall: 0.5, reported: 2, truePositives: 1, precision: 0.5, cleanCasesFlagged: 1, cleanFlaggedRate: 0.5 });
    expect(summarize([scoreCase(clean, [])]).precision).toBeNull(); // nothing reported: undefined, not 0
  });
});
