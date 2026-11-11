from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

from reviewer.db.models import Base, Installation, Repository, Review, ReviewComment, ReviewStatus, UsageEvent
from reviewer.db.session import create_session_factory
from reviewer.db.store import SessionFactory, begin_review, fail_review, fingerprint_of, finish_review
from reviewer.queue.jobs import ResolvedJob
from reviewer.review.pipeline import ReviewOutcome, Usage
from reviewer.review.schema import ReviewComment as Comment

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def sf() -> AsyncIterator[SessionFactory]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


def job(**over: object) -> ResolvedJob:
    base: dict[str, object] = {
        "delivery_id": "d",
        "installation_id": 11,
        "account_login": "octo",
        "repo_id": 1_442_699_472,  # larger than 2**30: must not be truncated
        "repo": "octo/app",
        "pr_number": 7,
        "trigger": "pull_request",
        "head_sha": "abc123",
        "pr_title": "Add thing",
        "pr_author": "alice",
    }
    return ResolvedJob.model_validate({**base, **over})


def completed(**over: object) -> ReviewOutcome:
    comment = Comment(
        path="a.ts",
        line=3,
        severity="warning",
        category="bug",
        message="Missing await.",
        suggested_fix="await x();",
    )
    base: dict[str, object] = {
        "status": "completed",
        "summary": "s",
        "risk": "medium",
        "model": "gemini-2.5-flash",
        "files_reviewed": 2,
        "usage": Usage(1000, 200),
        "comments": [comment],
    }
    return ReviewOutcome(**{**base, **over})  # type: ignore[arg-type]


async def all_rows(sf: SessionFactory, model: type) -> list:  # type: ignore[type-arg]
    async with sf() as s:
        return list((await s.scalars(select(model))).all())


class TestBeginReview:
    async def test_creates_the_whole_chain_and_returns_ids(self, sf: SessionFactory) -> None:
        began = await begin_review(sf, job())
        assert began.repository_enabled is True
        (repo,) = await all_rows(sf, Repository)
        assert repo.github_repo_id == 1_442_699_472 and repo.full_name == "octo/app"
        (review,) = await all_rows(sf, Review)
        assert (review.status, review.attempts, review.head_sha) == (ReviewStatus.running, 1, "abc123")
        assert review.started_at is not None

    async def test_a_retry_reuses_the_same_rows_and_counts_attempts(self, sf: SessionFactory) -> None:
        first = await begin_review(sf, job())
        second = await begin_review(sf, job(pr_title="Renamed"))
        assert first.review_id == second.review_id
        assert len(await all_rows(sf, Installation)) == 1
        assert len(await all_rows(sf, Repository)) == 1
        (review,) = await all_rows(sf, Review)
        assert review.attempts == 2

    async def test_a_new_commit_on_the_same_pr_gets_its_own_review(self, sf: SessionFactory) -> None:
        await begin_review(sf, job(head_sha="aaa"))
        await begin_review(sf, job(head_sha="bbb"))
        assert len(await all_rows(sf, Review)) == 2

    async def test_a_disabled_repository_is_reported(self, sf: SessionFactory) -> None:
        await begin_review(sf, job())
        async with sf() as s, s.begin():
            (repo,) = (await s.scalars(select(Repository))).all()
            repo.enabled = False
        assert (await begin_review(sf, job(head_sha="new"))).repository_enabled is False


