"""In-memory stand-ins for GitHub and the LLM, so the pipeline can be tested without any network."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from reviewer.ports import LlmResponse, PrRef, PullRequest
from reviewer.review.filters import ChangedFile
from reviewer.review.schema import ReviewComment

REF = PrRef("o", "r", 7)


def added_file_patch(lines: list[str]) -> str:
    """The patch GitHub returns for a brand new file."""
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)


def file(name: str, lines: list[str], status: str = "added") -> ChangedFile:
    return ChangedFile(name, status, added_file_patch(lines))


@dataclass
class PostedReview:
    head_sha: str
    body: str
    comments: list[ReviewComment]


@dataclass
class FakeGitHub:
    files: list[ChangedFile]
    title: str = "Test PR"
    draft: bool = False
    config: str | None = None  # contents of .aireview.yml on the base branch
    existing_keys: set[str] = field(default_factory=set)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    posted: PostedReview | None = None

    async def get_pull_request(self, ref: PrRef) -> PullRequest:
        return PullRequest(self.title, "desc", self.draft, "abc123", "main", "alice")

    async def get_pull_files(self, ref: PrRef) -> list[ChangedFile]:
        return self.files

    async def get_file(self, ref: PrRef, path: str, git_ref: str) -> str | None:
        self.calls.append(("get_file", (path, git_ref)))
        return self.config

    async def get_posted_comment_keys(self, ref: PrRef) -> set[str]:
        return set(self.existing_keys)

    async def post_review(
        self, ref: PrRef, *, head_sha: str, body: str, comments: list[ReviewComment]
    ) -> None:
        self.posted = PostedReview(head_sha, body, comments)

    async def react(self, ref: PrRef, comment_id: int, content: Literal["eyes", "+1"] = "eyes") -> bool:
        self.calls.append(("react", (comment_id, content)))
        return True


class ScriptedLlm:
    """Returns pre-set replies in order (the last one repeats) and records every prompt it receives."""

    model = "scripted"

    def __init__(
        self,
        comments: list[dict[str, Any]] | None = None,
        *,
        risk: str = "low",
        replies: list[str] | None = None,
    ):
        default = json.dumps({"summary": "ok", "overall_risk": risk, "comments": comments or []})
        self._replies = replies or [default]
        self.seen: list[dict[str, str]] = []

    async def review(self, *, system: str, user: str) -> LlmResponse:
        self.seen.append({"system": system, "user": user})
        reply = self._replies[min(len(self.seen) - 1, len(self._replies) - 1)]
        return LlmResponse(text=reply, input_tokens=100, output_tokens=20)

    @property
    def all_user_text(self) -> str:
        return "\n".join(s["user"] for s in self.seen)
