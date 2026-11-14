"""Building blocks for the worker's long-running loops."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from core.logging import get_logger

log = get_logger()


async def pause(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake at once when the worker is asked to stop."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def every(
    seconds: float, action: Callable[[], Awaitable[object]], stop: asyncio.Event, *, name: str
) -> None:
    """Run `action` now and then every `seconds` until stopped. A failed run is logged."""
    while not stop.is_set():
        try:
            await action()
        except Exception:
            log.exception("periodic_task_failed", task=name)
        await pause(stop, seconds)


async def wait_until(
    check: Callable[[], Awaitable[bool]], stop: asyncio.Event, *, poll_s: float
) -> bool:
    """Poll `check` until it passes. Returns False if the worker is stopped first.

    A check that raises counts as "not yet": what it waits for may not be up at all.
    """
    while not stop.is_set():
        with contextlib.suppress(Exception):
            if await check():
                return True
        await pause(stop, poll_s)
    return False
