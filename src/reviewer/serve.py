"""Runs the webhook API AND the review worker in ONE process:  python -m reviewer.serve

Handy for hosting on a single small service. For more throughput, run them separately instead:
  API:     uvicorn --factory reviewer.api.app:create_app --host 0.0.0.0 --port 3001
  Worker:  python -m reviewer.worker.main
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable

import uvicorn

from reviewer.api.app import create_app
from reviewer.config import get_settings
from reviewer.logging_setup import setup_logging
from reviewer.worker.main import run as run_worker

log = logging.getLogger(__name__)


async def supervise(
    server: Awaitable[None],
    worker: Callable[[asyncio.Event], Awaitable[None]],
    ask_server_to_exit: Callable[[], None],
) -> None:
    """Runs both until either stops, then stops the other and waits for it.

    * Server stops first (Ctrl+C / SIGTERM): tell the worker to finish its current job and exit.
    * Worker dies first (a crash): stop the server too and re-raise, so the platform restarts the whole
      process instead of leaving a webhook receiver that queues jobs nobody will run.
    """
    stop_worker = asyncio.Event()
    server_task = asyncio.ensure_future(server)
    worker_task = asyncio.ensure_future(worker(stop_worker))

    await asyncio.wait({server_task, worker_task}, return_when=asyncio.FIRST_COMPLETED)
    stop_worker.set()
    ask_server_to_exit()
    await asyncio.gather(server_task, worker_task, return_exceptions=True)
    for task in (worker_task, server_task):  # the worker's error is the more useful one
        if not task.cancelled() and task.exception() is not None:
            raise task.exception()  # type: ignore[misc]


async def amain() -> None:
    setup_logging()
    settings = get_settings()
    config = uvicorn.Config(
        create_app(settings), host="0.0.0.0", port=settings.port, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)

    def ask_server_to_exit() -> None:
        server.should_exit = True

    log.info("starting API and worker", extra={"port": settings.port})
    await supervise(server.serve(), lambda stop: run_worker(settings, stop), ask_server_to_exit)


def main() -> None:
    if sys.platform == "win32":
        # psycopg's async mode cannot use Windows' default (Proactor) event loop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(amain())


if __name__ == "__main__":
    main()
