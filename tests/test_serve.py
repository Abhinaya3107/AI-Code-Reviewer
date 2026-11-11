import asyncio

import pytest

from reviewer.serve import supervise


class Recorder:
    def __init__(self) -> None:
        self.server_exit_requested = asyncio.Event()
        self.worker_stopped = False

    def ask_server_to_exit(self) -> None:
        self.server_exit_requested.set()


async def test_when_the_server_stops_the_worker_is_told_to_finish_and_exit() -> None:
    rec = Recorder()

    async def server() -> None:
        await asyncio.sleep(0.05)  # e.g. Ctrl+C: uvicorn finishes gracefully

    async def worker(stop: asyncio.Event) -> None:
        await stop.wait()
        await asyncio.sleep(0.05)  # finishing the current job
        rec.worker_stopped = True

    await asyncio.wait_for(supervise(server(), worker, rec.ask_server_to_exit), timeout=5)
    assert rec.worker_stopped  # waited for the worker instead of abandoning it


async def test_when_the_worker_crashes_the_server_is_stopped_and_the_error_is_raised() -> None:
    rec = Recorder()
    server_done = asyncio.Event()

    async def server() -> None:
        await rec.server_exit_requested.wait()
        server_done.set()

    async def worker(stop: asyncio.Event) -> None:
        raise RuntimeError("redis is down")

    with pytest.raises(RuntimeError, match="redis is down"):
        await asyncio.wait_for(supervise(server(), worker, rec.ask_server_to_exit), timeout=5)
    assert server_done.is_set()  # the process does not keep accepting webhooks nobody will process


async def test_when_the_server_crashes_the_worker_is_stopped_too() -> None:
    rec = Recorder()

    async def server() -> None:
        raise OSError("port already in use")

    async def worker(stop: asyncio.Event) -> None:
        await stop.wait()
        rec.worker_stopped = True

    with pytest.raises(OSError, match="port already in use"):
        await asyncio.wait_for(supervise(server(), worker, rec.ask_server_to_exit), timeout=5)
    assert rec.worker_stopped
