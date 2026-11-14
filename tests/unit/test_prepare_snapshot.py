"""The `prepare_snapshot` job (sections 11.6 and 12.1)."""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import queue
from core.db import models
from core.errors import InfraError
from core.providers.local import LocalGitProvider
from core.repo import CommitNotFound, RepoInfo, RepoNotFound, SnapshotFiles
from core.snapshots import UnsafeArchive
from scripts.fixtures import load_history_fixtures
from scripts.git_fixtures import PUSH_LOG, build_history_remote
from tests.factories import T0, make_event, make_team
from tests.fakes import FakeClock
from worker.lanes import Outcome
from worker.snapshot import make_prepare_snapshot

Sessions = async_sessionmaker[AsyncSession]
H1 = next(h for h in load_history_fixtures() if h.id == "H1")


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    build_history_remote(H1, tmp_path / "remotes", T0)
    return tmp_path / "remotes"


@pytest.fixture
def h1_head(remotes: Path) -> str:
    log = json.loads((remotes / "H1-bulk-push.git" / PUSH_LOG).read_text())
    return str(log[-1]["head"])


async def team_with_repo(sessions: Sessions, *, registered: bool = True) -> uuid.UUID:
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        team = make_team(
            event.id,
            repo_url="local://H1-bulk-push",
            repo_provider="local",
            repository_id="H1-bulk-push",
            default_branch="main",
            repo_created_at=T0,
            registered_at=T0 if registered else None,
        )
        session.add(team)
        await session.flush()
        return team.id


async def claimed_job(sessions: Sessions, team_id: uuid.UUID, sha: str) -> queue.Job:
    async with sessions.begin() as session:
        await queue.enqueue(
            session,
            "prepare_snapshot",
            queue.prepare_key(team_id, sha),
            payload={"commit_sha": sha},
            team_id=team_id,
        )
    async with sessions.begin() as session:
        job = await queue.claim(session, "heavy")
    assert job is not None
    return job


async def snapshots(sessions: Sessions) -> list[models.Snapshot]:
    async with sessions() as session:
        return list((await session.scalars(select(models.Snapshot))).all())


def local_handler(sessions: Sessions, remotes: Path, archives: Path):  # type: ignore[no-untyped-def]
    return make_prepare_snapshot(sessions, LocalGitProvider(remotes, FakeClock(T0)), archives)


async def test_the_selected_commit_is_stored_as_a_snapshot(
    sessions: Sessions, remotes: Path, h1_head: str, tmp_path: Path
) -> None:
    team_id = await team_with_repo(sessions)
    job = await claimed_job(sessions, team_id, h1_head)

    outcome = await local_handler(sessions, remotes, tmp_path / "archives")(job)

    assert outcome is None
    [snapshot] = await snapshots(sessions)
    assert (snapshot.team_id, snapshot.commit_sha) == (team_id, h1_head)
    archive = Path(snapshot.archive_path)
    assert archive == tmp_path / "archives" / f"{snapshot.archive_sha256}.tar.gz"
    assert archive.is_file()
    assert next(c["message"] for c in snapshot.commit_history) == "Add the routing engine"
    assert snapshot.lines_total == 3 + 4 + 3000  # README, NOTES and the generated engine


async def test_preparing_the_same_commit_again_keeps_one_snapshot(
    sessions: Sessions, remotes: Path, h1_head: str, tmp_path: Path
) -> None:
    team_id = await team_with_repo(sessions)
    job = await claimed_job(sessions, team_id, h1_head)
    handler = local_handler(sessions, remotes, tmp_path / "archives")

    await handler(job)
    await handler(job)  # a retried attempt, or a stale one finishing late

    assert len(await snapshots(sessions)) == 1
    assert len(list((tmp_path / "archives").iterdir())) == 1


async def test_a_commit_the_repository_does_not_have_is_reported_to_the_team(
    sessions: Sessions, remotes: Path, tmp_path: Path
) -> None:
    team_id = await team_with_repo(sessions)
    job = await claimed_job(sessions, team_id, "0" * 40)

    outcome = await local_handler(sessions, remotes, tmp_path / "archives")(job)

    assert outcome is not None and outcome.error_class == "app"
    assert "0" * 40 in (outcome.error_detail or "")
    assert await snapshots(sessions) == []


async def test_a_job_without_a_full_commit_sha_is_rejected(
    sessions: Sessions, remotes: Path, tmp_path: Path
) -> None:
    team_id = await team_with_repo(sessions)
    job = await claimed_job(sessions, team_id, "main")

    outcome = await local_handler(sessions, remotes, tmp_path / "archives")(job)

    assert outcome == Outcome("validation", "prepare_snapshot needs a team and a full commit SHA")


async def test_a_team_without_a_registered_repository_is_rejected(
    sessions: Sessions, remotes: Path, h1_head: str, tmp_path: Path
) -> None:
    team_id = await team_with_repo(sessions, registered=False)
    job = await claimed_job(sessions, team_id, h1_head)

    outcome = await local_handler(sessions, remotes, tmp_path / "archives")(job)

    assert outcome is not None and outcome.error_class == "validation"
    assert await snapshots(sessions) == []


class Raising:
    """A provider whose snapshot fails with a given error."""

    name = "local"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def describe(self, repo_url: str) -> RepoInfo:
        raise NotImplementedError

    async def snapshot(self, repo: RepoInfo, sha: str, archives_dir: Path) -> SnapshotFiles:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        CommitNotFound("the repository has no commit"),
        RepoNotFound("no such repository"),
        UnsafeArchive("passwords: link to /etc/passwd"),
    ],
    ids=["missing commit", "repository gone", "link out of the project"],
)
async def test_problems_with_the_team_s_repository_are_results_not_retries(
    sessions: Sessions, h1_head: str, tmp_path: Path, error: Exception
) -> None:
    team_id = await team_with_repo(sessions)
    job = await claimed_job(sessions, team_id, h1_head)

    outcome = await make_prepare_snapshot(sessions, Raising(error), tmp_path)(job)

    assert outcome == Outcome("app", str(error))


async def test_infrastructure_errors_are_left_to_the_job_loop_to_retry(
    sessions: Sessions, h1_head: str, tmp_path: Path
) -> None:
    team_id = await team_with_repo(sessions)
    job = await claimed_job(sessions, team_id, h1_head)
    handler = make_prepare_snapshot(sessions, Raising(InfraError("clone timed out")), tmp_path)

    with pytest.raises(InfraError):
        await handler(job)

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(models.Snapshot)) == 0
