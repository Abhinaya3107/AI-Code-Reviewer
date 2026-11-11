"""Decides which changed files are worth sending to the reviewer, and in what order."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    status: str | None = None
    patch: str | None = None


_IGNORE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, flags)
    for p, flags in [
        # lockfiles
        (
            r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|uv\.lock"
            r"|Pipfile\.lock|Gemfile\.lock|composer\.lock|go\.sum)$",
            0,
        ),
        # minified, source maps, generated code, snapshots
        (r"\.min\.(js|css)$", 0),
        (r"\.map$", 0),
        (r"\.generated\.", 0),
        (r"\.snap$", 0),
        # build output, vendored and cache folders, anywhere in the path
        (r"(^|/)(dist|build|node_modules|vendor|coverage|\.next|__pycache__|\.venv|venv)/", 0),
        # binary-ish assets
        (r"\.(png|jpe?g|gif|svg|ico|webp|pdf|woff2?|ttf|eot|zip|gz|pyc)$", re.IGNORECASE),
    ]
)


def is_reviewable(file: ChangedFile, extra_ignore: Iterable[re.Pattern[str]] = ()) -> bool:
    """A file is reviewable if it has a text patch (binary or huge files have none) and is not ignored."""
    if not file.patch or file.status == "removed":
        return False
    return not any(p.search(file.filename) for p in (*_IGNORE, *extra_ignore))


def priority(filename: str) -> int:
    """Lower is reviewed first: source, then tests, then config, then docs."""
    if re.search(r"\.(md|mdx|txt|rst)$", filename, re.I) or re.search(r"(^|/)docs?/", filename):
        return 3
    if re.search(r"\.(json|ya?ml|toml|ini|env|lock)$", filename, re.I) or re.search(
        r"(^|/)\.[^/]+$", filename
    ):
        return 2
    if (
        re.search(r"(\.|_)(test|spec)\.[a-z]+$", filename, re.I)
        or re.search(r"(^|/)test_[^/]+\.py$", filename)
        or re.search(r"(^|/)(tests?|__tests__)/", filename)
    ):
        return 1
    return 0


def select_files(
    files: Sequence[ChangedFile], extra_ignore: Iterable[re.Pattern[str]] = ()
) -> list[ChangedFile]:
    extra = tuple(extra_ignore)
    return sorted((f for f in files if is_reviewable(f, extra)), key=lambda f: priority(f.filename))
