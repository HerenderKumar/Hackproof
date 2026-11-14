"""Worker process: one process, two lanes (section 11). The job loop arrives in M1."""

import asyncio
import signal

from core.logging import configure_logging, get_logger
from core.settings import Settings, get_settings


async def run(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    settings.require_for_worker()
    configure_logging(service="worker", level=settings.log_level)
    log = get_logger()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("worker_started", llm_mode=settings.llm_mode, repo_provider=settings.repo_provider)
    await stop.wait()
    log.info("worker_stopped")
