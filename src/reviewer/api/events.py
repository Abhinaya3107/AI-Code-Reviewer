"""Decides whether a GitHub webhook should start a review. Pure functions: no network, no queue."""

from dataclasses import dataclass
from typing import Any

from reviewer.queue.jobs import ReviewJob
from reviewer.review.commands import parse_command

_REVIEW_ACTIONS = {"opened", "synchronize", "reopened"}
# Only people with write-ish access may trigger a (paid) review by comment.
_TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass(frozen=True)
class QueuedReview:
    job_id: str  # same id => same job, which makes GitHub redeliveries harmless
    job: ReviewJob


def to_review_job(event: str | None, payload: dict[str, Any], delivery_id: str) -> QueuedReview | None:
    """Returns the review to queue, or None for everything we ignore."""
    installation = payload.get("installation") or {}
    repository = payload.get("repository")
    if not installation.get("id") or not repository:
        return None

    base = {
        "delivery_id": delivery_id,
        "installation_id": installation["id"],
        "account_login": (installation.get("account") or {}).get("login")
        or (repository.get("owner") or {}).get("login")
        or "unknown",
        "repo_id": repository["id"],
        "repo": repository["full_name"],
    }

    if event == "pull_request":
        pr = payload["pull_request"]
        if (
            payload.get("action") not in _REVIEW_ACTIONS
            or pr.get("draft")
            or (pr.get("user") or {}).get("type") == "Bot"
        ):
            return None
        return QueuedReview(
            job_id=f"{repository['id']}-{pr['number']}-{pr['head']['sha']}",
            job=ReviewJob(
                **base,
                trigger="pull_request",
                pr_number=pr["number"],
                pr_title=str(pr.get("title") or "")[:300],
                pr_author=(pr.get("user") or {}).get("login", "unknown"),
                head_sha=pr["head"]["sha"],
            ),
        )

    if event == "issue_comment":
        issue, comment = payload.get("issue") or {}, payload.get("comment") or {}
        if payload.get("action") != "created":
            return None
        if issue.get("pull_request") is None or issue.get("state") != "open":  # plain issues and closed PRs
            return None
        if (comment.get("user") or {}).get("type") == "Bot":  # never react to bots (including ourselves)
            return None
        if comment.get("author_association") not in _TRUSTED_ASSOCIATIONS:
            return None
        if parse_command(comment.get("body")) != "review":
            return None
        return QueuedReview(
            # One job per comment: each /review runs, but a redelivery of the same comment does not.
            job_id=f"{repository['id']}-{issue['number']}-cmd-{comment['id']}",
            job=ReviewJob(**base, trigger="command", pr_number=issue["number"], comment_id=comment["id"]),
        )

    return None
