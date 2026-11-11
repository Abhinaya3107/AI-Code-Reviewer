import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from reviewer.review.schema import SEVERITIES, ReviewComment


@dataclass(frozen=True)
class ValidateOutcome:
    kept: list[ReviewComment]
    dropped_below_threshold: int
    dropped_invalid_line: int
    dropped_duplicate: int
    dropped_over_cap: int


def fingerprint(c: ReviewComment) -> str:
    return hashlib.sha256(f"{c.path}:{c.line}:{c.message}".encode()).hexdigest()[:16]


def validate_comments(
    comments: list[ReviewComment],
    valid_lines_by_path: Mapping[str, set[int] | frozenset[int]],
    *,
    max_comments: int,
    min_severity: str = "nitpick",
) -> ValidateOutcome:
    """Keeps only comments GitHub will accept: the path must be a reviewed file and the line must be inside
    the diff. Also drops comments below the severity threshold and duplicates, ranks by severity and caps."""
    limit = SEVERITIES.index(min_severity)
    below = invalid = duplicate = 0
    seen: set[str] = set()
    valid: list[ReviewComment] = []

    for c in comments:
        if SEVERITIES.index(c.severity) > limit:
            below += 1
            continue
        if c.line not in valid_lines_by_path.get(c.path, frozenset()):
            invalid += 1
            continue
        fp = fingerprint(c)
        if fp in seen:
            duplicate += 1
            continue
        seen.add(fp)
        valid.append(c)

    valid.sort(key=lambda c: SEVERITIES.index(c.severity))  # stable: keeps LLM order within a severity
    kept = valid[:max_comments]
    return ValidateOutcome(
        kept=kept,
        dropped_below_threshold=below,
        dropped_invalid_line=invalid,
        dropped_duplicate=duplicate,
        dropped_over_cap=len(valid) - len(kept),
    )
