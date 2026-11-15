"""`python -m door`: the run's model door on port 8080 (Prune design, B1)."""

import os
from pathlib import Path

import uvicorn

from door.app import DoorConfig, make_app

PORT = 8080


def main() -> None:
    config = DoorConfig.from_env()
    ready = Path(os.environ.get("DOOR_READY", "/door/ready"))
    app = make_app(config, ready_path=ready)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
