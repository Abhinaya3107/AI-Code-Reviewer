"""Masks secrets in a diff before it is sent to a third-party LLM."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretFinding:
    line: int  # new-file line number of the ADDED line that contained the secret
    type: str


_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_TOKEN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("google-api-key", re.compile(r"\bAQ\.[A-Za-z0-9_-]{30,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{10,}")),
    ("llm-api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
)

# name = "value", where the name suggests a credential. Group 2 is the value.
_ASSIGNMENT = re.compile(
    r"""((?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key)["']?\s*[:=]\s*["'])([^"'\s]{8,})(["'])""",
    re.IGNORECASE,
)


def _redact_line(text: str) -> tuple[str, str | None]:
    found: list[str] = []  # first matching rule wins

    for kind, pattern in _TOKEN_RULES:

        def mask(_m: re.Match[str], kind: str = kind) -> str:
            found.append(kind)
            return f"[REDACTED:{kind}]"

        text = pattern.sub(mask, text)

    def mask_assignment(m: re.Match[str]) -> str:
        pre, value, post = m.group(1), m.group(2), m.group(3)
        if value.startswith("[REDACTED"):
            return f"{pre}{value}{post}"
        found.append("hardcoded-credential")
        return f"{pre}[REDACTED:hardcoded-credential]{post}"

    text = _ASSIGNMENT.sub(mask_assignment, text)
    return text, (found[0] if found else None)


def redact_patch(patch: str) -> tuple[str, list[SecretFinding]]:
    """Returns the patch with secrets masked, plus findings for ADDED lines.

    The number of lines never changes, so diff line numbers stay valid.
    """
    findings: list[SecretFinding] = []
    out: list[str] = []
    new_line = 0
    in_key_block = False

    for raw in patch.split("\n"):
        hunk = _HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            out.append(raw)
            continue

        marker = raw[:1]
        if marker not in ("+", " ", "-"):
            out.append(raw)
            continue

        body = raw[1:]
        is_added = marker == "+"
        line_no = new_line
        if marker != "-":
            new_line += 1

        kind: str | None
        if in_key_block or _KEY_BEGIN.search(body):
            kind = "private-key"
            result = "[REDACTED:private-key]"
            if not in_key_block and not _KEY_END.search(body):
                in_key_block = True  # the block continues on the following lines
            elif _KEY_END.search(body):
                in_key_block = False
            if is_added and not _KEY_BEGIN.search(body):
                kind = None  # report once, on the line where the block starts
        else:
            result, kind = _redact_line(body)

        if is_added and kind:
            findings.append(SecretFinding(line=line_no, type=kind))
        out.append(marker + result)

    return "\n".join(out), findings
