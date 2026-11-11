"""How a comment is rendered on GitHub, and how we recognise one we posted earlier."""

import re

from reviewer.review.schema import ReviewComment

_SEVERITY_LABEL = {
    "critical": "Critical",
    "warning": "Warning",
    "suggestion": "Suggestion",
    "nitpick": "Nitpick",
}


def format_comment(c: ReviewComment) -> str:
    head = f"**{_SEVERITY_LABEL[c.severity]} | {c.category.capitalize()}**"
    fix = f"\n\n```suggestion\n{c.suggested_fix}\n```" if c.suggested_fix else ""
    return f"{head}\n\n{c.message}{fix}"


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def comment_key(path: str, message: str) -> str:
    """Identity of a comment across commits. Line numbers are excluded because they shift between pushes."""
    return f"{path}|{_normalize(message)}"


def key_from_posted_body(path: str, body: str) -> str:
    """Recovers the key from a comment body that format_comment() posted earlier."""
    without_header = "\n".join(body.split("\n")[1:])
    return comment_key(path, without_header.split("```suggestion")[0])
