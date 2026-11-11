"""Parses the unified-diff patch GitHub returns for each file."""

import re
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class ParsedPatch:
    # New-file line numbers GitHub allows inline comments on: added and context lines (RIGHT side).
    valid_lines: frozenset[int]
    # The patch with new-file line numbers shown, so the LLM can cite exact lines.
    annotated: str


def parse_patch(patch: str) -> ParsedPatch:
    """Annotated output format:

    "   42 + added line"      added, commentable
    "   43   context line"    unchanged, commentable
    "      - removed line"    deleted, not commentable
    """
    valid: set[int] = set()
    out: list[str] = []
    new_line = 0
    in_hunk = False

    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline, not a context line

    for raw in lines:
        hunk = _HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
            out.append(raw)
            continue
        if not in_hunk:
            continue

        marker, text = raw[:1], raw[1:]
        if marker == "+":
            valid.add(new_line)
            out.append(f"{new_line:>5} + {text}")
            new_line += 1
        elif marker == "-":
            out.append(f"{'':>5} - {text}")
        elif marker == " " or raw == "":
            # An empty line inside a hunk is a blank context line whose leading space was trimmed.
            valid.add(new_line)
            out.append(f"{new_line:>5}   {text}")
            new_line += 1
        # "\ No newline at end of file" and anything else: ignored

    return ParsedPatch(valid_lines=frozenset(valid), annotated="\n".join(out))
