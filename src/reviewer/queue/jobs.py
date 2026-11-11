from typing import Literal

from pydantic import BaseModel


class ReviewJob(BaseModel):
    """What the webhook hands to the worker. Small and JSON-serialisable: it lives in Redis."""

    delivery_id: str
    installation_id: int
    account_login: str
    repo_id: int
    repo: str  # "owner/name"
    pr_number: int
    # What caused this job: a PR event, or someone commenting "/review".
    trigger: Literal["pull_request", "command"]
    # Known for pull_request events. A comment event does not carry them, so the worker looks them up.
    head_sha: str | None = None
    pr_title: str | None = None
    pr_author: str | None = None
    # The "/review" comment, so the worker can acknowledge it.
    comment_id: int | None = None

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1]


class ResolvedJob(ReviewJob):
    """A job whose PR details are all known."""

    head_sha: str
    pr_title: str
    pr_author: str
