"""Shared plumbing for the job entrypoints."""

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def run_async(coro: Coroutine[Any, Any, int]) -> int:
    """Run a coroutine to completion from synchronous code, wherever we are.

    Serverless `python_wheel_task`s call the entrypoint inside an IPython kernel that already
    runs an event loop, where `asyncio.run()` raises "cannot be called from a running event
    loop". In that case, run the coroutine on a fresh loop in a worker thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def finish(exit_code: int) -> None:
    """Fail the job task on a non-zero code. Raise, don't SystemExit: inside the wheel task's
    IPython kernel a SystemExit does not reliably fail the run."""
    if exit_code:
        raise RuntimeError(f"job finished with exit code {exit_code}; see the log above")
