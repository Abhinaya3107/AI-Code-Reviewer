import { describe, expect, it } from "vitest";
import { reviewResultSchema, type ReviewComment } from "../src/services/reviewSchema.js";
import { validateComments } from "../src/utils/validateComments.js";

const c = (over: Partial<ReviewComment> = {}): ReviewComment => ({
  path: "a.ts",
  line: 5,
  severity: "warning",
  category: "bug",
  message: "problem",
  ...over,
});
const valid = new Map([["a.ts", new Set([5, 6, 7])]]);

describe("validateComments", () => {
  it("drops comments on lines or files that are not in the diff", () => {
    const r = validateComments([c({ line: 99 }), c({ path: "other.ts" }), c()], valid, { maxComments: 10 });
    expect(r.kept).toHaveLength(1);
    expect(r.droppedInvalidLine).toBe(2);
  });

  it("removes duplicates", () => {
    const r = validateComments([c(), c()], valid, { maxComments: 10 });
    expect(r.kept).toHaveLength(1);
    expect(r.droppedDuplicate).toBe(1);
  });

  it("ranks by severity and caps the count, keeping the most severe", () => {
    const r = validateComments(
      [c({ line: 5, severity: "nitpick" }), c({ line: 6, severity: "critical" }), c({ line: 7, severity: "warning" })],
      valid,
      { maxComments: 2 },
    );
    expect(r.kept.map((x) => x.severity)).toEqual(["critical", "warning"]);
    expect(r.droppedOverCap).toBe(1);
  });
});

describe("reviewResultSchema", () => {
  it("accepts a valid result and rejects a bad severity", () => {
    const ok = { summary: "s", overall_risk: "low", comments: [c()] };
    expect(reviewResultSchema.safeParse(ok).success).toBe(true);
    const bad = { ...ok, comments: [{ ...c(), severity: "huge" }] };
    expect(reviewResultSchema.safeParse(bad).success).toBe(false);
  });
});
