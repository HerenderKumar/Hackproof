"""DockerRuntime when the daemon is down. Calls that need a daemon are in tests/integration."""

import pytest

from worker.runtime import DockerRuntime


@pytest.fixture
def no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")


async def test_ping_is_false_while_the_daemon_is_unreachable(no_daemon: None) -> None:
    assert await DockerRuntime().ping() is False


async def test_listing_raises_while_the_daemon_is_unreachable(no_daemon: None) -> None:
    with pytest.raises(Exception, match=r"(?i)docker|connection|no such file"):
        await DockerRuntime().attempt_containers()


async def test_ping_recovers_once_the_daemon_is_back(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = DockerRuntime()
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    assert await runtime.ping() is False

    class Daemon:
        class api:
            @staticmethod
            def ping() -> bool:
                return True

    monkeypatch.setattr("worker.runtime.docker.from_env", lambda **_: Daemon())
    assert await runtime.ping() is True
