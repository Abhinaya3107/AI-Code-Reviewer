from collections.abc import Sequence
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class PromptFile:
    filename: str
    annotated: str


def chunk_by_budget(items: Sequence[PromptFile], max_chars: int) -> list[list[PromptFile]]:
    """Groups files into chunks whose combined text stays under max_chars (about 4 chars per token).

    A file that alone exceeds the budget is truncated so it still fits in a chunk of its own.
    """
    chunks: list[list[PromptFile]] = []
    current: list[PromptFile] = []
    size = 0

    for item in items:
        fitted = (
            replace(item, annotated=item.annotated[:max_chars] + "\n[diff truncated]")
            if len(item.annotated) > max_chars
            else item
        )
        if size + len(fitted.annotated) > max_chars and current:
            chunks.append(current)
            current, size = [], 0
        current.append(fitted)
        size += len(fitted.annotated)

    if current:
        chunks.append(current)
    return chunks
