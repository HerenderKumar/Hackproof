"""`python -m door`: the run's model door on port 8080, and, when the event lists outside
hosts, the allowlist proxy for the app's own calls on 8081 (Prune design, B1; D-063)."""

import asyncio
import os
from pathlib import Path

import uvicorn

from door.app import DoorConfig, make_app
from door.egress import Egress, serve

PORT = 8080
EGRESS_PORT = 8081


async def main() -> None:
    config = DoorConfig.from_env()
    ready = Path(os.environ.get("DOOR_READY", "/door/ready"))
    app = make_app(config, ready_path=None)
    door = app.state.door
    if config.egress_hosts:
        egress = Egress(config.egress_hosts, door.log, max_tunnels=config.max_egress)
        await serve(egress, EGRESS_PORT)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning", access_log=False)
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving  # raises why it stopped
            return
        await asyncio.sleep(0.05)
    # Both servers are listening: the worker may start the app.
    ready.write_text("ready\n")  # noqa: ASYNC240 - once, before any request
    await serving


if __name__ == "__main__":
    asyncio.run(main())
