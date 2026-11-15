"""Everything a brief shows that comes from stored rows (section 7): the facts.

Read at request time by the API and once by the `brief` job, so the page and the model see the
same thing. Verdicts come from `core.results`; an audit or a written part counts only when its
row belongs to its job's finished attempt (a stale attempt can leave a row behind, D-053).
Final cases are in here, so this is for judges and organisers, never for a team. A UI case
carries its steps in words, what the page held when it did not pass, and whether a video and a
trace were kept; the files themselves are served by `api/media.py`.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.claims import claim_fields
from core.db import models
from core.results import current_test_results, final_run_job, submission_verdicts
from core.signals import describe, growth, signals

BODY_CHARS = 2000


async def latest_job(session: AsyncSession, kind: str, submission_id: UUID) -> models.Job | None:
    found = await session.scalars(
        select(models.Job)
        .where(models.Job.kind == kind, models.Job.submission_id == submission_id)
        .order_by(models.Job.created_at.desc())
        .limit(1)
    )
    return found.first()


async def current_audit(session: AsyncSession, submission_id: UUID) -> models.Audit | None:
    job = await latest_job(session, "audit", submission_id)
    if job is None or job.status != "done":
        return None
    row: models.Audit | None = await session.scalar(
        select(models.Audit).where(
            models.Audit.job_id == job.id, models.Audit.attempt_id == job.attempt_id
        )
    )
    return row


async def current_brief(session: AsyncSession, submission_id: UUID) -> models.Brief | None:
    job = await latest_job(session, "brief", submission_id)
    if job is None or job.status != "done":
        return None
    row: models.Brief | None = await session.scalar(
        select(models.Brief).where(
            models.Brief.job_id == job.id, models.Brief.attempt_id == job.attempt_id
        )
    )
    return row


def job_state(job: models.Job | None) -> str:
    """`none`, `pending` (queued or running), `done` or `failed`."""
    if job is None:
        return "none"
    if job.status in ("done", "failed"):
        return str(job.status)
    return "pending"


def citation_url(team: models.Team, commit_sha: str, citation: dict[str, Any]) -> str | None:
    """The cited lines on GitHub at the pinned commit; None for another provider."""
    if team.repo_provider != "github" or not team.repo_url:
        return None
    base = team.repo_url.removesuffix(".git").rstrip("/")
    lines = f"#L{citation['start_line']}-L{citation['end_line']}"
    return f"{base}/blob/{commit_sha}/{citation['path']}{lines}"


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > BODY_CHARS:
        return value[:BODY_CHARS] + "…"
    return value


_STEP_FIELDS = ("id", "words", "status", "expected", "actual", "message")
OUTLINE_CHARS = 4000


def ui_steps(actual: Any) -> list[dict[str, Any]]:
    """A UI case's steps as sentences with how each ended (M11 design, part 8)."""
    steps = actual.get("steps", []) if isinstance(actual, dict) else []
    return [
        {key: _clip(step.get(key)) for key in _STEP_FIELDS if key in step}
        for step in steps
        if isinstance(step, dict)
    ]


def ui_page(actual: Any, outline_chars: int = OUTLINE_CHARS) -> dict[str, Any] | None:
    """What the page held and noted when a UI case did not pass: never sent to a model."""
    page = actual.get("page") if isinstance(actual, dict) else None
    if not isinstance(page, dict):
        return None
    kept = {key: value for key, value in page.items() if key != "outline" and value}
    outline = page.get("outline")
    if isinstance(outline, str) and outline:
        kept["outline"] = outline[:outline_chars]
    return kept or None


def _ui_case(row: models.TestResult) -> dict[str, Any]:
    case = row.expected if isinstance(row.expected, dict) else {}
    assertions = [a for a in (row.assertion_results or []) if isinstance(a, dict)]
    return {
        "result_id": str(row.id),
        "kind": "ui",
        "case_id": row.case_id,
        "verdict": row.verdict,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "input": case.get("input"),
        "expected": case.get("expected"),
        "request": None,
        "response": None,
        "steps": ui_steps(row.actual),
        "page": ui_page(row.actual),
        "media": {"video": bool(row.video_path), "trace": bool(row.trace_path)},
        "assertions_total": len(assertions),
        "failed": [a for a in assertions if not a.get("passed")],
    }


