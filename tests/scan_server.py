"""The test app served over real HTTP, for the scanner, which talks to it with urllib (M9)."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[str]:
    """Serve `app` on a free local port, on this event loop; yield its address.

    The scanner's calls block, so a test runs it in a thread (`asyncio.to_thread`) while this
    loop answers. Lifespan is off: the test's fixture disposes the app's engine itself.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()
