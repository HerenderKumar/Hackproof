"""What judges and the organiser read about a team's submission (sections 7 and 17).

The submission that counts is the team's latest made by the deadline. A brief is the stored
facts (`core.brief_data`) plus the written part: the model's when its row belongs to the brief
job's finished attempt and was usable, otherwise template questions made on the spot, so a
brief renders whatever happened to the AI calls (section 7's degradation rule).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.brief import raw_content
from core.brief_data import current_brief, job_state, latest_job, load_facts
from core.db import models


async def judged_submission(
    session: AsyncSession, team: models.Team, event: models.Event
) -> models.Submission | None:
    found: models.Submission | None = await session.scalar(
        select(models.Submission)
        .where(
            models.Submission.team_id == team.id,
            models.Submission.submitted_at <= event.deadline_at,
        )
        .order_by(models.Submission.submitted_at.desc(), models.Submission.id.desc())
        .limit(1)
    )
    return found


async def brief_status(session: AsyncSession, submission_id: uuid.UUID) -> str:
    """`ok`, `failed` (the page shows raw results), `pending` or `none`."""
    row = await current_brief(session, submission_id)
    if row is not None:
        return "ok" if row.ai_status == "ok" else "failed"
    state = job_state(await latest_job(session, "brief", submission_id))
    return "failed" if state == "done" else state


async def assemble_brief(session: AsyncSession, submission_id: uuid.UUID) -> dict[str, Any] | None:
    facts = await load_facts(session, submission_id)
    if facts is None:
        return None
    row = await current_brief(session, submission_id)
    status = await brief_status(session, submission_id)
    written = row.content if row is not None and row.ai_status == "ok" else raw_content(facts)
    return {**facts, "written": written, "ai_status": status}


def totals(rubric: list[dict[str, Any]], scores: dict[str, int]) -> int:
    """A judge's total over the criteria the rubric has now. Computed in code, never stored."""
    return sum(scores.get(str(criterion["id"]), 0) for criterion in rubric)