def _case(row: models.TestResult, kind: str = "api") -> dict[str, Any]:
    """One final case as evidence: its input, the step that decided it and what failed."""
    if kind == "ui":
        return _ui_case(row)
    case = row.expected if isinstance(row.expected, dict) else {}
    steps = (row.actual or {}).get("steps", []) if isinstance(row.actual, dict) else []
    ran = [step for step in steps if isinstance(step, dict) and step.get("request")]
    decided = next(
        (s for s in ran if any(not a.get("passed") for a in s.get("assertions", []))),
        ran[-1] if ran else None,
    )
    assertions = [a for a in (row.assertion_results or []) if isinstance(a, dict)]
    response = (decided or {}).get("response")
    if isinstance(response, dict):
        response = {key: _clip(value) for key, value in response.items()}
    return {
        "result_id": str(row.id),
        "case_id": row.case_id,
        "verdict": row.verdict,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "input": case.get("input"),
        "expected": case.get("expected"),
        "request": (decided or {}).get("request"),
        "response": response,
        "assertions_total": len(assertions),
        "failed": [a for a in assertions if not a.get("passed")],
    }


def _model_use(use: Any) -> dict[str, Any] | None:
    if not isinstance(use, dict):
        return None
    outside = use.get("outside") or {}
    return {
        "calls": use.get("calls", 0),
        "calls_ok": use.get("calls_ok", 0),
        "refused": use.get("refused", []),
        "models": use.get("models", {}),
        "outside_hosts": outside.get("hosts", {}),
        "outside_refused": outside.get("refused", 0),
        "receipt_verified": use.get("receipt_verified"),
    }


