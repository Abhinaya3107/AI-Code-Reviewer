import { describe, expect, it } from "vitest";
import { chunkByBudget } from "../src/utils/chunk.js";
import { parsePatch } from "../src/utils/diffParser.js";
import { isReviewable, selectFiles } from "../src/utils/fileFilter.js";

describe("isReviewable / selectFiles", () => {
  const f = (filename: string, patch: string | undefined = "@@ -1 +1 @@\n+x") => ({ filename, patch });

  it("skips lockfiles, build output, minified, images and patch-less files", () => {
    expect(isReviewable(f("package-lock.json"))).toBe(false);
    expect(isReviewable(f("apps/web/pnpm-lock.yaml"))).toBe(false);
    expect(isReviewable(f("dist/index.js"))).toBe(false);
    expect(isReviewable(f("packages/a/node_modules/x.js"))).toBe(false);
    expect(isReviewable(f("public/app.min.js"))).toBe(false);
    expect(isReviewable(f("logo.PNG"))).toBe(false);
    expect(isReviewable({ filename: "big.bin" })).toBe(false); // no patch: binary or too large
    expect(isReviewable({ filename: "gone.ts", patch: "@@", status: "removed" })).toBe(false);
  });

  it("keeps normal source files", () => {
    expect(isReviewable(f("src/auth/login.ts"))).toBe(true);
    expect(isReviewable(f("builder/index.ts"))).toBe(true); // "build" must not match "builder"
  });

  it("orders source, then tests, then config, then docs", () => {
    const sorted = selectFiles([f("README.md"), f("package.json"), f("src/a.test.ts"), f("src/a.ts")]);
    expect(sorted.map((x) => x.filename)).toEqual(["src/a.ts", "src/a.test.ts", "package.json", "README.md"]);
  });
});

describe("parsePatch", () => {
  const patch = ["@@ -10,4 +10,5 @@ fn", " ctx1", "-old", "+new1", "+new2", " ctx2"].join("\n");

  it("maps added and context lines to new-file line numbers, skipping removed lines", () => {
    const { validLines } = parsePatch(patch);
    expect([...validLines].sort((a, b) => a - b)).toEqual([10, 11, 12, 13]);
  });

  it("annotates lines with their new-file numbers", () => {
    const { annotated } = parsePatch(patch);
    expect(annotated).toContain("   11 + new1");
    expect(annotated).toContain("   12 + new2");
    expect(annotated).toContain("      - old");
  });

  it("handles several hunks, blank context lines and a trailing newline", () => {
    const multi = ["@@ -1,2 +1,2 @@", " a", "+b", "@@ -50,2 +60,3 @@", " c", "", "+d", "\\ No newline at end of file", ""].join("\n");
    const { validLines } = parsePatch(multi);
    expect([...validLines].sort((a, b) => a - b)).toEqual([1, 2, 60, 61, 62]);
  });
});

describe("chunkByBudget", () => {
  it("splits when the budget is exceeded and truncates a single oversized item", () => {
    const items = [{ annotated: "a".repeat(60) }, { annotated: "b".repeat(60) }, { annotated: "c".repeat(500) }];
    const chunks = chunkByBudget(items, 100);
    expect(chunks.length).toBe(3);
    expect(chunks[2][0].annotated.length).toBeLessThan(200);
    expect(chunks[2][0].annotated).toContain("[diff truncated]");
  });
});
