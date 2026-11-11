"""Entry point for the worker process:  python -m reviewer.worker.main"""

import asyncio
import logging
import signal
import sys
from functools import partial

from reviewer.config import Settings, get_settings
from reviewer.db.session import create_engine, create_session_factory
from reviewer.github.client import GitHubApp
from reviewer.llm import create_llm
from reviewer.logging_setup import setup_logging
from reviewer.queue.redis_queue import JobQueue, create_redis
from reviewer.worker.handler import WorkerDeps, process_job
from reviewer.worker.runner import run_worker

log = logging.getLogger(__name__)


def install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGINT (Ctrl+C) and SIGTERM (a platform asking us to stop) both mean: finish current work, then exit."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops do not support add_signal_handler
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def run(settings: Settings, stop: asyncio.Event) -> None:
    """Builds everything the worker needs and runs it until `stop` is set. Shared with `reviewer.serve`."""
    redis = create_redis(settings.redis_url)
    queue = JobQueue(redis)
    github = GitHubApp(settings.github_app_id, settings.github_private_key)
    llm = create_llm(settings.llm_provider, settings.llm_api_key, settings.llm_model)
    engine = create_engine(settings.sqlalchemy_url) if settings.sqlalchemy_url else None
    deps = WorkerDeps(github.for_installation, llm, create_session_factory(engine) if engine else None)

    log.info("review worker started", extra={"llm": llm.model, "database": "on" if engine else "off"})
    try:
        await run_worker(queue, partial(process_job, deps=deps, max_attempts=queue.max_attempts), stop=stop)
    finally:
        await redis.aclose()
        if engine:
            await engine.dispose()
    log.info("review worker stopped")


async def amain() -> None:
    setup_logging()
    stop = asyncio.Event()
    install_signal_handlers(stop)
    await run(get_settings(), stop)


def main() -> None:
    if sys.platform == "win32":
        # psycopg's async mode cannot use Windows' default (Proactor) event loop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(amain())


if __name__ == "__main__":
    main()
