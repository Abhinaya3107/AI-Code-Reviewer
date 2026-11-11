"""The two things the review pipeline depends on, as interfaces. Real implementations talk to GitHub and
an LLM over HTTP; tests swap in in-memory fakes."""

from dataclasses import dataclass
from typing import Literal, Protocol

from reviewer.review.filters import ChangedFile
from reviewer.review.schema import ReviewComment


@dataclass(frozen=True)
class PrRef:
    owner: str
    repo: str
    number: int


@dataclass(frozen=True)
class PullRequest:
    title: str
    body: str | None
    draft: bool
    head_sha: str
    base_ref: str
    author: str


class GitHubPort(Protocol):
    async def get_pull_request(self, ref: PrRef) -> PullRequest: ...

    async def get_pull_files(self, ref: PrRef) -> list[ChangedFile]: ...

    async def get_file(self, ref: PrRef, path: str, git_ref: str) -> str | None:
        """A text file from the repository at a ref, or None if it does not exist."""

    async def get_posted_comment_keys(self, ref: PrRef) -> set[str]:
        """Keys of review comments this bot already posted on the PR."""

    async def post_review(
        self, ref: PrRef, *, head_sha: str, body: str, comments: list[ReviewComment]
    ) -> None:
        """Posts ONE review (event COMMENT: the bot never approves or blocks a PR)."""

    async def react(self, ref: PrRef, comment_id: int, content: Literal["eyes", "+1"] = "eyes") -> bool: ...


@dataclass(frozen=True)
class LlmResponse:
    text: str  # raw JSON text returned by the model
    input_tokens: int | None = None
    output_tokens: int | None = None


class LlmClient(Protocol):
    model: str

    async def review(self, *, system: str, user: str) -> LlmResponse: ...
