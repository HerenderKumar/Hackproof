"""The repo watch (sections 6.6 and 13): the `kickoff_snapshot` and `poll_repo` jobs.

Each writes `repo_observations`, the platform's own record of when each head was first seen.
Commit dates inside them are the team's word; `observed_at` is ours (the injected clock, D-035).
Both handlers ask the provider first, outside any transaction, then write under a lock on the
team's row, so two attempts of one job, or two polls racing, store one row.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import queue
from core.clock import Clock
from core.db import models
from core.db.engine import Sessions
from core.logging import get_logger
from core.repo import Comparison, RepoError, RepoInfo, RepoProvider
from worker.lanes import Handler, Outcome

log = get_logger()

BASELINE_KINDS = ("kickoff", "late_registration")


def repo_of(team: models.Team, provider: RepoProvider) -> RepoInfo:
    """The registered repository as the provider needs it. The team must have a `repo_url`."""
    assert team.repo_url
    return RepoInfo(
        provider=provider.name,
        id=team.repository_id or "",
        url=team.repo_url,
        default_branch=team.default_branch or "main",
        created_at=team.repo_created_at,
        is_public=True,
    )


def make_kickoff_handler(sessions: Sessions, provider: RepoProvider, clock: Clock) -> Handler:
    async def kickoff_snapshot(job: queue.Job) -> Outcome | None:
        if job.team_id is None:
            return Outcome("validation", "kickoff_snapshot needs a team")
        async with sessions() as session:
            team = await session.get(models.Team, job.team_id)
            event = await session.get(models.Event, team.event_id) if team else None
            done = await _has_baseline(session, job.team_id)
        if team is None or event is None or team.registered_at is None or not team.repo_url:
            return Outcome("validation", "the team has no registered repository")
        if done:
            return None

        repo = repo_of(team, provider)
        try:
            head = await provider.head(repo)
            found = await provider.compare(repo, None, head) if head is not None else None
        except RepoError as exc:
            log.info("kickoff_refused", reason=str(exc))
            return Outcome("app", str(exc))

        kind = "late_registration" if team.registered_at > event.kickoff_at else "kickoff"
        async with sessions.begin() as session:
            await _lock_team(session, job.team_id)
            if await _has_baseline(session, job.team_id):
                return None
            session.add(
                models.RepoObservation(
                    team_id=job.team_id,
                    kind=kind,
                    observed_at=clock.now(),
                    head_sha=head,
                    lines_total=found.lines_added if found else 0,
                    files_changed=found.files_changed if found else 0,
                    commits=found.commits if found else [],
                )
            )
        log.info("kickoff_observed", kind=kind, head_sha=head)
        return None

    return kickoff_snapshot


def make_poll_handler(sessions: Sessions, provider: RepoProvider, clock: Clock) -> Handler:
    async def poll_repo(job: queue.Job) -> Outcome | None:
        if job.team_id is None:
            return Outcome("validation", "poll_repo needs a team")
        async with sessions() as session:
            team = await session.get(models.Team, job.team_id)
            latest = await _newest(session, job.team_id)
        if team is None or team.registered_at is None or not team.repo_url:
            return Outcome("validation", "the team has no registered repository")
        if latest is None:
            return None  # the kickoff row comes first; the next poll will see the change

        repo = repo_of(team, provider)
        try:
            head = await provider.head(repo)
            if head == latest.head_sha:
                return None
            found = (
                await provider.compare(repo, latest.head_sha, head)
                if head is not None
                else Comparison(status="diverged", lines_added=0, files_changed=0, commits=[])
            )
        except RepoError as exc:
            log.info("poll_refused", reason=str(exc))
            return Outcome("app", str(exc))

        async with sessions.begin() as session:
            await _lock_team(session, job.team_id)
            newest = await _newest(session, job.team_id)
            # Another poll wrote first: this head is recorded, or the comparison's base is stale
            # and the next poll compares against the newer row.
            if newest is None or newest.id != latest.id:
                return None
            session.add(
                models.RepoObservation(
                    team_id=job.team_id,
                    kind="poll",
                    observed_at=clock.now(),
                    head_sha=head,
                    previous_head_sha=latest.head_sha,
                    descends_from_previous=found.status == "ahead",
                    lines_added=found.lines_added,
                    files_changed=found.files_changed,
                    commits=found.commits,
                )
            )
        log.info(
            "poll_observed",
            head_sha=head,
            previous_head_sha=latest.head_sha,
            status=found.status,
            lines_added=found.lines_added,
        )
        return None

    return poll_repo


async def _has_baseline(session: AsyncSession, team_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(models.RepoObservation.id)
        .where(
            models.RepoObservation.team_id == team_id,
            models.RepoObservation.kind.in_(BASELINE_KINDS),
        )
        .limit(1)
    )
    return found is not None


async def _newest(session: AsyncSession, team_id: uuid.UUID) -> models.RepoObservation | None:
    return await session.scalar(
        select(models.RepoObservation)
        .where(models.RepoObservation.team_id == team_id)
        .order_by(models.RepoObservation.observed_at.desc(), models.RepoObservation.id.desc())
        .limit(1)
    )


async def _lock_team(session: AsyncSession, team_id: uuid.UUID) -> None:
    await session.execute(select(models.Team.id).where(models.Team.id == team_id).with_for_update())
