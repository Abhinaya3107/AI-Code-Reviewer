export interface SecretFinding {
  /** New-file line number of the added line that contained the secret. */
  line: number;
  type: string;
}

const KEY_BLOCK_BEGIN = /-----BEGIN [A-Z ]*PRIVATE KEY-----/;
const KEY_BLOCK_END = /-----END [A-Z ]*PRIVATE KEY-----/;
const HUNK = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

const TOKEN_RULES: { type: string; re: RegExp }[] = [
  { type: "aws-access-key", re: /\bAKIA[0-9A-Z]{16}\b/g },
  { type: "github-token", re: /\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b/g },
  { type: "google-api-key", re: /\bAIza[0-9A-Za-z_-]{30,}/g },
  { type: "google-api-key", re: /\bAQ\.[A-Za-z0-9_-]{30,}/g },
  { type: "slack-token", re: /\bxox[abprs]-[A-Za-z0-9-]{10,}/g },
  { type: "stripe-key", re: /\b[sr]k_(?:live|test)_[0-9A-Za-z]{10,}/g },
  { type: "llm-api-key", re: /\bsk-[A-Za-z0-9_-]{20,}/g },
];

// name = "value" where the name suggests a credential. Group 2 is the value.
const ASSIGNMENT = /((?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key)["']?\s*[:=]\s*["'])([^"'\s]{8,})(["'])/gi;

function redactLine(text: string): { text: string; type?: string } {
  let type: string | undefined;
  let out = text;
  for (const rule of TOKEN_RULES) {
    out = out.replace(rule.re, () => {
      type ??= rule.type;
      return `[REDACTED:${rule.type}]`;
    });
  }
  out = out.replace(ASSIGNMENT, (_m, pre: string, value: string, post: string) => {
    if (value.startsWith("[REDACTED")) return `${pre}${value}${post}`;
    type ??= "hardcoded-credential";
    return `${pre}[REDACTED:hardcoded-credential]${post}`;
  });
  return { text: out, type };
}

/**
 * Masks secrets in a unified-diff patch before it is sent to an LLM. The number of lines never
 * changes, so line numbers stay valid. Findings are reported only for ADDED lines.
 */
export function redactPatch(patch: string): { patch: string; findings: SecretFinding[] } {
  const findings: SecretFinding[] = [];
  let newLine = 0;
  let inKeyBlock = false;

  const out = patch.split("\n").map((raw) => {
    const hunk = HUNK.exec(raw);
    if (hunk) {
      newLine = parseInt(hunk[1], 10);
      return raw;
    }
    const marker = raw[0];
    if (marker !== "+" && marker !== " " && marker !== "-") return raw;
    const body = raw.slice(1);
    const isAdded = marker === "+";
    const lineNo = newLine;
    if (marker !== "-") newLine++;

    let result = body;
    let type: string | undefined;

    if (inKeyBlock || KEY_BLOCK_BEGIN.test(body)) {
      type = "private-key";
      result = "[REDACTED:private-key]";
      if (!inKeyBlock && !KEY_BLOCK_END.test(body)) inKeyBlock = true; // block continues on next lines
      else if (KEY_BLOCK_END.test(body)) inKeyBlock = false;
      // Report once, on the line where the block starts.
      if (isAdded && !KEY_BLOCK_BEGIN.test(body)) type = undefined;
    } else {
      const r = redactLine(body);
      result = r.text;
      type = r.type;
    }

    if (isAdded && type) findings.push({ line: lineNo, type });
    return marker + result;
  });

  return { patch: out.join("\n"), findings };
}
