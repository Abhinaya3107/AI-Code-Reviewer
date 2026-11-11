"""What the worker does for one job: look up the PR, review it, and record the result."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from reviewer.db.store import SessionFactory, begin_review, fail_review, finish_review
from reviewer.ports import GitHubPort, LlmClient, PrRef
from reviewer.queue.jobs import ResolvedJob, ReviewJob
from reviewer.queue.redis_queue import QueuedJob
from reviewer.review.pipeline import ReviewOutcome, review_pull_request

log = logging.getLogger(__name__)


@dataclass
class WorkerDeps:
    github: Callable[[int], GitHubPort]  # installation id -> an authenticated client
    llm: LlmClient
    sessions: SessionFactory | None  # None: review PRs but store nothing


async def resolve_job(gh: GitHubPort, job: ReviewJob, ref: PrRef) -> ResolvedJob:
    """Fills in the PR details a "/review" comment event does not carry. PR events already have them."""
    if job.head_sha and job.pr_title is not None and job.pr_author:
        return ResolvedJob.model_validate(job.model_dump())
    pr = await gh.get_pull_request(ref)
    return ResolvedJob.model_validate(
        {**job.model_dump(), "head_sha": pr.head_sha, "pr_title": pr.title[:300], "pr_author": pr.author}
    )


async def process_job(job: QueuedJob, deps: WorkerDeps, *, max_attempts: int) -> ReviewOutcome:
    data = ReviewJob.model_validate(job.payload)
    gh = deps.github(data.installation_id)
    ref = PrRef(data.owner, data.name, data.pr_number)
    started = time.monotonic()

    resolved = await resolve_job(gh, data, ref)
    if resolved.comment_id:  # tell the person who typed "/review" that it was picked up
        await gh.react(ref, resolved.comment_id, "eyes")

    began = await begin_review(deps.sessions, resolved) if deps.sessions else None
    try:
        if began and not began.repository_enabled:
            outcome = ReviewOutcome(status="skipped", reason="repository disabled")
        else:
            outcome = await review_pull_request(gh, deps.llm, ref)
    except Exception as err:
        if deps.sessions and began:
            try:
                await fail_review(deps.sessions, began.review_id, err, final=job.attempts >= max_attempts)
            except Exception:
                log.exception("could not record the failure")
        raise

    if deps.sessions and began:
        await finish_review(
            deps.sessions,
            began,
            outcome,
            int((time.monotonic() - started) * 1000),
            append=resolved.trigger == "command",
        )
    log.info(
        "review finished",
        extra={
            "repo": data.repo,
            "pr": data.pr_number,
            "trigger": resolved.trigger,
            "stored": deps.sessions is not None,
            "status": outcome.status,
            "reason": outcome.reason,
            "comments_posted": outcome.comments_posted,
            "input_tokens": outcome.usage.input_tokens,
            "output_tokens": outcome.usage.output_tokens,
        },
    )
    return outcome
