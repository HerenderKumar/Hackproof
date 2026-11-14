"""The worker's heartbeat file: how `/readyz` learns whether Docker is reachable (D-030)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from core import worker_health
from core.worker_health import WorkerHealth
from tests.fakes import FakeClock, FakeRuntime
from worker import heartbeat

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def test_a_written_heartbeat_reads_back(tmp_path: Path) -> None:
    worker_health.write(tmp_path, WorkerHealth(at=NOW, docker_ok=True))
    assert worker_health.read(tmp_path) == WorkerHealth(at=NOW, docker_ok=True)


def test_writing_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    worker_health.write(tmp_path, WorkerHealth(at=NOW, docker_ok=True))
    worker_health.write(tmp_path, WorkerHealth(at=NOW, docker_ok=False))
    assert [p.name for p in tmp_path.iterdir()] == ["worker_health.json"]


def test_a_missing_or_unreadable_heartbeat_reads_as_none(tmp_path: Path) -> None:
    assert worker_health.read(tmp_path) is None
    (tmp_path / "worker_health.json").write_text("{not json")
    assert worker_health.read(tmp_path) is None


def test_docker_is_ready_only_with_a_fresh_heartbeat_that_says_so() -> None:
    fresh = WorkerHealth(at=NOW - timedelta(seconds=59), docker_ok=True)
    stale = WorkerHealth(at=NOW - timedelta(seconds=61), docker_ok=True)
    down = WorkerHealth(at=NOW, docker_ok=False)

    assert worker_health.docker_ready(fresh, NOW) is True
    assert worker_health.docker_ready(stale, NOW) is False
    assert worker_health.docker_ready(down, NOW) is False
    assert worker_health.docker_ready(None, NOW) is False


async def test_a_beat_records_that_docker_answered(tmp_path: Path) -> None:
    state = heartbeat.DockerState()

    await heartbeat.beat(FakeRuntime(reachable=True), tmp_path, FakeClock(NOW), state)

    assert state.reachable is True
    assert worker_health.read(tmp_path) == WorkerHealth(at=NOW, docker_ok=True)


async def test_a_beat_records_that_docker_is_down(tmp_path: Path) -> None:
    state = heartbeat.DockerState(reachable=True)

    await heartbeat.beat(FakeRuntime(reachable=False), tmp_path, FakeClock(NOW), state)

    assert state.reachable is False
    assert worker_health.read(tmp_path) == WorkerHealth(at=NOW, docker_ok=False)


async def test_a_ping_that_raises_counts_as_docker_down(tmp_path: Path) -> None:
    class BrokenRuntime(FakeRuntime):
        async def ping(self) -> bool:
            raise OSError("socket not found")

    state = heartbeat.DockerState(reachable=True)

    await heartbeat.beat(BrokenRuntime(), tmp_path, FakeClock(NOW), state)

    assert state.reachable is False
    assert worker_health.read(tmp_path) == WorkerHealth(at=NOW, docker_ok=False)