class TestFinishReview:
    async def test_stores_tokens_cost_comments_and_a_monthly_usage_event(self, sf: SessionFactory) -> None:
        began = await begin_review(sf, job())
        await finish_review(sf, began, completed(), 1234)

        (review,) = await all_rows(sf, Review)
        assert review.status == ReviewStatus.completed and review.overall_risk == "medium"
        assert (review.input_tokens, review.output_tokens, review.duration_ms) == (1000, 200, 1234)
        assert float(review.cost_usd or 0) == pytest.approx(0.0008)
        (comment,) = await all_rows(sf, ReviewComment)
        assert (
            comment.fingerprint == fingerprint_of("a.ts", "Missing await.")
            and comment.suggested_fix == "await x();"
        )
        (usage,) = await all_rows(sf, UsageEvent)
        assert usage.tokens == 1200 and len(usage.month) == 7

    async def test_rerunning_replaces_comments_and_usage_instead_of_duplicating(
        self, sf: SessionFactory
    ) -> None:
        began = await begin_review(sf, job())
        await finish_review(sf, began, completed(), 1)
        await finish_review(sf, began, completed(), 1)
        assert len(await all_rows(sf, ReviewComment)) == 1
        assert len(await all_rows(sf, UsageEvent)) == 1

    async def test_append_mode_keeps_earlier_comments_and_usage(self, sf: SessionFactory) -> None:
        began = await begin_review(sf, job())
        await finish_review(sf, began, completed(), 1)
        other = Comment(path="b.ts", line=1, severity="critical", category="security", message="New issue.")
        await finish_review(sf, began, completed(comments=[other]), 1, append=True)
        assert len(await all_rows(sf, ReviewComment)) == 2
        assert len(await all_rows(sf, UsageEvent)) == 2

    async def test_skipped_reviews_store_the_reason_and_nothing_else(self, sf: SessionFactory) -> None:
        began = await begin_review(sf, job())
        await finish_review(sf, began, ReviewOutcome(status="skipped", reason="draft"), 5)
        (review,) = await all_rows(sf, Review)
        assert (review.status, review.skip_reason) == (ReviewStatus.skipped, "draft")
        assert await all_rows(sf, ReviewComment) == [] and await all_rows(sf, UsageEvent) == []

    async def test_no_usage_event_when_the_provider_reported_no_tokens(self, sf: SessionFactory) -> None:
        began = await begin_review(sf, job())
        await finish_review(sf, began, completed(usage=Usage(0, 0), model="fake-llm"), 1)
        assert await all_rows(sf, UsageEvent) == []
        (review,) = await all_rows(sf, Review)
        assert review.cost_usd is None  # unknown model: no price

    async def test_a_success_after_a_failed_attempt_clears_the_old_error(self, sf: SessionFactory) -> None:
        began = await begin_review(sf, job())
        await fail_review(sf, began.review_id, RuntimeError("boom"), final=False)
        await finish_review(sf, began, completed(), 1)
        (review,) = await all_rows(sf, Review)
        assert review.error_message is None


class TestFailReview:
    async def test_marks_failed_only_on_the_final_attempt(self, sf: SessionFactory) -> None:
        began = await begin_review(sf, job())
        await fail_review(sf, began.review_id, RuntimeError("boom"), final=False)
        (review,) = await all_rows(sf, Review)
        assert (review.status, review.error_message, review.finished_at) == (
            ReviewStatus.queued,
            "boom",
            None,
        )
        await fail_review(sf, began.review_id, "still boom", final=True)
        (review,) = await all_rows(sf, Review)
        assert review.status == ReviewStatus.failed and review.finished_at is not None


class TestMigration:
    def test_the_migration_creates_exactly_the_schema_the_models_describe(self, tmp_path: Path) -> None:
        url = f"sqlite:///{tmp_path / 'm.db'}"
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")

        insp = inspect(create_engine(url))
        for table in Base.metadata.sorted_tables:
            assert insp.has_table(table.name), table.name
            migrated = {c["name"]: c for c in insp.get_columns(table.name)}
            assert set(migrated) == {c.name for c in table.columns}, table.name
            for col in table.columns:
                assert migrated[col.name]["nullable"] == col.nullable, f"{table.name}.{col.name}"
            model_indexes = {i.name for i in table.indexes}
            assert {i["name"] for i in insp.get_indexes(table.name)} >= model_indexes, table.name
            model_uniques = {
                frozenset(c.name for c in u.columns)
                for u in table.constraints
                if u.__class__.__name__ == "UniqueConstraint"
            }
            db_uniques = {frozenset(u["column_names"]) for u in insp.get_unique_constraints(table.name)}
            assert db_uniques == model_uniques, table.name

        command.downgrade(cfg, "base")
        assert insp.clear_cache() is None
        assert set(inspect(create_engine(url)).get_table_names()) <= {"alembic_version"}
