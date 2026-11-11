import asyncio
import logging
from collections.abc import Awaitable, Callable

from reviewer.queue.redis_queue import POLL_TIMEOUT_S, JobQueue, QueuedJob

log = logging.getLogger(__name__)

Handler = Callable[[QueuedJob], Awaitable[object]]


async def run_worker(
    queue: JobQueue,
    handler: Handler,
    *,
    stop: asyncio.Event,
    concurrency: int = 2,
    poll_timeout_s: int = POLL_TIMEOUT_S,
) -> None:
    """Runs `concurrency` consumers until `stop` is set.

    A consumer that is idle (waiting for a job) is cancelled at once. One that is busy finishes its current
    job first, so a review is never cut off halfway by a restart.
    """
    busy = [False] * concurrency
    max_backoff_s = 60.0

    async def consume(n: int) -> None:
        failures = 0  # consecutive queue errors: drives the growing pause below
        while not stop.is_set():
            await asyncio.sleep(0)  # let other tasks and timers (including a pending shutdown) run
            try:
                job = await queue.next_job(poll_timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                failures += 1
                delay = min(5.0 * 2 ** (failures - 1), max_backoff_s)
                # One short line, not a traceback: when Redis is unreachable this repeats until it is back.
                log.warning(
                    "queue error; retrying",
                    extra={
                        "error": f"{type(err).__name__}: {err}"[:200],
                        "retry_in_s": delay,
                        "failures": failures,
                    },
                )
                await asyncio.sleep(delay)
                continue
            failures = 0
            if job is None:
                continue
            busy[n] = True
            try:
                await handler(job)
                await queue.complete(job)
            except Exception as err:
                will_retry = await queue.fail(job, f"{type(err).__name__}: {err}")
                log.error(
                    "review job failed",
                    extra={
                        "job_id": job.id,
                        "attempt": job.attempts,
                        "will_retry": will_retry,
                        "error": str(err)[:300],
                    },
                )
            finally:
                busy[n] = False

    tasks = [asyncio.create_task(consume(n)) for n in range(concurrency)]
    await stop.wait()
    for n, task in enumerate(tasks):
        if not busy[n]:
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
