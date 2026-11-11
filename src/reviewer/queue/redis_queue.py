"""A small reliable job queue on Redis.

Why not Celery or RQ? Celery polls Redis constantly (a cost problem on pay-per-command hosted Redis such
as Upstash), and RQ forks processes (not available on Windows). This queue needs only blocking pops.

Guarantees
  * Idempotent: enqueueing the same job id twice is a no-op (so a GitHub webhook redelivery is harmless).
  * At-least-once: a job moves to a "processing" list when claimed. If the worker dies, the job is found
    again after `visibility_timeout` and re-queued.
  * Retries with exponential backoff, then a dead-letter list for inspection.

Redis keys (all under a prefix):
  job:<id>   hash   payload, state, attempts, claimed_at, error
  pending    list   job ids waiting to run (LPUSH, claimed from the right: FIFO)
  processing list   job ids a worker has claimed
  delayed    zset   job ids waiting for a retry, scored by the time they become due
  failed     list   job ids that ran out of attempts
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

log = logging.getLogger(__name__)

# How long a worker waits for a job in one blocking command. One command per wait keeps idle cost very low.
POLL_TIMEOUT_S = 30


def create_redis(url: str) -> Redis:
    """The Redis client used everywhere, configured for blocking pops.

    redis-py 8 applies a short default read timeout (about 5 seconds), which silently kills any blocking wait
    longer than that: the worker would fail every few seconds instead of waiting for a job. The socket timeout
    must be longer than the longest blocking wait.
    """
    return Redis.from_url(
        url, decode_responses=True, socket_timeout=POLL_TIMEOUT_S + 15, socket_connect_timeout=10
    )


def _text(value: object) -> str:
    """The client is created with decode_responses=True, so values are text; this keeps the type checker honest."""
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(frozen=True)
class QueuedJob:
    id: str
    payload: dict[str, Any]
    attempts: int  # 1 on the first run


class JobQueue:
    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = "acr",
        max_attempts: int = 3,
        base_delay_s: float = 5.0,
        retention_s: int = 7 * 24 * 3600,
        visibility_timeout_s: float = 15 * 60,
    ) -> None:
        self._r = redis
        self._p = prefix
        self.max_attempts = max_attempts
        self._base_delay = base_delay_s
        self._retention = retention_s
        self._visibility = visibility_timeout_s

    def _job_key(self, job_id: str) -> str:
        return f"{self._p}:job:{job_id}"

    @property
    def _pending(self) -> str:
        return f"{self._p}:pending"

    @property
    def _processing(self) -> str:
        return f"{self._p}:processing"

    @property
    def _delayed(self) -> str:
        return f"{self._p}:delayed"

    @property
    def _failed(self) -> str:
        return f"{self._p}:failed"

    async def enqueue(self, job_id: str, payload: dict[str, Any]) -> bool:
        """Returns False if this job id was already enqueued (within the retention period).

        HSETNX is the atomic "create if absent" gate. The rest is one MULTI/EXEC transaction. If a process dies
        between the two, the job record exists without a "state": the next enqueue of the same id sees that and
        finishes the job instead of treating it as a duplicate.
        """
        key = self._job_key(job_id)
        created = await self._r.hsetnx(key, "payload", json.dumps(payload))
        if not created and await self._r.hexists(key, "state"):
            return False
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={"state": "queued", "attempts": 0})
            pipe.expire(key, self._retention)
            pipe.lpush(self._pending, job_id)
            await pipe.execute()
        return True

    async def next_job(self, timeout_s: int = 30) -> QueuedJob | None:
        """Waits up to timeout_s for a job. One blocking command per wait keeps idle cost very low."""
        await self._promote_due()
        await self._recover_stalled()
        popped = await self._r.blmove(self._pending, self._processing, timeout_s, "RIGHT", "LEFT")
        if popped is None:
            return None
        job_id = _text(popped)
        key = self._job_key(job_id)
        raw = await self._r.hget(key, "payload")
        if raw is None:  # the job record expired while it waited: nothing to run
            await self._r.lrem(self._processing, 1, job_id)
            return None
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={"state": "running", "claimed_at": time.time()})
            pipe.hincrby(key, "attempts", 1)
            _, attempts = await pipe.execute()
        return QueuedJob(id=job_id, payload=json.loads(raw), attempts=int(attempts))

    async def complete(self, job: QueuedJob) -> None:
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.lrem(self._processing, 1, job.id)
            pipe.hset(self._job_key(job.id), mapping={"state": "completed"})
            await pipe.execute()

    async def fail(self, job: QueuedJob, error: str) -> bool:
        """Records a failure. Returns True if the job will be retried, False if it was dead-lettered."""
        key = self._job_key(job.id)
        retry = job.attempts < self.max_attempts
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.lrem(self._processing, 1, job.id)
            if retry:
                delay = self._base_delay * 2 ** (job.attempts - 1)
                pipe.hset(key, mapping={"state": "retrying", "error": error[:500]})
                pipe.zadd(self._delayed, {job.id: time.time() + delay})
            else:
                pipe.hset(key, mapping={"state": "failed", "error": error[:500]})
                pipe.lpush(self._failed, job.id)
            await pipe.execute()
        return retry

    async def _promote_due(self) -> None:
        """Moves retries whose delay has passed back to the pending list."""
        due = await self._r.zrangebyscore(self._delayed, "-inf", time.time())
        for raw_id in due:
            job_id = _text(raw_id)
            if await self._r.zrem(self._delayed, job_id) == 1:  # only one worker wins the claim
                await self._r.lpush(self._pending, job_id)

    async def _recover_stalled(self) -> None:
        """Re-queues jobs a crashed worker was holding (claimed longer ago than the visibility timeout)."""
        for raw_id in await self._r.lrange(self._processing, 0, -1):
            job_id = _text(raw_id)
            claimed_at = await self._r.hget(self._job_key(job_id), "claimed_at")
            if claimed_at is not None and time.time() - float(claimed_at) < self._visibility:
                continue
            if await self._r.lrem(self._processing, 1, job_id) == 1:
                log.warning("re-queueing a stalled job", extra={"job_id": job_id})
                await self._r.lpush(self._pending, job_id)

    async def stats(self) -> dict[str, int]:
        return {
            "pending": await self._r.llen(self._pending),
            "processing": await self._r.llen(self._processing),
            "delayed": await self._r.zcard(self._delayed),
            "failed": await self._r.llen(self._failed),
        }
