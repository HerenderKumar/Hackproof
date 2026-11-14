"""Invitation tokens and signed session cookies (section 12.4, D-014).

A session is a signed cookie, so reading one needs no database. It carries its own expiry,
checked against the injected clock.
"""

import secrets
import uuid
from datetime import datetime, timedelta
from typing import Literal

from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.responses import Response

from core.hashing import sha256_hex
from core.settings import Settings

Role = Literal["organiser", "judge", "team"]

COOKIE_NAME = "hp_session"
SESSION_SALT = "hackproof.session"


class Session(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Role
    subject_id: uuid.UUID | None = None  # teams.id or judges.id; the organiser has none
    event_id: uuid.UUID | None = None
    link_id: uuid.UUID | None = None  # the invitation link the session came from
    expires_at: datetime


class SessionCodec:
    def __init__(self, secret: str) -> None:
        self._serializer = URLSafeSerializer(secret, salt=SESSION_SALT)

    def encode(self, session: Session) -> str:
        return self._serializer.dumps(session.model_dump(mode="json"))

    def decode(self, cookie: str, now: datetime) -> Session | None:
        """The session in a cookie, or None if it is forged, malformed or expired."""
        try:
            session = Session.model_validate(self._serializer.loads(cookie))
        except (BadSignature, ValidationError):
            return None
        return session if session.expires_at > now else None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """What the database stores. The token itself is shown once and never kept."""
    return sha256_hex(token)


def start_session(
    response: Response, settings: Settings, codec: SessionCodec, now: datetime, **who: object
) -> Session:
    """Issue a session cookie on `response`."""
    lifetime = timedelta(hours=settings.session_ttl_hours)
    session = Session(expires_at=now + lifetime, **who)  # type: ignore[arg-type]
    response.set_cookie(
        COOKIE_NAME,
        codec.encode(session),
        max_age=int(lifetime.total_seconds()),
        path="/",
        httponly=True,
        samesite="lax",
        secure=not settings.is_local,  # plain http only on a developer's localhost
    )
    return session


def end_session(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=not settings.is_local
    )