async def load_facts(session: AsyncSession, submission_id: UUID) -> dict[str, Any] | None:
    submission = await session.get(models.Submission, submission_id)
    if submission is None:
        return None
    team = await session.get(models.Team, submission.team_id)
    snapshot = await session.get(models.Snapshot, submission.snapshot_id)
    if team is None or snapshot is None:
        return None
    event = await session.get(models.Event, team.event_id)
    if event is None:
        return None
    claims = await session.scalar(
        select(models.Claims).where(
            models.Claims.team_id == team.id, models.Claims.version == submission.claims_version
        )
    )
    items = [claim_fields(item) for item in (claims.items if claims is not None else [])]

    verdicts = await submission_verdicts(session, submission.id)
    run_job = await final_run_job(session, submission.id)
    rows = await current_test_results(session, run_job.id) if run_job is not None else []
    specs: dict[UUID, models.TestSpec] = {}
    if rows:
        found = await session.scalars(
            select(models.TestSpec).where(models.TestSpec.id.in_({row.spec_id for row in rows}))
        )
        specs = {spec.id: spec for spec in found}
    types = dict(
        (
            await session.execute(
                select(models.TestSpec.claim_id, models.TestSpec.claim_type)
                .where(
                    models.TestSpec.team_id == team.id,
                    models.TestSpec.claims_version == submission.claims_version,
                )
                .order_by(models.TestSpec.version)
            )
        ).all()
    )
    run_row = None
    if run_job is not None and run_job.status == "done":
        run_row = await session.scalar(
            select(models.RunResult).where(
                models.RunResult.job_id == run_job.id,
                models.RunResult.attempt_id == run_job.attempt_id,
            )
        )

    audit_job = await latest_job(session, "audit", submission.id)
    audit = await current_audit(session, submission.id)
    content = audit.content if audit is not None else {}
    audited = {entry["claim_id"]: entry for entry in content.get("claims", [])}
    mapped = (audit.code_map or {}).get("claims", {}) if audit is not None else {}

    claim_cards = []
    for item in items:
        claim_id = item["id"]
        cases = [
            _case(row, spec.kind)
            for row in sorted(rows, key=lambda r: (str(r.spec_id), r.case_id or ""))
            if (spec := specs.get(row.spec_id)) is not None and spec.claim_id == claim_id
        ]
        entry = audited.get(claim_id)
        card_audit = None
        if entry is not None:
            card_audit = {
                "implementation": entry.get("implementation"),
                "explanation": entry.get("explanation"),
                "citations": [
                    {**c, "url": citation_url(team, snapshot.commit_sha, c)}
                    for c in entry.get("citations", [])
                ],
            }
        claim_cards.append(
            {
                "id": claim_id,
                "text": item["text"],
                "target": item["target"],
                "type": types.get(claim_id),
                "verdict": verdicts.get(claim_id, "unverified"),
                "cases": cases,
                "audit": card_audit,
                "code_map": mapped.get(claim_id),
            }
        )

    observed = list(
        await session.scalars(
            select(models.RepoObservation)
            .where(models.RepoObservation.team_id == team.id)
            .order_by(models.RepoObservation.observed_at, models.RepoObservation.id)
        )
    )
    observations = [
        {
            "kind": o.kind,
            "observed_at": o.observed_at.isoformat(),
            "head_sha": o.head_sha,
            "previous_head_sha": o.previous_head_sha,
            "lines_added": o.lines_added,
            "lines_total": o.lines_total,
            "descends_from_previous": o.descends_from_previous,
        }
        for o in observed
    ]
    # Context for a judge's questions, never a verdict (invariant 5): nothing reads these back
    # into a score or a test result.
    found_signals = [
        {
            "kind": signal.name,
            "detail": describe(signal),
            "observed_at": signal.detail.get("observed_at"),
            "facts": signal.detail,
        }
        for signal in signals(
            kickoff_at=event.kickoff_at,
            bulk_push_lines=event.bulk_push_lines,
            bulk_push_window_minutes=event.bulk_push_window_minutes,
            repo_created_at=team.repo_created_at,
            observations=observed,
        )
    ]
    growth_points = [
        {"observed_at": when.isoformat(), "lines": lines} for when, lines in growth(observed)
    ]

    audit_status = job_state(audit_job)
    if audit is not None:
        audit_status = "partial" if audit.partial else "ok"
    elif audit_status == "done":
        audit_status = "failed"
    return {
        "submission": {
            "id": str(submission.id),
            "team_id": str(team.id),
            "team_name": team.name,
            "event_id": str(event.id),
            "mode": submission.mode,
            "commit_sha": snapshot.commit_sha,
            "archive_sha256": snapshot.archive_sha256,
            "commit_url": (
                f"{team.repo_url.removesuffix('.git').rstrip('/')}/tree/{snapshot.commit_sha}"
                if team.repo_provider == "github" and team.repo_url
                else None
            ),
            "submitted_at": submission.submitted_at.isoformat(),
            "claims_version": submission.claims_version,
            "receipt_hash": submission.receipt_hash,
        },
        "run": {
            "status": job_state(run_job),
            "error": run_job.error_detail if run_job is not None else None,
            "build_status": run_row.build_status if run_row is not None else None,
            "startup_status": run_row.startup_status if run_row is not None else None,
            "startup_detail": run_row.startup_detail if run_row is not None else None,
            "image_digest": run_row.image_digest if run_row is not None else None,
            # The model door's summary (Prune design): context for a question, never a verdict.
            "model_use": _model_use(run_row.model_use) if run_row is not None else None,
        },
        "claims": claim_cards,
        "audit": {
            "status": audit_status,
            "failure": content.get("failure"),
            "text_hints": content.get("text_hints", []),
            "citations_dropped": audit.citations_dropped if audit is not None else 0,
            "code_map_status": audit.code_map_status if audit is not None else None,
        },
        "observations": observations,
        "growth": growth_points,
        "kickoff_at": event.kickoff_at.isoformat(),
        "signals": found_signals,
        "reuse_declaration": submission.reuse_declaration,
    }
