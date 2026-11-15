"""The `audit` job (sections 7, 11.6, 15.2, 15.3 and 16): after a final submission, a budgeted
model audit of the pinned archive, its citations checked by code, and a code map for the judge.

The job reads the submission's snapshot, never the live repository: the archive is extracted
into `ARTIFACT_DIR/work/<attempt_id>/src`, and that folder is deleted however the job ends. The
model reads the claims version the submission pinned, not a later one. Graphify runs after the
audit, so its output folder is never among the files the model lists, and its failure is
recorded in the code map, never raised: the brief renders from the audit alone.

One `audits` row per attempt. The table has no unique key on the attempt, so an attempt that
already stored its row is found before the model is called (to spend nothing) and again inside
the writing transaction, under the job row's lock. Gateway failures (`InfraError`,
`ReplayMissing`) propagate, so the job loop retries the job. A budget hit completes the job
with `error_class = 'budget'` and a row with `partial = true` (section 11.3).
"""

import asyncio
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import queue
from core.archive_tools import ArchiveTools
from core.audit import AuditBudget, AuditInput, AuditRun, run_audit
from core.citations import ValidatedAudit, validate_audit
from core.claims import claim_fields
from core.codemap import Graph, GraphError, Status, code_map, run_graphify
from core.db import models
from core.db.engine import Sessions
from core.llm import LLMClient
from core.logging import get_logger
from core.settings import Settings
from core.snapshots import extract_archive
from worker.lanes import Handler, Outcome

log = get_logger()

Graphify = Callable[[Path], Awaitable[Graph]]


@dataclass(frozen=True)
class _Found:
    submission_id: UUID
    snapshot: models.Snapshot
    given: AuditInput
    ui_specs: dict[str, dict[str, Any]]  # each UI claim's newest test, for the code map only


@dataclass(frozen=True)
class Audited:
    ran: AuditRun
    validated: ValidatedAudit
    code_map: dict[str, Any]
    code_map_status: Status


def budget_of(settings: Settings) -> AuditBudget:
    return AuditBudget(
        max_steps=settings.audit_max_steps,
        max_tokens=settings.audit_max_tokens,
        timeout_s=float(settings.audit_timeout_s),
    )


