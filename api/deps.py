"""Request dependencies: the database session and the one role guard (section 12.4).

Every route under `/api` names the roles it allows by depending on `Organiser`, `TeamUser`,
`JudgeUser`, `Reader` or `AnyUser`. A test walks the route table and fails if one does not.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import COOKIE_NAME, Role, Session
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
