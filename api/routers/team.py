"""The team's own data (section 5.2). Every query starts from the session's team ID."""

from fastapi import APIRouter, Request
from sqlalchemy import select

from api.auth import Session
from api.deps import Db, TeamUser
from api.errors import api_error
from api.schemas import RepoIn, TeamEvent, TeamRepo, TeamView
from core.db import models
from core.errors import InfraError
from core.repo import InvalidRepoUrl, RepoNotFound, RepoProvider

router = APIRouter(prefix="/api", tags=["team"])


async def _own(db: Db, session: Session) -> tuple[models.Team, models.Event]:
    """The signed-in team and its event. No route takes a team ID from the request."""
    team = await db.get(models.Team, session.subject_id)
    event = await db.get(models.Event, team.event_id) if team is not None else None
    if team is None or event is None:
        raise api_error(401, "not_signed_in", "Sign in to continue.")
    return team, event


def _view(team: models.Team, event: models.Event) -> TeamView:
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
        event=TeamEvent.model_validate(event),
        rules_acknowledged_at=team.rules_acknowledged_at,
        repo=repo,
        suggested_repo_url=None if repo else team.repo_url,
        health_path=team.health_path,
    )


@router.get("/team")
async def read_team(session: TeamUser, db: Db) -> TeamView:
    return _view(*await _own(db, session))


@router.post("/team/rules-ack")
async def acknowledge_rules(session: TeamUser, request: Request, db: Db) -> TeamView:
    team, event = await _own(db, session)
    if team.rules_acknowledged_at is None:
        team.rules_acknowledged_at = request.app.state.clock.now()
        await db.commit()
    return _view(team, event)


@router.post("/team/repo")
async def register_repo(body: RepoIn, session: TeamUser, request: Request, db: Db) -> TeamView:
    """Register the team's public repository, checked through the provider.

    Allowed after kickoff for a team that has none yet (it is marked late), but a registered
    repository cannot be swapped once the event has started: the push log is tied to it.
    """
    state = request.app.state
    provider: RepoProvider = state.repo_provider
    team, event = await _own(db, session)
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
    return _view(team, event)
