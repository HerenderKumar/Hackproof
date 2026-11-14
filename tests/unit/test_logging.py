import json

import pytest
import structlog

from core.logging import configure_logging


def test_log_lines_are_json_with_service_and_bound_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(service="worker", level="INFO")
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(job_id="j-1", attempt_id="a-1")
    structlog.get_logger().info("job_started", stage="build")
    structlog.contextvars.clear_contextvars()

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "job_started"
    assert line["service"] == "worker"
    assert line["job_id"] == "j-1"
    assert line["attempt_id"] == "a-1"
    assert line["stage"] == "build"
    assert line["level"] == "info"
    assert "timestamp" in line


def test_debug_lines_are_dropped_at_info_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="api", level="INFO")
    structlog.get_logger().debug("noise")
    assert capsys.readouterr().out == ""
