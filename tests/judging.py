"""A judged submission planted in the database: a final run with results, an audit and, when
asked, a written brief. For the judge's, the organiser's and the brief job's tests."""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from core import queue
from core.db import models
from tests.factories import T0, make_claims, make_snapshot, make_submission, make_test_spec
from tests.helpers import Sessions, claimed

FAILED = {
    "op": "sum_equals",
    "path": "$.shares[*]",
    "expected": "100.0",
    "actual": "99.99",
    "passed": False,
    "note": None,
    "step": "s1",
}
PASSED = {**FAILED, "op": "status", "path": None, "expected": 200, "actual": 200, "passed": True}
REQUEST = {"method": "POST", "path": "/split", "query": {}, "headers": {}, "json": {"total": 100}}
RESPONSE = {"status": 200, "body": {"shares": [33.33, 33.33, 33.33]}}
CITATION = {"id": "c1-1", "path": "server.js", "start_line": 4, "end_line": 9, "why": "the split"}
WRITTEN = {
    "summary": "A bill splitter whose shares miss the total by a paisa.",
    "claims": {"c1": "Three shares of 33.33 add up to 99.99, not 100."},
    "demo_questions": [
        {"question": "Where does the missing paisa go?", "ref": {"kind": "citation", "id": "c1-1"}},
        {"question": "Why 99.99?", "ref": {"kind": "citation", "id": "c1-1"}},
    ],
    "questions_from": "model",
}


@dataclass
class Judged:
    team_id: uuid.UUID
    submission_id: uuid.UUID
    result_ids: dict[str, uuid.UUID]
    final_run_id: uuid.UUID
    audit_id: uuid.UUID


async def _done(sessions: Sessions, job: queue.Job, *rows: Any) -> None:
    async with sessions.begin() as session:
        session.add_all(rows)
        await session.flush()
        assert await queue.complete(session, job.id, job.attempt_id)


async def judged(
    sessions: Sessions,
    team_id: uuid.UUID | str,
    *,
    audit: bool = True,
    written: dict[str, Any] | None = None,
    submitted_at: Any = None,
    with_claims: bool = True,
) -> Judged:
    """c1 fails with evidence, c2 passes; the audit cites `server.js` for c1."""
    team = uuid.UUID(str(team_id))
    async with sessions.begin() as session:
        snapshot = make_snapshot(team)
        session.add(snapshot)
        if with_claims:  # a team's second submission pins the same claims version
            session.add(make_claims(team))
        await session.flush()
        specs = {claim: make_test_spec(team, claim_id=claim) for claim in ("c1", "c2")}
        session.add_all(specs.values())
        submission = make_submission(
            team, snapshot.id, submitted_at=submitted_at or T0 + timedelta(hours=1)
        )
        session.add(submission)
        await session.flush()
        submission_id, spec_ids = submission.id, {c: s.id for c, s in specs.items()}

    run = await claimed(
        sessions,
        "final_run",
        key=queue.final_run_key(submission_id),
        submission_id=submission_id,
        team_id=team,
    )
    results = {
        "c1": models.TestResult(
            id=uuid.uuid4(),
            job_id=run.id,
            attempt_id=run.attempt_id,
            spec_id=spec_ids["c1"],
            case_id="f1",
            verdict="fail",
            expected={"id": "f1", "input": {"total": 100, "people": 3}, "expected": {}},
            actual={
                "steps": [
                    {
                        "id": "s1",
                        "request": REQUEST,
                        "response": RESPONSE,
                        "assertions": [PASSED, FAILED],
                    }
                ]
            },
            assertion_results=[PASSED, FAILED],
            duration_ms=12,
        ),
        "c2": models.TestResult(
            id=uuid.uuid4(),
            job_id=run.id,
            attempt_id=run.attempt_id,
            spec_id=spec_ids["c2"],
            case_id="f1",
            verdict="pass",
            assertion_results=[PASSED],
        ),
    }
    await _done(sessions, run, *results.values())

    audit_job = await claimed(
        sessions,
        "audit",
        key=queue.audit_key(submission_id),
        submission_id=submission_id,
        team_id=team,
    )
    rows = []
    if audit:
        rows.append(
            models.Audit(
                submission_id=submission_id,
                job_id=audit_job.id,
                attempt_id=audit_job.attempt_id,
                content={
                    "claims": [
                        {
                            "claim_id": "c1",
                            "implementation": "implemented",
                            "explanation": "Rounds each share down.",
                            "citations": [CITATION],
                        }
                    ],
                    "text_hints": [],
                    "missing_claims": ["c2"],
                    "failure": None,
                },
                code_map={"claims": {"c1": {"method": "search", "hits": [], "anchors": []}}},
                code_map_status="fallback",
            )
        )
    await _done(sessions, audit_job, *rows)

    if written is not None:
        brief_job = await claimed(
            sessions,
            "brief",
            key=queue.brief_key(submission_id),
            submission_id=submission_id,
            team_id=team,
        )
        await _done(
            sessions,
            brief_job,
            models.Brief(
                submission_id=submission_id,
                job_id=brief_job.id,
                attempt_id=brief_job.attempt_id,
                content=written,
                ai_status="ok",
                model="m",
            ),
        )
    return Judged(team, submission_id, {c: r.id for c, r in results.items()}, run.id, audit_job.id)
