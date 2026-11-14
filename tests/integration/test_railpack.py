"""Railpack inside the worker image. Runs there (`make test-integration`)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.fixtures import FIXTURES_DIR

pytestmark = pytest.mark.integration

FOUND = "Mise executable exists at"
DOWNLOADING = "not found, installing"


def first_mise_line(app: Path, out: Path) -> str:
    """Railpack's first log line about mise, without waiting for the rest of the plan.

    The rest needs the network (version lookups); whether mise is already there does not.
    """
    process = subprocess.Popen(
        [
            "railpack",
            "--verbose",
            "prepare",
            str(app),
            "--plan-out",
            str(out / "plan.json"),
            "--info-out",
            str(out / "info.json"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if FOUND in line or DOWNLOADING in line:
                return line.strip()
        return f"railpack exited ({process.wait()}) without mentioning mise"
    finally:
        process.kill()
        process.wait()


def test_railpack_uses_the_mise_baked_into_the_image(tmp_path: Path) -> None:
    app = tmp_path / "app"
    shutil.copytree(FIXTURES_DIR / "F01-node-splitter" / "good", app)

    line = first_mise_line(app, tmp_path)

    # Downloading it at run time took minutes on a slow link, with no output (progress.md).
    assert FOUND in line, line
