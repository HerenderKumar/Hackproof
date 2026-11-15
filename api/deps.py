"""Request dependencies: the database session and the one role guard (section 12.4).

Every route under `/api` names the roles it allows by depending on `Organiser`, `TeamUser`,
`JudgeUser`, `Reader` or `AnyUser`. A test walks the route table and fails if one does not.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import COOKIE_NAME, Role, Session, hash_token
from api.errors import api_error
from core.db import models


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """A session for one request. Handlers that write call `commit()` before they return."""
    async with request.app.state.sessions() as session:
        yield session


Db = Annotated[AsyncSession, Depends(get_db)]


def require(*roles: Role) -> Callable[[Request, AsyncSession], Awaitable[Session]]:
    """A dependency that admits only the given roles and returns the caller's session."""
    allowed = frozenset(roles)

    async def guard(request: Request, db: Db) -> Session:
        state = request.app.state
        cookie = request.cookies.get(COOKIE_NAME, "")
        session: Session | None = state.codec.decode(cookie, state.clock.now())
        if session is None:
            raise api_error(401, "not_signed_in", "Sign in to continue.")
        if session.link_id is not None:
            # A revoked invitation ends the sessions it started, not only future sign-ins.
            link = await db.get(models.AccessLink, session.link_id)
            if link is None or link.revoked_at is not None:
                raise api_error(401, "link_revoked", "This invitation link has been revoked.")
        if session.role not in allowed:
            raise api_error(403, "forbidden", "Your role cannot do this.")
        return session

    guard.allowed_roles = allowed  # type: ignore[attr-defined]
    return guard


Organiser = Annotated[Session, Depends(require("organiser"))]
TeamUser = Annotated[Session, Depends(require("team"))]
JudgeUser = Annotated[Session, Depends(require("judge"))]
Reader = Annotated[Session, Depends(require("organiser", "judge"))]
AnyUser = Annotated[Session, Depends(require("organiser", "judge", "team"))]


# --- the kickoff scanner (M9) ----------------------------------------------------------------


@dataclass(frozen=True)
class Device:
    """A device reached by its own code: the scanner holds no session."""

    device: models.BaselineDevice
    team: models.Team
    event: models.Event


async def device_from_code(db: Db, x_device_code: Annotated[str | None, Header()] = None) -> Device:
    """The device whose code is in `X-Device-Code`. A scanner route reaches only that device's
    own baseline; the code is never a session and opens no other route."""
    if not x_device_code or len(x_device_code) > 200:
        raise api_error(401, "bad_device_code", "This device code is not valid.")
    device = await db.scalar(
        select(models.BaselineDevice).where(
            models.BaselineDevice.code_hash == hash_token(x_device_code)
        )
    )
    team = await db.get(models.Team, device.team_id) if device is not None else None
    event = await db.get(models.Event, team.event_id) if team is not None else None
    if device is None or team is None or event is None:
        raise api_error(401, "bad_device_code", "This device code is not valid.")
    return Device(device, team, event)


DeviceAuth = Annotated[Device, Depends(device_from_code)]
