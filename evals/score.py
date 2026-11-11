"""Evaluation helpers.

A case is written as ordinary source code where each buggy line ends with a marker like "@@BUG:security@@".
The marker is stripped before the code is shown to the reviewer, so the answer key (which line is buggy) is
derived automatically and never leaks into the prompt.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

_MARKER = re.compile(r"\s*@@BUG:([a-z]+)@@\s*$")


@dataclass(frozen=True)
class EvalCase:
    id: str
    title: str  # used as the PR title in the prompt
    filename: str
    code: str  # source with markers still attached


@dataclass(frozen=True)
class Bug:
    line: int  # 1-based line in the file the reviewer sees
    category: str


@dataclass(frozen=True)
class ParsedCase:
    id: str
    title: str
    filename: str
    code: str  # markers removed: exactly what the reviewer is given
    bugs: tuple[Bug, ...]


def parse_case(case: EvalCase) -> ParsedCase:
    bugs: list[Bug] = []
    lines: list[str] = []
    for i, raw in enumerate(case.code.split("\n"), start=1):
        m = _MARKER.search(raw)
        if m:
            bugs.append(Bug(line=i, category=m.group(1)))
        lines.append(_MARKER.sub("", raw))
    return ParsedCase(case.id, case.title, case.filename, "\n".join(lines), tuple(bugs))


def to_patch(code: str) -> str:
    """The unified-diff patch GitHub would return for a brand new file."""
    lines = code.split("\n")
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)


@dataclass(frozen=True)
class Reported:
    path: str
    line: int


@dataclass(frozen=True)
class CaseScore:
    id: str
    bugs: int
    found: int  # seeded bugs that at least one comment pointed at
    reported: int
    true_positives: int  # comments that pointed at a seeded bug
    false_positives: int  # comments that pointed at nothing seeded


def score_case(case: ParsedCase, comments: Sequence[Reported], tolerance: int = 2) -> CaseScore:
    """A comment counts for a bug if it is on the same file and within `tolerance` lines of it."""

    def hits(c: Reported, b: Bug) -> bool:
        return c.path == case.filename and abs(c.line - b.line) <= tolerance

    true_positives = sum(1 for c in comments if any(hits(c, b) for b in case.bugs))
    found = sum(1 for b in case.bugs if any(hits(c, b) for c in comments))
    return CaseScore(
        case.id, len(case.bugs), found, len(comments), true_positives, len(comments) - true_positives
    )


@dataclass(frozen=True)
class Summary:
    cases: int
    buggy_cases: int
    clean_cases: int
    bugs: int
    found: int
    recall: float | None  # found / bugs
    reported: int
    true_positives: int
    precision: float | None  # true_positives / reported (None when nothing was reported)
    clean_cases_flagged: int  # clean cases where the reviewer still commented
    clean_flagged_rate: float | None


def _ratio(a: int, b: int) -> float | None:
    return None if b == 0 else a / b


def summarize(scores: Sequence[CaseScore]) -> Summary:
    clean = [s for s in scores if s.bugs == 0]
    flagged = sum(1 for s in clean if s.reported > 0)
    bugs, found = sum(s.bugs for s in scores), sum(s.found for s in scores)
    reported, tp = sum(s.reported for s in scores), sum(s.true_positives for s in scores)
    return Summary(
        cases=len(scores),
        buggy_cases=len(scores) - len(clean),
        clean_cases=len(clean),
        bugs=bugs,
        found=found,
        recall=_ratio(found, bugs),
        reported=reported,
        true_positives=tp,
        precision=_ratio(tp, reported),
        clean_cases_flagged=flagged,
        clean_flagged_rate=_ratio(flagged, len(clean)),
    )
