import asyncio
import time

import fakeredis
import pytest

from reviewer.queue.redis_queue import POLL_TIMEOUT_S, JobQueue, create_redis


@pytest.fixture
async def redis() -> fakeredis.FakeAsyncRedis:
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


def make(redis: fakeredis.FakeAsyncRedis, **kw: float) -> JobQueue:
    return JobQueue(redis, base_delay_s=kw.pop("base_delay_s", 0.0), **kw)  # type: ignore[arg-type]


class TestEnqueue:
    async def test_the_same_job_id_is_only_enqueued_once(self, redis: fakeredis.FakeAsyncRedis) -> None:
        q = make(redis)
        assert await q.enqueue("job-1", {"a": 1}) is True
        assert await q.enqueue("job-1", {"a": 2}) is False  # a GitHub redelivery
        assert (await q.stats())["pending"] == 1

    async def test_a_finished_job_id_still_cannot_be_enqueued_again(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        q = make(redis)
        await q.enqueue("job-1", {})
        job = await q.next_job(timeout_s=1)
        assert job is not None
        await q.complete(job)
        assert await q.enqueue("job-1", {}) is False  # one review per commit, even after it completed


class TestEnqueueCrashSafety:
    async def test_an_enqueue_that_died_halfway_is_finished_by_the_next_attempt(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        q = make(redis)
        await redis.hset(
            "acr:job:job-1", "payload", '{"n": 1}'
        )  # created, but never queued: the process died
        assert await q.enqueue("job-1", {"n": 1}) is True
        job = await q.next_job(timeout_s=1)
        assert job is not None and job.id == "job-1"


class TestClaimAndComplete:
    async def test_jobs_run_in_fifo_order_with_their_payload(self, redis: fakeredis.FakeAsyncRedis) -> None:
        q = make(redis)
        await q.enqueue("first", {"n": 1})
        await q.enqueue("second", {"n": 2})
        a, b = await q.next_job(timeout_s=1), await q.next_job(timeout_s=1)
        assert a is not None and b is not None
        assert (a.id, a.payload, a.attempts) == ("first", {"n": 1}, 1)
        assert b.id == "second"

    async def test_claimed_jobs_wait_in_processing_until_completed(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        q = make(redis)
        await q.enqueue("job-1", {})
        job = await q.next_job(timeout_s=1)
        assert job is not None
        assert (await q.stats())["processing"] == 1
        await q.complete(job)
        assert await q.stats() == {"pending": 0, "processing": 0, "delayed": 0, "failed": 0}

    async def test_an_empty_queue_returns_none_after_the_timeout(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        assert await make(redis).next_job(timeout_s=1) is None


class TestRetries:
    async def test_a_failure_is_retried_with_a_higher_attempt_count(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        q = make(redis, max_attempts=3)
        await q.enqueue("job-1", {})
        first = await q.next_job(timeout_s=1)
        assert first is not None and first.attempts == 1
        assert await q.fail(first, "boom") is True
        second = await q.next_job(timeout_s=1)  # base delay 0: due immediately
        assert second is not None and second.id == "job-1" and second.attempts == 2

    async def test_a_retry_waits_for_its_backoff_delay(self, redis: fakeredis.FakeAsyncRedis) -> None:
        q = make(redis, base_delay_s=3600)
        await q.enqueue("job-1", {})
        job = await q.next_job(timeout_s=1)
        assert job is not None
        await q.fail(job, "boom")
        assert (await q.stats())["delayed"] == 1
        assert await q.next_job(timeout_s=1) is None  # not due for an hour

    async def test_backoff_doubles_each_attempt(self, redis: fakeredis.FakeAsyncRedis) -> None:
        q = make(redis, base_delay_s=10, max_attempts=4)
        await q.enqueue("job-1", {})
        gaps = []
        for _ in range(3):
            await redis.zadd("acr:delayed", {"job-1": 0})  # make any scheduled retry due, then re-read
            job = await q.next_job(timeout_s=1)
            assert job is not None
            before = time.time()
            await q.fail(job, "boom")
            score = (await redis.zrange("acr:delayed", 0, -1, withscores=True))[0][1]
            gaps.append(round(score - before))
        assert gaps == [10, 20, 40]

    async def test_a_job_out_of_attempts_goes_to_the_dead_letter_list(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        q = make(redis, max_attempts=2)
        await q.enqueue("job-1", {})
        for expected_retry in (True, False):
            job = await q.next_job(timeout_s=1)
            assert job is not None
            assert await q.fail(job, "boom") is expected_retry
        assert await q.stats() == {"pending": 0, "processing": 0, "delayed": 0, "failed": 1}
        assert await redis.hget("acr:job:job-1", "error") == "boom"
        assert await redis.hget("acr:job:job-1", "state") == "failed"


class TestCrashRecovery:
    async def test_a_job_held_by_a_crashed_worker_is_requeued_after_the_visibility_timeout(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        q = make(redis, visibility_timeout_s=60)
        await q.enqueue("job-1", {})
        job = await q.next_job(timeout_s=1)  # claimed, then the worker "crashes": never completed
        assert job is not None
        await redis.hset("acr:job:job-1", "claimed_at", time.time() - 120)
        again = await q.next_job(timeout_s=1)
        assert again is not None and again.id == "job-1" and again.attempts == 2

    async def test_a_job_that_is_still_within_the_timeout_is_left_alone(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        q = make(redis, visibility_timeout_s=60)
        await q.enqueue("job-1", {})
        assert await q.next_job(timeout_s=1) is not None
        assert await q.next_job(timeout_s=1) is None  # another worker must not steal it
        assert (await q.stats())["processing"] == 1

    async def test_an_expired_job_record_is_dropped(self, redis: fakeredis.FakeAsyncRedis) -> None:
        q = make(redis)
        await q.enqueue("job-1", {})
        await redis.delete("acr:job:job-1")
        assert await q.next_job(timeout_s=1) is None
        assert (await q.stats())["processing"] == 0

    async def test_only_one_of_two_competing_workers_gets_a_job(
        self, redis: fakeredis.FakeAsyncRedis
    ) -> None:
        q = make(redis)
        await q.enqueue("job-1", {})
        results = await asyncio.gather(q.next_job(timeout_s=1), q.next_job(timeout_s=1))
        assert sorted(r is not None for r in results) == [False, True]


class TestCreateRedis:
    def test_the_socket_timeout_outlasts_the_longest_blocking_wait(self) -> None:
        # redis-py 8 defaults to a ~5s read timeout, which killed every 30s blocking pop against a real server.
        kwargs = create_redis("redis://localhost:6379").connection_pool.connection_kwargs
        timeouts = [v for k, v in kwargs.items() if k.endswith("socket_timeout") and v is not None]
        assert timeouts, "an explicit socket timeout must be set"
        assert all(timeout > POLL_TIMEOUT_S for timeout in timeouts)

    def test_returns_text_not_bytes(self) -> None:
        assert (
            create_redis("redis://localhost:6379").connection_pool.connection_kwargs["decode_responses"]
            is True
        )
