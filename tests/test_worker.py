import asyncio
from collections.abc import AsyncIterator
from typing import Any

import fakeredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from reviewer.db.models import Base, Review, ReviewComment, ReviewStatus
from reviewer.db.session import create_session_factory
from reviewer.db.store import SessionFactory
from reviewer.ports import PrRef
from reviewer.queue.jobs import ReviewJob
from reviewer.queue.redis_queue import JobQueue, QueuedJob
from reviewer.review.pipeline import InvalidLlmOutput
from reviewer.worker.handler import WorkerDeps, process_job, resolve_job
from reviewer.worker.runner import run_worker
from tests.fakes import FakeGitHub, ScriptedLlm, file

JOB: dict[str, Any] = {
    "delivery_id": "d",
    "installation_id": 11,
    "account_login": "octo",
    "repo_id": 99,
    "repo": "octo/app",
    "pr_number": 7,
    "trigger": "pull_request",
    "head_sha": "abc123",
    "pr_title": "Add thing",
    "pr_author": "alice",
}
COMMENT = {
    "path": "src/a.ts",
    "line": 1,
    "severity": "warning",
    "category": "bug",
    "message": "Missing await.",
}


def queued(payload: dict[str, Any] | None = None, attempts: int = 1) -> QueuedJob:
    return QueuedJob(id="job-1", payload={**JOB, **(payload or {})}, attempts=attempts)


@pytest.fixture
async def sf() -> AsyncIterator[SessionFactory]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


def deps(gh: FakeGitHub, llm: ScriptedLlm, sf: SessionFactory | None) -> WorkerDeps:
    return WorkerDeps(github=lambda _installation_id: gh, llm=llm, sessions=sf)


async def rows(sf: SessionFactory, model: type) -> list:  # type: ignore[type-arg]
    async with sf() as s:
        return list((await s.scalars(select(model))).all())


class TestProcessJob:
    async def test_reviews_posts_and_stores_the_result(self, sf: SessionFactory) -> None:
        gh = FakeGitHub([file("src/a.ts", ["a", "b"])])
        out = await process_job(queued(), deps(gh, ScriptedLlm([COMMENT]), sf), max_attempts=3)

        assert out.status == "completed" and gh.posted is not None and len(gh.posted.comments) == 1
        (review,) = await rows(sf, Review)
        assert review.status == ReviewStatus.completed and review.input_tokens == 100
        assert len(await rows(sf, ReviewComment)) == 1

    async def test_works_without_a_database(self) -> None:
        gh = FakeGitHub([file("src/a.ts", ["a", "b"])])
        out = await process_job(queued(), deps(gh, ScriptedLlm([COMMENT]), None), max_attempts=3)
        assert out.status == "completed" and gh.posted is not None

    async def test_a_command_job_looks_up_the_pr_reacts_and_appends(self, sf: SessionFactory) -> None:
        gh = FakeGitHub([file("src/a.ts", ["a", "b"])])
        command = {
            "trigger": "command",
            "head_sha": None,
            "pr_title": None,
            "pr_author": None,
            "comment_id": 555,
        }
        await process_job(queued(command), deps(gh, ScriptedLlm([COMMENT]), sf), max_attempts=3)

        assert ("react", (555, "eyes")) in gh.calls
        (review,) = await rows(sf, Review)
        assert review.head_sha == "abc123"  # looked up from the PR, not carried by the comment event

    async def test_a_disabled_repository_is_skipped_without_calling_the_llm(self, sf: SessionFactory) -> None:
        from reviewer.db.models import Repository

        gh, llm = FakeGitHub([file("src/a.ts", ["a"])]), ScriptedLlm()
        await process_job(queued(), deps(gh, llm, sf), max_attempts=3)  # creates the rows
        async with sf() as s, s.begin():
            (repo,) = (await s.scalars(select(Repository))).all()
            repo.enabled = False

        llm2 = ScriptedLlm()
        out = await process_job(queued({"head_sha": "newsha"}), deps(gh, llm2, sf), max_attempts=3)
        assert (out.status, out.reason) == ("skipped", "repository disabled")
        assert llm2.seen == []

    async def test_a_failure_is_recorded_then_reraised_for_the_queue_to_retry(
        self, sf: SessionFactory
    ) -> None:
        gh = FakeGitHub([file("src/a.ts", ["a"])])
        bad = ScriptedLlm(replies=["not json"])
        with pytest.raises(InvalidLlmOutput):
            await process_job(queued(attempts=1), deps(gh, bad, sf), max_attempts=3)
        (review,) = await rows(sf, Review)
        assert review.status == ReviewStatus.queued and "invalid output" in (review.error_message or "")

        with pytest.raises(InvalidLlmOutput):
            await process_job(queued(attempts=3), deps(gh, bad, sf), max_attempts=3)
        (review,) = await rows(sf, Review)
        assert review.status == ReviewStatus.failed  # out of attempts


