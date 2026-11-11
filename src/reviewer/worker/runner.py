import asyncio
import logging
from collections.abc import Awaitable, Callable

from reviewer.queue.redis_queue import JobQueue, QueuedJob

log = logging.getLogger(__name__)

Handler = Callable[[QueuedJob], Awaitable[object]]


async def run_worker(
    queue: JobQueue,
    handler: Handler,
    *,
    stop: asyncio.Event,
    concurrency: int = 2,
    poll_timeout_s: int = 30,
) -> None:
    """Runs `concurrency` consumers until `stop` is set.

    A consumer that is idle (waiting for a job) is cancelled at once. One that is busy finishes its current
    job first, so a review is never cut off halfway by a restart.
    """
    busy = [False] * concurrency

    async def consume(n: int) -> None:
        while not stop.is_set():
            await asyncio.sleep(0)  # let other tasks and timers (including a pending shutdown) run
            try:
                job = await queue.next_job(poll_timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("queue error; retrying in 5s")
                await asyncio.sleep(5)
                continue
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
