"""Organiser set-up: the event, team import, judges and invitation links (section 5.1)."""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.auth import hash_token, new_token
from api.csv_import import MAX_BYTES, parse_teams_csv
from api.deps import Db, Organiser
from api.errors import api_error
from api.schemas import (
    EventIn,
    EventOut,
    EventPatch,
    ImportedTeam,
    ImportResult,
    JudgeIn,
    JudgeOut,
    LinkIn,
    LinkOut,
    SkippedRow,
)
from core.db import models

router = APIRouter(prefix="/api", tags=["organiser"])


async def _event(db: Db, event_id: uuid.UUID) -> models.Event:
    event = await db.get(models.Event, event_id)
    if event is None:
        raise api_error(404, "event_not_found", "There is no event with that ID.")
    return event


@router.post("/events", status_code=201)
async def create_event(body: EventIn, _: Organiser, db: Db) -> EventOut:
    event = models.Event(**body.model_dump(mode="python"))
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return EventOut.model_validate(event)


@router.get("/events/{event_id}")
async def read_event(event_id: uuid.UUID, _: Organiser, db: Db) -> EventOut:
    return EventOut.model_validate(await _event(db, event_id))


@router.patch("/events/{event_id}")
async def edit_event(event_id: uuid.UUID, body: EventPatch, _: Organiser, db: Db) -> EventOut:
    event = await _event(db, event_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True, mode="python")
    current = EventOut.model_validate(event).model_dump(include=set(EventIn.model_fields))
    try:
        merged = EventIn.model_validate({**current, **changes})
    except ValueError as exc:
        raise api_error(422, "event_invalid", str(exc)) from exc

    rubric = merged.model_dump()["rubric"]
    if rubric != event.rubric:
        if event.scores_locked:
            raise api_error(409, "scores_locked", "The rubric is frozen once scores are locked.")
        # Submissions record the rubric version they were made under.
        event.rubric_version += 1
    for field, value in merged.model_dump(mode="python").items():
        setattr(event, field, value)
    await db.commit()
    await db.refresh(event)
    return EventOut.model_validate(event)


@router.post("/events/{event_id}/teams/import")
async def import_teams(event_id: uuid.UUID, file: UploadFile, _: Organiser, db: Db) -> ImportResult:
    """Create a team for each new name in the CSV. A file with any bad row imports nothing."""
    await _event(db, event_id)
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise api_error(413, "file_too_large", "The CSV is larger than 1 MB.")
    rows, errors = parse_teams_csv(data)
    if errors:
        raise api_error(
            422,
            "csv_invalid",
            "The CSV has problems; nothing was imported.",
            errors=[{"line": e.line, "message": e.message} for e in errors],
        )

    names = await db.execute(select(models.Team.name).where(models.Team.event_id == event_id))
    existing = {name.casefold() for name in names.scalars()}
    created: list[models.Team] = []
    skipped: list[SkippedRow] = []
    for row in rows:
        if row.name.casefold() in existing:
            skipped.append(SkippedRow(line=row.line, team_name=row.name, reason="already exists"))
            continue
        team = models.Team(
            event_id=event_id,
            name=row.name,
            contact_email=row.contact_email,
            repo_url=row.repo_url,  # a suggestion until the team registers it (D-038)
        )
        db.add(team)
        created.append(team)
    try:
        await db.commit()
    except IntegrityError as exc:
        raise api_error(409, "import_conflict", "Another import ran at the same time.") from exc
    return ImportResult(
        created=[ImportedTeam.model_validate(team) for team in created], skipped=skipped
    )


@router.post("/events/{event_id}/judges", status_code=201)
async def add_judge(event_id: uuid.UUID, body: JudgeIn, _: Organiser, db: Db) -> JudgeOut:
    await _event(db, event_id)
    judge = models.Judge(event_id=event_id, name=body.name, email=body.email or None)
    db.add(judge)
    await db.commit()
    return JudgeOut.model_validate(judge)


@router.post("/events/{event_id}/links", status_code=201)
async def create_link(
    event_id: uuid.UUID, body: LinkIn, _: Organiser, request: Request, db: Db
) -> LinkOut:
    """Issue an invitation link. Its URL is in this response and nowhere else."""
    state = request.app.state
    await _event(db, event_id)
    model = models.Team if body.role == "team" else models.Judge
    subject = await db.get(model, body.subject_id)
    if subject is None or subject.event_id != event_id:
        raise api_error(404, "subject_not_found", f"This event has no such {body.role}.")

    token = new_token()
    days = body.expires_in_days or state.settings.link_ttl_days
    link = models.AccessLink(
        event_id=event_id,
        role=body.role,
        subject_id=body.subject_id,
        token_hash=hash_token(token),
        expires_at=state.clock.now() + timedelta(days=days),
    )
    db.add(link)
    await db.commit()
    base = state.settings.public_base_url.rstrip("/")
    return LinkOut(
        id=link.id,
        role=body.role,
        subject_id=body.subject_id,
        url=f"{base}/redeem?token={token}",
        expires_at=link.expires_at,
    )


@router.delete("/links/{link_id}", status_code=204)
async def revoke_link(link_id: uuid.UUID, _: Organiser, request: Request, db: Db) -> Response:
    link = await db.get(models.AccessLink, link_id)
    if link is None:
        raise api_error(404, "link_not_found", "There is no link with that ID.")
    if link.revoked_at is None:
        link.revoked_at = request.app.state.clock.now()
        await db.commit()
    return Response(status_code=204)
