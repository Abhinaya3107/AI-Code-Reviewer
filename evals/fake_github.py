"""A minimal fake GitHub so the real review pipeline can run on a synthetic PR. Nothing here touches the network."""

from typing import Literal

from reviewer.ports import PrRef, PullRequest
from reviewer.review.filters import ChangedFile
from reviewer.review.schema import ReviewComment


class EvalGitHub:
    def __init__(self, title: str, filename: str, patch: str) -> None:
        self._title = title
        self._file = ChangedFile(filename, "added", patch)

    async def get_pull_request(self, ref: PrRef) -> PullRequest:
        return PullRequest(self._title, "", False, "eval", "main", "eval")

    async def get_pull_files(self, ref: PrRef) -> list[ChangedFile]:
        return [self._file]

    async def get_file(self, ref: PrRef, path: str, git_ref: str) -> str | None:
        return None

    async def get_posted_comment_keys(self, ref: PrRef) -> set[str]:
        return set()

    async def post_review(
        self, ref: PrRef, *, head_sha: str, body: str, comments: list[ReviewComment]
    ) -> None:
        return None

    async def react(self, ref: PrRef, comment_id: int, content: Literal["eyes", "+1"] = "eyes") -> bool:
        return True
