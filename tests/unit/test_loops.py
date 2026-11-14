"""Periodic tasks in the worker: the scheduler and the heartbeat run on these."""

import asyncio
import time

from worker.loops import every, pause, wait_until


async def test_pause_returns_at_once_when_the_worker_is_stopping() -> None:
    stop = asyncio.Event()
    stop.set()
    started = time.monotonic()

    await pause(stop, 30)

    assert time.monotonic() - started < 1


async def test_pause_sleeps_for_the_given_time_otherwise() -> None:
    started = time.monotonic()
    await pause(asyncio.Event(), 0.05)
    assert time.monotonic() - started >= 0.04


async def test_every_repeats_the_action_until_stopped() -> None:
    stop = asyncio.Event()
    runs = 0

    async def action() -> None:
        nonlocal runs
        runs += 1
        if runs == 3:
            stop.set()

    await asyncio.wait_for(every(0.01, action, stop, name="test"), timeout=5)

    assert runs == 3


async def test_every_keeps_going_after_the_action_raises() -> None:
    stop = asyncio.Event()
    runs = 0

    async def action() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("database is restarting")
        stop.set()

    await asyncio.wait_for(every(0.01, action, stop, name="test"), timeout=5)

    assert runs == 2


async def test_wait_until_returns_true_once_the_check_passes() -> None:
    answers = iter([False, False, True])
    calls = 0

    async def check() -> bool:
        nonlocal calls
        calls += 1
        return next(answers)

    waited = await asyncio.wait_for(wait_until(check, asyncio.Event(), poll_s=0.01), timeout=5)

    assert waited is True
    assert calls == 3


async def test_wait_until_treats_a_check_that_raises_as_not_yet() -> None:
    calls = 0

    async def check() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("database is starting")
        return True

    assert await asyncio.wait_for(wait_until(check, asyncio.Event(), poll_s=0.01), timeout=5)
    assert calls == 2


async def test_wait_until_gives_up_when_the_worker_is_stopped() -> None:
    stop = asyncio.Event()

    async def check() -> bool:
        stop.set()
        return False

    assert await asyncio.wait_for(wait_until(check, stop, poll_s=30), timeout=5) is False
