"""Startup cleanup (section 11.4): leftovers of dead attempts and old work directories go."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core import queue
from tests.fakes import FakeRuntime
from tests.helpers import Sessions, claim, claimed, expire_lease
from worker.cleanup import startup_cleanup

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


async def test_leftovers_of_attempts_that_are_not_running_are_removed(
    sessions: Sessions, tmp_path: Path
) -> None:
    running = await claimed(sessions, "final_run")
    dead = str(uuid.uuid4())
    runtime = FakeRuntime(
        containers={"app-live": str(running.attempt_id), "app-dead": dead, "runner-dead": dead},
        networks={"net-live": str(running.attempt_id), "net-dead": dead},
    )

    report = await startup_cleanup(sessions, runtime, tmp_path, NOW)

    assert runtime.containers == {"app-live": str(running.attempt_id)}
    assert runtime.networks == {"net-live": str(running.attempt_id)}
    assert (report.containers, report.networks) == (2, 1)


async def test_leftovers_of_a_finished_job_are_removed(sessions: Sessions, tmp_path: Path) -> None:
    job = await claimed(sessions, "final_run")
    async with sessions.begin() as s:
        await queue.complete(s, job.id, job.attempt_id)
    runtime = FakeRuntime(containers={"app": str(job.attempt_id)})

    await startup_cleanup(sessions, runtime, tmp_path, NOW)

    assert runtime.containers == {}


async def test_leftovers_of_a_superseded_attempt_are_removed(
    sessions: Sessions, tmp_path: Path
) -> None:
    first = await claimed(sessions, "final_run")
    await expire_lease(sessions, first.id)
    second = await claim(sessions, "heavy")
    assert second is not None
    runtime = FakeRuntime(containers={"old": str(first.attempt_id), "new": str(second.attempt_id)})

    await startup_cleanup(sessions, runtime, tmp_path, NOW)

    assert runtime.containers == {"new": str(second.attempt_id)}


async def test_containers_are_removed_before_their_networks(
    sessions: Sessions, tmp_path: Path
) -> None:
    dead = str(uuid.uuid4())
    runtime = FakeRuntime(containers={"c1": dead, "c2": dead}, networks={"n1": dead})

    await startup_cleanup(sessions, runtime, tmp_path, NOW)

    assert sorted(runtime.removed[:2]) == ["c1", "c2"]
    assert runtime.removed[2:] == ["n1"]


async def test_one_failed_removal_does_not_stop_the_rest(
    sessions: Sessions, tmp_path: Path
) -> None:
    dead = str(uuid.uuid4())
    runtime = FakeRuntime(
        containers={"stuck": dead, "c2": dead}, networks={"n1": dead}, unremovable={"stuck"}
    )

    report = await startup_cleanup(sessions, runtime, tmp_path, NOW)

    assert runtime.containers == {"stuck": dead}
    assert runtime.networks == {}
    assert (report.containers, report.networks) == (1, 1)


def _work_dir(root: Path, name: str, age: timedelta) -> Path:
    path = root / name
    (path / "src").mkdir(parents=True)
    (path / "src" / "app.py").write_text("print('hi')\n")
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


async def test_work_directories_older_than_an_hour_are_deleted(
    sessions: Sessions, tmp_path: Path
) -> None:
    old = _work_dir(tmp_path, "attempt-old", timedelta(minutes=61))
    fresh = _work_dir(tmp_path, "attempt-fresh", timedelta(minutes=59))

    report = await startup_cleanup(sessions, FakeRuntime(), tmp_path, NOW)

    assert not old.exists()
    assert (fresh / "src" / "app.py").exists()
    assert report.work_dirs == 1


async def test_a_symlink_in_the_work_directory_is_unlinked_not_followed(
    sessions: Sessions, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    work = tmp_path / "work"
    work.mkdir()
    link = work / "attempt-link"
    link.symlink_to(outside, target_is_directory=True)
    stamp = (NOW - timedelta(hours=2)).timestamp()
    os.utime(link, (stamp, stamp), follow_symlinks=False)

    await startup_cleanup(sessions, FakeRuntime(), work, NOW)

    assert not link.is_symlink()
    assert (outside / "keep.txt").read_text() == "keep"


async def test_a_missing_work_directory_is_not_an_error(sessions: Sessions, tmp_path: Path) -> None:
    report = await startup_cleanup(sessions, FakeRuntime(), tmp_path / "absent", NOW)
    assert report.work_dirs == 0


async def test_work_directories_are_still_swept_when_docker_is_unreachable(
    sessions: Sessions, tmp_path: Path
) -> None:
    old = _work_dir(tmp_path, "attempt-old", timedelta(hours=3))

    report = await startup_cleanup(sessions, FakeRuntime(reachable=False), tmp_path, NOW)

    assert not old.exists()
    assert (report.containers, report.networks, report.work_dirs) == (0, 0, 1)
