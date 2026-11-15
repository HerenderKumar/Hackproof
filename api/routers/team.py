"""The team's own data (section 5.2). Every query starts from the session's team ID."""

from datetime import datetime

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from api.deps import Db, TeamUser
from api.errors import api_error
from api.routers.team_tests import tests_view
from api.schemas import ModelDoorTerms, RepoIn, TeamEvent, TeamProgress, TeamRepo, TeamView
from api.team_common import own_team
from core.db import models
from core.errors import InfraError
from core.events import claims_lock_at
from core.repo import InvalidRepoUrl, RepoNotFound, RepoProvider
from core.settings import Settings

router = APIRouter(prefix="/api", tags=["team"])


async def _progress(db: Db, team: models.Team) -> TeamProgress:
    tests = await tests_view(db, team.id)
    states = [claim.state for claim in tests.claims]
    snapshots = await db.scalar(
        select(func.count()).select_from(models.Snapshot).where(models.Snapshot.team_id == team.id)
    )
    submissions = await db.scalar(
        select(func.count())
        .select_from(models.Submission)
        .where(models.Submission.team_id == team.id)
    )
    return TeamProgress(
        has_claims=tests.claims_version is not None,
        tests_approved=states.count("approved"),
        tests_total=states.count("approved") + states.count("draft"),
        has_snapshot=bool(snapshots),
        has_submitted=bool(submissions),
    )


async def _view(
    db: Db, team: models.Team, event: models.Event, now: datetime, settings: Settings
) -> TeamView:
    repo = None
    if team.registered_at is not None:
        repo = TeamRepo(
            url=team.repo_url or "",
            provider=team.repo_provider or "",
            default_branch=team.default_branch or "",
            registered_at=team.registered_at,
            late=team.registered_at > event.kickoff_at,
        )
    return TeamView(
        id=team.id,
        name=team.name,
        now=now,
        event=TeamEvent(
            id=event.id,
            name=event.name,
            kickoff_at=event.kickoff_at,
            claims_lock_at=claims_lock_at(event),
            deadline_at=event.deadline_at,
            policy_text=event.policy_text,
        ),
        rules_acknowledged_at=team.rules_acknowledged_at,
        repo=repo,
        suggested_repo_url=None if repo else team.repo_url,
        health_path=team.health_path,
        progress=await _progress(db, team),
        model_door=door_terms(settings),
    )


def door_terms(settings: Settings) -> ModelDoorTerms | None:
    if settings.model_door != "prune":
        return None
    return ModelDoorTerms(
        models=list(settings.door_model_list),
        run_budget_usd=settings.door_run_budget_usd,
        max_calls=settings.door_max_calls,
        outside_hosts=list(settings.door_egress_host_list),
    )


@router.get("/team")
async def read_team(session: TeamUser, request: Request, db: Db) -> TeamView:
    team, event = await own_team(db, session)
    return await _view(db, team, event, request.app.state.clock.now(), request.app.state.settings)


@router.post("/team/rules-ack")
async def acknowledge_rules(session: TeamUser, request: Request, db: Db) -> TeamView:
    team, event = await own_team(db, session)
    now = request.app.state.clock.now()
    if team.rules_acknowledged_at is None:
        team.rules_acknowledged_at = now
        await db.commit()
    return await _view(db, team, event, now, request.app.state.settings)


@router.post("/team/repo")
async def register_repo(body: RepoIn, session: TeamUser, request: Request, db: Db) -> TeamView:
    """Register the team's public repository, checked through the provider.

    Allowed after kickoff for a team that has none yet (it is marked late), but a registered
    repository cannot be swapped once the event has started: the push log is tied to it.
    """
    state = request.app.state
    provider: RepoProvider = state.repo_provider
    team, event = await own_team(db, session)
    now = state.clock.now()
    if team.rules_acknowledged_at is None:
        raise api_error(409, "rules_not_acknowledged", "Acknowledge the event rules first.")
    if team.registered_at is not None and now >= event.kickoff_at:
        raise api_error(409, "repo_locked", "The repository cannot be changed after kickoff.")

    try:
        info = await provider.describe(body.repo_url)
    except InvalidRepoUrl as exc:
        raise api_error(422, "repo_url_invalid", f"That URL cannot be used: {exc}.") from exc
    except RepoNotFound as exc:
        raise api_error(422, "repo_not_found", "No public repository was found there.") from exc
    except InfraError as exc:
        raise api_error(
            503, "repo_provider_unavailable", "The repository host is not answering. Try again."
        ) from exc
    if not info.is_public:
        raise api_error(422, "repo_private", "The repository must be public.")

    taken = await db.execute(
        select(models.Team.id).where(
            models.Team.event_id == team.event_id,
            models.Team.id != team.id,
            models.Team.repo_provider == info.provider,
            models.Team.repository_id == info.id,
            models.Team.registered_at.is_not(None),
        )
    )
    if taken.first() is not None:
        raise api_error(409, "repo_taken", "Another team has registered that repository.")

    team.repo_url = info.url
    team.repo_provider = info.provider
    team.repository_id = info.id
    team.default_branch = info.default_branch
    team.repo_created_at = info.created_at
    team.registered_at = now
    if body.health_path is not None:
        team.health_path = body.health_path
    await db.commit()
    return await _view(db, team, event, now, request.app.state.settings)
