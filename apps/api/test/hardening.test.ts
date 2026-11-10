import { describe, expect, it } from "vitest";
import { parseRepoConfig, DEFAULT_CONFIG } from "../src/services/repoConfig.js";
import type { ReviewComment } from "../src/services/reviewSchema.js";
import { commentKey, formatComment, keyFromPostedBody } from "../src/utils/commentFormat.js";
import { globToRegex } from "../src/utils/glob.js";
import { redactPatch } from "../src/utils/redact.js";
import { validateComments } from "../src/utils/validateComments.js";

describe("redactPatch", () => {
  const patch = [
    "@@ -1,2 +1,5 @@",
    " const a = 1;",
    '+const API_SECRET = "sk_live_1234567890abcdef";',
    '+const key = "AIzaSyA1234567890abcdefghijklmnopqrstuv";',
    "+const ok = 2;",
  ].join("\n");

  it("masks secrets, keeps the line count, and reports added lines", () => {
    const r = redactPatch(patch);
    expect(r.patch.split("\n")).toHaveLength(patch.split("\n").length);
    expect(r.patch).not.toContain("1234567890abcdef");
    expect(r.patch).not.toContain("AIzaSy");
    expect(r.patch).toContain("const ok = 2;");
    expect(r.findings.map((f) => f.line)).toEqual([2, 3]);
  });

  it("masks a multi-line private key and reports it once", () => {
    const key = [
      "@@ -0,0 +1,5 @@",
      "+const k = 1;",
      "+-----BEGIN RSA PRIVATE KEY-----",
      "+MIIEpAIBAAKCAQEA1uNkWoQjatLiWR65OhROej56",
      "+-----END RSA PRIVATE KEY-----",
      "+const z = 2;",
    ].join("\n");
    const r = redactPatch(key);
    expect(r.patch).not.toContain("MIIEpA");
    expect(r.patch).toContain("const z = 2;");
    expect(r.findings).toEqual([{ line: 2, type: "private-key" }]);
  });

  it("does not flag secrets on context lines or leave normal code alone", () => {
    const r = redactPatch(["@@ -1,2 +1,2 @@", ' const token = "abcdefghijkl";', "+const tokenCount = 5;"].join("\n"));
    expect(r.findings).toEqual([]);
    expect(r.patch).toContain("tokenCount = 5");
  });
});

describe("globToRegex", () => {
  it("matches common ignore patterns", () => {
    expect(globToRegex("docs/**").test("docs/a/b.md")).toBe(true);
    expect(globToRegex("docs/**").test("src/docs.md")).toBe(false);
    expect(globToRegex("**/*.generated.ts").test("src/x/api.generated.ts")).toBe(true);
    expect(globToRegex("**/*.generated.ts").test("api.generated.ts")).toBe(true);
    expect(globToRegex("src/*.js").test("src/a/b.js")).toBe(false);
  });
});

describe("parseRepoConfig", () => {
  it("applies defaults and accepts valid settings", () => {
    const { config, error } = parseRepoConfig("severity_threshold: warning\nmax_comments: 5\nignore_paths:\n  - 'docs/**'\nfocus: [security]\n");
    expect(error).toBeUndefined();
    expect(config).toMatchObject({ enabled: true, severity_threshold: "warning", max_comments: 5, focus: ["security"] });
  });

  it("falls back to defaults with a reason when invalid", () => {
    const a = parseRepoConfig("severity_threshold: loud");
    expect(a.config).toEqual(DEFAULT_CONFIG);
    expect(a.error).toContain("severity_threshold");
    const b = parseRepoConfig("a: [unclosed");
    expect(b.config).toEqual(DEFAULT_CONFIG);
    expect(b.error).toContain("invalid YAML");
  });
});

describe("repeat-comment detection", () => {
  const c: ReviewComment = { path: "a.ts", line: 10, severity: "warning", category: "bug", message: "Missing await.", suggested_fix: "await x();" };

  it("recognises a previously posted comment even if its line moved", () => {
    const posted = keyFromPostedBody("a.ts", formatComment(c));
    expect(posted).toBe(commentKey("a.ts", "  missing   AWAIT. "));
    expect(commentKey(c.path, c.message)).toBe(posted);
  });
});

describe("severity threshold", () => {
  const mk = (severity: ReviewComment["severity"], line: number): ReviewComment => ({ path: "a.ts", line, severity, category: "bug", message: `m${line}` });
  it("drops comments below the threshold", () => {
    const valid = new Map([["a.ts", new Set([1, 2, 3, 4])]]);
    const r = validateComments([mk("critical", 1), mk("warning", 2), mk("suggestion", 3), mk("nitpick", 4)], valid, { maxComments: 10, minSeverity: "warning" });
    expect(r.kept.map((x) => x.severity)).toEqual(["critical", "warning"]);
    expect(r.droppedBelowThreshold).toBe(2);
  });
});
