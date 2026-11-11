"""Saves review results. Each public function is one short transaction.

The database may be far away (a hosted Postgres can be ~0.5s per round trip), so each function uses as few
statements as it can, and inserts use INSERT ... ON CONFLICT (an upsert), which is also safe when two workers
race to create the same row.
"""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, delete, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from reviewer.cost import estimate_cost_usd
from reviewer.db.models import (
    Installation,
    PullRequest,
    Repository,
    Review,
    ReviewComment,
    ReviewStatus,
    UsageEvent,
)
from reviewer.queue.jobs import ResolvedJob
from reviewer.review.comments import comment_key
from reviewer.review.pipeline import ReviewOutcome

SessionFactory = async_sessionmaker[AsyncSession]


def fingerprint_of(path: str, message: str) -> str:
    """Stable id for "the same finding", used to recognise repeats across commits."""
    return hashlib.sha256(comment_key(path, message).encode()).hexdigest()[:32]


async def _upsert(
    session: AsyncSession,
    model: type[Any],
    values: dict[str, Any],
    *,
    conflict: list[str],
    update_values: dict[str, Any],
    returning: list[InstrumentedAttribute[Any]],
) -> Row[Any]:
    """INSERT ... ON CONFLICT DO UPDATE ... RETURNING, on PostgreSQL (production) or SQLite (tests)."""
    dialect = session.sync_session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    stmt: Any = insert(model).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=conflict, set_=update_values).returning(*returning)
    return (await session.execute(stmt)).one()


class BeganReview:
    def __init__(self, review_id: int, installation_id: int, repository_enabled: bool) -> None:
        self.review_id = review_id
        self.installation_id = installation_id  # internal id of the installation row
        self.repository_enabled = repository_enabled  # False when switched off for this repository


async def begin_review(sf: SessionFactory, job: ResolvedJob) -> BeganReview:
    """Records that a review is starting. Safe to call again on a retry: one row per (PR, commit)."""
    now = datetime.now(UTC)
    async with sf() as session, session.begin():
        inst = await _upsert(
            session,
            Installation,
            {"github_installation_id": job.installation_id, "account_login": job.account_login},
            conflict=["github_installation_id"],
            update_values={"account_login": job.account_login},
            returning=[Installation.id],
        )
        repo = await _upsert(
            session,
            Repository,
            {"github_repo_id": job.repo_id, "full_name": job.repo, "installation_id": inst.id},
            conflict=["github_repo_id"],
            update_values={"full_name": job.repo, "installation_id": inst.id},
            returning=[Repository.id, Repository.enabled],
        )
        pr = await _upsert(
            session,
            PullRequest,
            {
                "repository_id": repo.id,
                "number": job.pr_number,
                "title": job.pr_title,
                "author": job.pr_author,
            },
            conflict=["repository_id", "number"],
            update_values={"title": job.pr_title, "author": job.pr_author},
            returning=[PullRequest.id],
        )
        review = await _upsert(
            session,
            Review,
            {
                "pull_request_id": pr.id,
                "head_sha": job.head_sha,
                "status": ReviewStatus.running,
                "started_at": now,
                "attempts": 1,
            },
            conflict=["pull_request_id", "head_sha"],
            update_values={
                "status": ReviewStatus.running,
                "started_at": now,
                "error_message": None,
                "attempts": Review.attempts + 1,
            },
            returning=[Review.id],
        )
    return BeganReview(review_id=review.id, installation_id=inst.id, repository_enabled=bool(repo.enabled))


async def finish_review(
    sf: SessionFactory,
    began: BeganReview,
    outcome: ReviewOutcome,
    duration_ms: int,
    *,
    append: bool = False,
) -> None:
    """Stores the result atomically.

    append=True is for a manual "/review" re-run of the same commit: that run only posts NEW comments (earlier
    ones are skipped as repeats), so keep what is already stored and add to it instead of replacing it.
    """
    finished_at = datetime.now(UTC)
    async with sf() as session, session.begin():
        review = await session.get(Review, began.review_id)
        if review is None:
            raise LookupError(f"review {began.review_id} does not exist")
        review.duration_ms = duration_ms
        review.finished_at = finished_at
        review.error_message = None  # a success after an earlier failed attempt clears the old error

        if outcome.status == "skipped":
            review.status = ReviewStatus.skipped
            review.skip_reason = outcome.reason
            return

        tokens_in, tokens_out = outcome.usage.input_tokens, outcome.usage.output_tokens
        cost = estimate_cost_usd(outcome.model, tokens_in, tokens_out) if outcome.model else None
        review.status = ReviewStatus.completed
        review.summary = outcome.summary
        review.overall_risk = outcome.risk
        review.model = outcome.model
        review.input_tokens, review.output_tokens = tokens_in, tokens_out
        review.cost_usd = Decimal(str(round(cost, 6))) if cost is not None else None
        review.files_reviewed = outcome.files_reviewed

        if not append:
            await session.execute(delete(ReviewComment).where(ReviewComment.review_id == began.review_id))
            await session.execute(delete(UsageEvent).where(UsageEvent.review_id == began.review_id))
        session.add_all(
            ReviewComment(
                review_id=began.review_id,
                path=c.path,
                line=c.line,
                severity=c.severity,
                category=c.category,
                message=c.message,
                suggested_fix=c.suggested_fix,
                fingerprint=fingerprint_of(c.path, c.message),
            )
            for c in outcome.comments
        )
        if tokens_in + tokens_out > 0:
            session.add(
                UsageEvent(
                    installation_id=began.installation_id,
                    review_id=began.review_id,
                    month=finished_at.strftime("%Y-%m"),
                    tokens=tokens_in + tokens_out,
                    cost_usd=review.cost_usd,
                )
            )


async def fail_review(sf: SessionFactory, review_id: int, error: BaseException | str, *, final: bool) -> None:
    """`final` is true on the last attempt: only then is the review marked failed (otherwise it is queued again)."""
    message = str(error)[:500]
    values: dict[str, Any] = {
        "status": ReviewStatus.failed if final else ReviewStatus.queued,
        "error_message": message,
    }
    if final:
        values["finished_at"] = datetime.now(UTC)
    async with sf() as session, session.begin():
        await session.execute(update(Review).where(Review.id == review_id).values(**values))
