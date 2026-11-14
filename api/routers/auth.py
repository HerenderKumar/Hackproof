"""Sign-in and sign-out (section 17)."""

import hmac

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from api.auth import end_session, hash_token, start_session
from api.deps import AnyUser, Db
from api.errors import api_error
from api.schemas import Me, PasswordIn
from core.db import models

router = APIRouter(prefix="/api", tags=["auth"])

# Where each role lands after redeeming its link (section 18).
HOME = {"team": "/team", "judge": "/judge"}


@router.post("/auth/organiser", status_code=204)
async def sign_in_organiser(body: PasswordIn, request: Request, response: Response) -> None:
    state = request.app.state
    expected = state.settings.organiser_password.get_secret_value()
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        raise api_error(401, "invalid_password", "Wrong password.")
    start_session(response, state.settings, state.codec, state.clock.now(), role="organiser")


@router.get("/auth/redeem")
async def redeem(request: Request, db: Db, token: str = "") -> RedirectResponse:
    """Exchange an invitation token for a session, then send the browser to its role's page.

    The link stays usable until it expires or is revoked: judges switch devices (D-014).
    """
    state = request.app.state
    now = state.clock.now()
    link = (
        await db.execute(
            select(models.AccessLink).where(models.AccessLink.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()
    if link is None:
        return _redirect("/redeem?error=invalid")
    if link.revoked_at is not None:
        return _redirect("/redeem?error=revoked")
    if link.expires_at <= now:
        return _redirect("/redeem?error=expired")

    response = _redirect(HOME[link.role])
    start_session(
        response,
        state.settings,
        state.codec,
        now,
        role=link.role,
        subject_id=link.subject_id,
        event_id=link.event_id,
        link_id=link.id,
    )
    return response


def _redirect(path: str) -> RedirectResponse:
    # The request URL holds the token: keep it out of caches and Referer headers.
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    return RedirectResponse(path, status_code=303, headers=headers)


@router.post("/auth/logout", status_code=204)
async def sign_out(_: AnyUser, request: Request, response: Response) -> None:
    end_session(response, request.app.state.settings)


@router.get("/me")
async def me(session: AnyUser, db: Db) -> Me:
    event_id = session.event_id
    if session.role == "organiser":
        # The UI works on one event at a time (section 4.5): the most recent one.
        name = "Organiser"
        event_id = (
            await db.execute(
                select(models.Event.id).order_by(models.Event.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    else:
        model = models.Team if session.role == "team" else models.Judge
        subject = await db.get(model, session.subject_id)
        if subject is None:
            raise api_error(401, "not_signed_in", "Sign in to continue.")
        name = subject.name
    return Me(
        role=session.role,
        subject_id=session.subject_id,
        event_id=event_id,
        name=name,
        expires_at=session.expires_at,
    )