async def audit_folder(
    client: LLMClient,
    src: Path,
    given: AuditInput,
    *,
    model: str,
    budget: AuditBudget,
    graphify: Graphify = run_graphify,
    job_id: UUID | None = None,
    ui_specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> Audited:
    """Audit an extracted snapshot, keep the citations that hold, then map the code.

    The job and `make audits` both go through here. Graphify runs last: it writes into `src`.
    """
    tools = ArchiveTools(src)
    ran = await run_audit(client, tools, given, model=model, budget=budget, job_id=job_id)
    validated = await asyncio.to_thread(
        validate_audit,
        ran.output,
        tools,
        [claim["id"] for claim in given.claims],
        partial=ran.partial,
        failure=ran.failure,
        steps=ran.steps,
        tokens=ran.tokens,
        model=model,
    )
    graph, graph_error = await _graph(graphify, src)
    mapped, status = await asyncio.to_thread(
        code_map, graph, graph_error, tools, given.claims, validated.content, ui_specs
    )
    return Audited(ran, validated, mapped, status)


def make_audit_handler(
    sessions: Sessions,
    client: LLMClient,
    settings: Settings,
    *,
    graphify: Graphify = run_graphify,
) -> Handler:
    budget = budget_of(settings)

    async def audit(job: queue.Job) -> Outcome | None:
        found = await _load(sessions, job)
        if isinstance(found, Outcome):
            return found
        async with sessions() as session:
            if await _stored(session, job):
                log.info("audit_skipped", reason="this attempt already stored its audit")
                return None

        work = settings.artifact_dir / "work" / str(job.attempt_id)
        src = work / "src"
        try:
            snapshot = found.snapshot
            await asyncio.to_thread(
                extract_archive, Path(snapshot.archive_path), snapshot.archive_sha256, src
            )
            done = await audit_folder(
                client,
                src,
                found.given,
                model=settings.audit_model,
                budget=budget,
                graphify=graphify,
                job_id=job.id,
                ui_specs=found.ui_specs,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, work, ignore_errors=True)

        ran, validated = done.ran, done.validated
        row = models.Audit(
            submission_id=found.submission_id,
            job_id=job.id,
            attempt_id=job.attempt_id,
            content=validated.content,
            citations_dropped=validated.citations_dropped,
            partial=ran.partial,
            code_map=done.code_map,
            code_map_status=done.code_map_status,
        )
        if not await _store(sessions, job, row):
            log.info("audit_skipped", reason="this attempt already stored its audit")
            return None
        content = validated.content
        log.info(
            "audit_stored",
            steps=ran.steps,
            tokens=ran.tokens,
            partial=ran.partial,
            failure=ran.failure,
            claims={c["claim_id"]: c["implementation"] for c in content["claims"]},
            missing_claims=content["missing_claims"],
            citations=sum(len(c["citations"]) for c in content["claims"]),
            citations_dropped=validated.citations_dropped,
            hints=len(content["text_hints"]),
            code_map_status=done.code_map_status,
        )
        if ran.partial:
            detail = ran.failure or f"the audit used up its budget after {ran.steps} steps"
            return Outcome("budget", detail)
        return None

    return audit


async def _load(sessions: Sessions, job: queue.Job) -> _Found | Outcome:
    """The submission's snapshot, and what the model is told: the event, the team and the
    claims version the submission pinned."""
    async with sessions() as session:
        submission = None
        if job.submission_id is not None:
            submission = await session.get(models.Submission, job.submission_id)
        if submission is None:
            return Outcome("validation", "audit needs a submission")
        snapshot = await session.get(models.Snapshot, submission.snapshot_id)
        team = await session.get(models.Team, submission.team_id)
        event = await session.get(models.Event, team.event_id) if team is not None else None
        claims = await session.scalar(
            select(models.Claims).where(
                models.Claims.team_id == submission.team_id,
                models.Claims.version == submission.claims_version,
            )
        )
        ui_rows = await session.scalars(
            select(models.TestSpec)
            .where(
                models.TestSpec.team_id == submission.team_id,
                models.TestSpec.claims_version == submission.claims_version,
                models.TestSpec.kind == "ui",
            )
            .order_by(models.TestSpec.version)
        )
        # The newest UI test of each claim: its names anchor the claim in the code map. The
        # model never sees a test (D-047 point 7); this only places the claim in the code.
        ui_specs = {row.claim_id: {"steps": row.steps} for row in ui_rows if row.claim_id}
    if snapshot is None or team is None or event is None:
        return Outcome("validation", "audit needs the submission's snapshot, team and event")
    if claims is None:
        return Outcome("validation", f"the team has no claims version {submission.claims_version}")
    given = AuditInput(
        event_name=event.name,
        kickoff_at=event.kickoff_at,
        team_name=team.name,
        problem_statement=claims.problem_statement,
        claims=[claim_fields(item) for item in claims.items],
    )
    return _Found(submission.id, snapshot, given, ui_specs)


async def _graph(graphify: Graphify, src: Path) -> tuple[Graph | None, str | None]:
    """Graphify's graph, or why there is none. Never raises: the audit is already paid for."""
    try:
        return await graphify(src), None
    except GraphError as exc:
        detail = str(exc)
        log.warning("code_map_without_graph", detail=detail)
    except Exception as exc:
        detail = f"graphify failed: {type(exc).__name__}: {exc}"
        log.warning("code_map_without_graph", detail=detail, exc_info=True)
    return None, detail


async def _stored(session: AsyncSession, job: queue.Job) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(models.Audit)
        .where(models.Audit.job_id == job.id, models.Audit.attempt_id == job.attempt_id)
    )
    return bool(count)


async def _store(sessions: Sessions, job: queue.Job, row: models.Audit) -> bool:
    """Write the row unless this attempt already did. False when it had."""
    async with sessions.begin() as session:
        await session.execute(
            select(models.Job.id).where(models.Job.id == job.id).with_for_update()
        )
        if await _stored(session, job):
            return False
        session.add(row)
    return True
