"""Which specs a run carries (M5 design, section 5).

Specs belong to a team's claims version, not to a snapshot: they are drafted from the team's
text before any code exists (D-047). A final run carries exactly the specs its submission
pinned: for each claim, the latest version approved with `approved_spec_hash = spec_hash` for the
submission's team and claims version, whatever its status is now (a later claims edit may have
superseded it). If those no longer reproduce the submission's `test_spec_hash`, the run carries
nothing.

A self-check carries the specs its job lists, or else the team's current ones.

Every spec is cut to its run's mode (`for_mode`), so hidden cases never reach a practice run's
folder, results or logs. Specs that cannot run are listed with the reason, and each gets an
`unverified` row. Startup rows are never part of this: the startup check is made by code.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import models
from core.specs import Mode, for_mode, test_spec_hash
from worker.lanes import Outcome

NOT_APPROVED = "the test was not approved"


@dataclass(frozen=True)
class NotRunnable:
    spec_id: UUID
    claim_id: str
    reason: str


@dataclass(frozen=True)
class RunPlan:
    mode: Mode
    runnable: list[dict[str, Any]]  # spec documents cut to `mode`, each with its "spec_id"
    not_runnable: list[NotRunnable]


def spec_document(row: models.TestSpec) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "claim_id": row.claim_id,
        "claim_type": row.claim_type,
        "steps": row.steps,
        "practice_cases": row.practice_cases,
        "final_cases": row.final_cases,
    }


async def final_run_plan(session: AsyncSession, submission: models.Submission) -> RunPlan | Outcome:
    rows = await _behaviour_specs(
        session,
        models.TestSpec.team_id == submission.team_id,
        models.TestSpec.claims_version == submission.claims_version,
    )
    approved = _latest_per_claim(
        row
        for row in rows
        if row.approved_at is not None and row.approved_spec_hash == row.spec_hash
    )
    if test_spec_hash(row.spec_hash for row in approved) != submission.test_spec_hash:
        return Outcome("validation", "the approved tests do not match the submission")

    pinned_claims = {row.claim_id for row in approved}
    unapproved = _latest_per_claim(row for row in rows if row.claim_id not in pinned_claims)
    return RunPlan(
        "final",
        [_cut(row, "final") for row in approved],
        [NotRunnable(row.id, row.claim_id or "", _reason(row)) for row in unapproved],
    )


async def self_check_plan(
    session: AsyncSession, team_id: UUID, spec_ids: list[UUID] | None
) -> RunPlan:
    if spec_ids:
        rows = await _behaviour_specs(
            session,
            models.TestSpec.team_id == team_id,
            models.TestSpec.id.in_(spec_ids),
        )
    else:
        live = [
            row
            for row in await _behaviour_specs(session, models.TestSpec.team_id == team_id)
            if row.status != "superseded"
        ]
        newest = max((row.claims_version for row in live), default=None)
        rows = _latest_per_claim(row for row in live if row.claims_version == newest)

    runnable = [row for row in rows if row.status in ("draft", "approved")]
    blocked = [row for row in rows if row.status not in ("draft", "approved")]
    return RunPlan(
        "practice",
        [_cut(row, "practice") for row in runnable],
        [NotRunnable(row.id, row.claim_id or "", _reason(row)) for row in blocked],
    )


# --- helpers -------------------------------------------------------------------------------------


async def _behaviour_specs(session: AsyncSession, *where: Any) -> list[models.TestSpec]:
    found = await session.scalars(
        select(models.TestSpec)
        .where(models.TestSpec.kind != "startup", *where)
        .order_by(models.TestSpec.claim_id, models.TestSpec.version)
    )
    return list(found.all())


def _latest_per_claim(rows: Iterable[models.TestSpec]) -> list[models.TestSpec]:
    latest: dict[str | None, models.TestSpec] = {}
    for row in rows:
        current = latest.get(row.claim_id)
        if current is None or row.version > current.version:
            latest[row.claim_id] = row
    return sorted(latest.values(), key=lambda row: row.claim_id or "")


def _cut(row: models.TestSpec, mode: Mode) -> dict[str, Any]:
    return for_mode(spec_document(row), mode) | {"spec_id": str(row.id)}


def _reason(row: models.TestSpec) -> str:
    if row.status == "needs_clarification" and row.clarification_reason:
        return row.clarification_reason
    if row.status == "superseded":
        return "the test was replaced by a newer version"
    return NOT_APPROVED