class TestResolveJob:
    async def test_looks_up_missing_pr_details_and_leaves_complete_jobs_alone(self) -> None:
        gh = FakeGitHub([])
        ref = PrRef("octo", "app", 7)
        cmd = ReviewJob.model_validate(
            {**JOB, "trigger": "command", "head_sha": None, "pr_title": None, "pr_author": None}
        )
        resolved = await resolve_job(gh, cmd, ref)
        assert (resolved.head_sha, resolved.pr_title, resolved.pr_author) == ("abc123", "Test PR", "alice")

        full = ReviewJob.model_validate(JOB)
        assert (await resolve_job(gh, full, ref)).head_sha == "abc123"


class TestRunner:
    async def test_runs_queued_jobs_and_shuts_down_cleanly(self) -> None:
        redis = fakeredis.FakeAsyncRedis(decode_responses=True)
        q = JobQueue(redis, base_delay_s=0)
        await q.enqueue("job-1", {"n": 1})
        await q.enqueue("job-2", {"n": 2})
        done: list[str] = []
        stop = asyncio.Event()

        async def handler(job: QueuedJob) -> None:
            done.append(job.id)
            if len(done) == 2:
                stop.set()

        await asyncio.wait_for(run_worker(q, handler, stop=stop, concurrency=2, poll_timeout_s=1), timeout=10)
        assert sorted(done) == ["job-1", "job-2"]
        assert await q.stats() == {"pending": 0, "processing": 0, "delayed": 0, "failed": 0}
        await redis.aclose()

    async def test_a_failing_job_is_retried_then_dead_lettered(self) -> None:
        redis = fakeredis.FakeAsyncRedis(decode_responses=True)
        q = JobQueue(redis, base_delay_s=0, max_attempts=3)
        await q.enqueue("job-1", {})
        attempts: list[int] = []
        stop = asyncio.Event()

        async def handler(job: QueuedJob) -> None:
            attempts.append(job.attempts)
            if len(attempts) == 3:
                asyncio.get_running_loop().call_later(0.2, stop.set)
            raise RuntimeError("boom")

        await asyncio.wait_for(run_worker(q, handler, stop=stop, concurrency=1, poll_timeout_s=1), timeout=15)
        assert attempts == [1, 2, 3]
        assert (await q.stats())["failed"] == 1
        await redis.aclose()


class TestRunnerQueueErrors:
    async def test_queue_errors_back_off_instead_of_hammering_and_recover(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            if seconds > 0:  # sleep(0) is the loop's own cooperative yield
                delays.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        stop = asyncio.Event()

        class UnreachableQueue:
            def __init__(self) -> None:
                self.calls = 0

            async def next_job(self, timeout_s: int) -> None:
                self.calls += 1
                if self.calls <= 4:
                    raise ConnectionError("Timeout reading from redis")
                stop.set()  # Redis is back and there is simply no work: the worker shuts down cleanly

        queue = UnreachableQueue()
        handler_calls: list[QueuedJob] = []

        async def handler(job: QueuedJob) -> None:
            handler_calls.append(job)

        await asyncio.wait_for(
            run_worker(queue, handler, stop=stop, concurrency=1, poll_timeout_s=1), timeout=5
        )  # type: ignore[arg-type]
        assert delays == [5.0, 10.0, 20.0, 40.0]  # doubles each time (capped at 60)
        assert handler_calls == []

    async def test_the_pause_resets_after_a_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        delays: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            if seconds > 0:
                delays.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        stop = asyncio.Event()
        script: list[object] = [ConnectionError("x"), ConnectionError("x"), None, ConnectionError("x")]

        class Scripted:
            async def next_job(self, timeout_s: int) -> None:
                item = script.pop(0) if script else None
                if not script:
                    stop.set()
                if isinstance(item, Exception):
                    raise item

        async def handler(job: QueuedJob) -> None:
            return None

        await asyncio.wait_for(
            run_worker(Scripted(), handler, stop=stop, concurrency=1, poll_timeout_s=1), timeout=5
        )  # type: ignore[arg-type]
        assert delays == [5.0, 10.0, 5.0]  # the third error starts again from 5s
