"""The `prepare_snapshot` job: archive the commit a team selected (sections 11.6 and 12.1).

A snapshot is keyed by team and commit and its archive by content, so running the job again,
or a stale attempt finishing late, stores nothing new. Problems with the team's repository
(the commit is not there, the repository is gone, a link leads out of the project) are
results for the team, not errors to retry; anything else is left to the job loop.

Tests are drafted from a team's claims, not from its snapshots (D-047), so finishing a
snapshot enqueues nothing: `draft_specs` follows a claims version.
"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from core import queue
from core.db import models
from core.db.engine import Sessions
from core.logging import get_logger
from core.repo import CommitNotFound, InvalidCommit, RepoNotFound, RepoProvider
from core.snapshots import UnsafeArchive, check_commit_sha
from worker.lanes import Handler, Outcome
from worker.watch import repo_of

log = get_logger()


def make_prepare_snapshot(
    sessions: Sessions, provider: RepoProvider, archives_dir: Path
) -> Handler:
    async def prepare_snapshot(job: queue.Job) -> Outcome | None:
        sha = str(job.payload.get("commit_sha", ""))
        if job.team_id is None or not _is_full_sha(sha):
            return Outcome("validation", "prepare_snapshot needs a team and a full commit SHA")

        async with sessions() as session:
            team = await session.get(models.Team, job.team_id)
            existing = await session.scalar(
                select(models.Snapshot.id).where(
                    models.Snapshot.team_id == job.team_id, models.Snapshot.commit_sha == sha
                )
            )
        if existing is not None:
            return None
        if team is None or team.registered_at is None or not team.repo_url:
            return Outcome("validation", "the team has no registered repository")

        repo = repo_of(team, provider)
        try:
            files = await provider.snapshot(repo, sha, archives_dir)
        except (CommitNotFound, RepoNotFound, UnsafeArchive) as exc:
            log.info("snapshot_refused", commit_sha=sha, reason=str(exc))
            return Outcome("app", str(exc))

        async with sessions.begin() as session:
            await session.execute(
                insert(models.Snapshot)
                .values(
                    team_id=job.team_id,
                    commit_sha=sha,
                    archive_sha256=files.archive_sha256,
                    archive_path=str(files.archive_path),
                    commit_history=files.commit_history,
                    lines_total=files.lines_total,
                )
                .on_conflict_do_nothing(index_elements=["team_id", "commit_sha"])
            )
        log.info(
            "snapshot_prepared",
            commit_sha=sha,
            archive_sha256=files.archive_sha256,
            lines_total=files.lines_total,
        )
        return None

    return prepare_snapshot


def _is_full_sha(sha: str) -> bool:
    try:
        check_commit_sha(sha)
    except InvalidCommit:
        return False
    return True
