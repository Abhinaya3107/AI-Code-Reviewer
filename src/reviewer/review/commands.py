import re
from typing import Literal

Command = Literal["review"]

_REVIEW = re.compile(r"^/review\b(?![-\w])", re.IGNORECASE)


def parse_command(body: str | None) -> Command | None:
    """Reads a slash command from a PR comment.

    Only the FIRST non-empty line counts, so a quoted reply ("> /review") or a command buried in a
    paragraph does nothing, and "/reviewer" is not "/review".
    """
    first = next((line for line in (body or "").split("\n") if line.strip()), None)
    if first is None:
        return None
    return "review" if _REVIEW.match(first.strip()) else None
