"""
agent/core/async_utils.py

Safe coroutine runner for code that may be called from both sync and async contexts.

Problem:
    Every skill calls asyncio.run(llm.chat(...)).
    If the skill is ever called from FastAPI, pytest-asyncio, Jupyter, or the AgentOS
    dashboard (all of which run their own event loop), asyncio.run() raises:
        RuntimeError: This event loop is already running.

Solution:
    run_sync() detects whether a loop is already running and routes accordingly.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Coroutine, TypeVar

T = TypeVar("T")


def run_sync(coro: Coroutine[None, None, T]) -> T:
    """
    Run a coroutine from synchronous code — safely handles an already-running loop.

    Usage (replace every ``asyncio.run(...)`` in skills with this):
        from agent.core.async_utils import run_sync
        result = run_sync(llm.chat(...))
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to use asyncio.run()
        return asyncio.run(coro)

    # A loop is already running (FastAPI / dashboard / test runner).
    # Submit the coroutine to a fresh thread that runs its own loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()
