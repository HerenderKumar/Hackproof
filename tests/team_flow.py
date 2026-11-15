"""A team partway through an event, for the tests of the team routes (M7).

Events here are built around the real current time, not the fixed T0 of the other tests: the
final submission judges its deadline by the database's clock, which a test cannot move. The
API's clock (a FakeClock) is set inside the event, and tests move it with `move_to`.

A session lasts 12 hours from the moment its link is redeemed (`started` redeems at kickoff
plus 10 minutes), so a test that needs a time later than that sets a short event or its own
claims lock rather than moving the clock past the session's end.
"""

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select, text, update

from core.db import models
from core.repo import RepoInfo
from scripts.fixtures import FIXTURES_DIR
from tests import spec_examples
from tests.api_helpers import Api, make_app
from tests.factories import make_snapshot, make_test_spec
from tests.fakes import FakeClock, FakeRepoProvider
from tests.helpers import Sessions

_F01 = FIXTURES_DIR / "F01-node-splitter"
_F01_START = json.loads((_F01 / "start.json").read_text())
_F01_CLAIM = {
    k: v for k, v in json.loads((_F01 / "claims.json").read_text())[0].items() if k != "id"
}

# F01's start text as a team would type it: the server gives the claim its ID.
START: dict[str, Any] = {
    "problem_statement": _F01_START["problem_statement"],
    "explanation": _F01_START["explanation"],
    "items": [_F01_CLAIM],
}

# A value `drafted` puts in a hidden case's input: no team response may ever contain it.
HIDDEN = 987654321

# F01's drafted test, as the draft job stores it for claim c1.
F01_TEST: dict[str, Any] = {**spec_examples.F01, "claim_id": "c1"}


@dataclass
class Flow:
    api: Api
    clock: FakeClock
    organiser: httpx.AsyncClient
    event: dict[str, Any]
    team: dict[str, Any]
    client: httpx.AsyncClient  # a browser signed in as the team
    team_id: uuid.UUID


def at(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def move_to(clock: FakeClock, when: str | datetime) -> None:
    """Set the API's clock (an ISO string, as the API returns times, or a datetime)."""
    clock.at = at(when)


async def started(
    api: Api,
    clock: FakeClock,
    *,
    name: str = "Rocket",
    kickoff_ago: timedelta = timedelta(hours=3),
    ends_in: timedelta = timedelta(days=1),
    organiser: httpx.AsyncClient | None = None,
    event: dict[str, Any] | None = None,
    **event_fields: Any,
) -> Flow:
    """A signed-in team that has acknowledged the rules, in an event that has started.

    Passing `organiser` and `event` adds another team to that event, leaving the clock alone.
    """
    if organiser is None or event is None:
        real_now = datetime.now(UTC)
        kickoff = real_now - kickoff_ago
        move_to(clock, kickoff + timedelta(minutes=10))
        organiser = await api.organiser()
        fields = {"kickoff_at": kickoff, "deadline_at": real_now + ends_in, **event_fields}
        event = await api.event(
            organiser,
            **{k: v.isoformat() if isinstance(v, datetime) else v for k, v in fields.items()},
        )
    team, client = await api.team(organiser, event["id"], name)
    acknowledged = await client.post("/api/team/rules-ack")
    assert acknowledged.status_code == 200, acknowledged.text
    return Flow(api, clock, organiser, event, team, client, uuid.UUID(team["id"]))


async def save_claims(flow: Flow, body: dict[str, Any] = START) -> dict[str, Any]:
    response = await flow.client.put("/api/team/claims", json=body)
    assert response.status_code == 200, response.text
    saved: dict[str, Any] = response.json()
    return saved


@asynccontextmanager
async def api_with(
    database_url: str,
    artifact_dir: Path,
    clock: FakeClock,
    repos: FakeRepoProvider,
    **settings: Any,
) -> AsyncIterator[Api]:
    """An API made with other settings than the `api` fixture's (a lower cap, say)."""
    app = make_app(database_url, artifact_dir, clock, repos, **settings)
    helper = Api(app)
    try:
        yield helper
    finally:
        await helper.aclose()
        await app.state.engine.dispose()


def _hide(document: dict[str, Any]) -> dict[str, Any]:
    first, *rest = document["final_cases"] or [{"id": "f1", "input": {}}]
    return {**document, "final_cases": [{**first, "input": {**first["input"], "n": HIDDEN}}, *rest]}


async def drafted(
    sessions: Sessions,
    flow: Flow,
    *documents: dict[str, Any],
    job: str = "done",
    status: str = "draft",
) -> list[models.TestSpec]:
    """What the draft job leaves for the team's current claims version.

    One test_specs row per document (its `claim_id` names the claim; an optional `status` and
    `reason` override the defaults), the rows of older claims versions superseded, and the
    newest draft job set to `job`. With no documents, only the job changes.
    """
    rows: list[models.TestSpec] = []
    async with sessions.begin() as session:
        version = await session.scalar(
            select(func.max(models.Claims.version)).where(models.Claims.team_id == flow.team_id)
        )
        assert version is not None, "save claims first"
        for document in documents:
            claim_id = document["claim_id"]
            highest = await session.scalar(
                select(func.max(models.TestSpec.version)).where(
                    models.TestSpec.team_id == flow.team_id, models.TestSpec.claim_id == claim_id
                )
            )
            fields = {k: v for k, v in document.items() if k not in ("status", "reason")}
            row = make_test_spec(
                flow.team_id,
                claim_id,
                _hide(fields),
                status=document.get("status", status),
                version=(highest or 0) + 1,
                claims_version=version,
                clarification_reason=document.get("reason"),
            )
            session.add(row)
            rows.append(row)
        if documents:
            await session.execute(
                update(models.TestSpec)
                .where(
                    models.TestSpec.team_id == flow.team_id,
                    models.TestSpec.claims_version < version,
                )
                .values(status="superseded")
            )
        newest = await session.scalar(
            select(models.Job)
            .where(models.Job.team_id == flow.team_id, models.Job.kind == "draft_specs")
            .order_by(models.Job.created_at.desc())
            .limit(1)
        )
        assert newest is not None
        newest.status = job
    return rows


async def approve_all(flow: Flow) -> None:
    """Approve every drafted test through the API, as the team would."""
    view = (await flow.client.get("/api/team/tests")).json()
    for claim in view["claims"]:
        if claim["state"] == "draft":
            test = claim["test"]
            response = await flow.client.post(
                f"/api/team/tests/{test['id']}/approve", json={"spec_hash": test["spec_hash"]}
            )
            assert response.status_code == 200, response.text


REPO_URL = "https://github.com/acme/rocket"


async def with_repo(
    flow: Flow, repos: FakeRepoProvider, head: str | Exception | None = "a" * 40
) -> RepoInfo:
    """Register the team's repository through the API, with `head` as its default branch's."""
    info = repos.add(REPO_URL)
    repos.heads[info.url] = head
    response = await flow.client.post("/api/team/repo", json={"repo_url": REPO_URL})
    assert response.status_code == 200, response.text
    return info


async def snapshot(sessions: Sessions, flow: Flow, **overrides: Any) -> models.Snapshot:
    """A snapshot of the team's repository, as the prepare job stores it."""
    async with sessions.begin() as session:
        row = make_snapshot(flow.team_id, **overrides)
        session.add(row)
        await session.flush()
        await session.refresh(row)
    return row


async def finished_self_check(
    sessions: Sessions,
    job_id: uuid.UUID,
    *,
    results: Sequence[tuple[models.TestSpec, str, str]] = (),
    build: str = "ok",
    startup: str = "ok",
    startup_detail: str | None = None,
    logs: dict[str, Path] | None = None,
) -> None:
    """What the worker leaves after running a job: its run, one result per (test, case, verdict)
    under the attempt that ran, and the job done."""
    logs = logs or {}
    async with sessions.begin() as session:
        attempt = await session.scalar(
            text(
                "UPDATE jobs SET status = 'running', attempt_id = gen_random_uuid(), "
                "attempts = attempts + 1, lease_until = now() + interval '20 minutes' "
                "WHERE id = :id RETURNING attempt_id"
            ),
            {"id": job_id},
        )
        assert attempt is not None, f"no job {job_id}"
        session.add(
            models.RunResult(
                job_id=job_id,
                attempt_id=attempt,
                build_status=build,
                startup_status=startup,
                startup_detail=startup_detail,
                build_log_path=str(logs["build"]) if "build" in logs else None,
                app_log_path=str(logs["app"]) if "app" in logs else None,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        for spec, case_id, verdict in results:
            passed = verdict == "pass"
            session.add(
                models.TestResult(
                    job_id=job_id,
                    attempt_id=attempt,
                    spec_id=spec.id,
                    case_id=case_id,
                    verdict=verdict,
                    assertion_results=[
                        {
                            "op": "status_equals",
                            "expected": 200,
                            "actual": 200 if passed else 500,
                            "passed": passed,
                        }
                    ],
                    duration_ms=12,
                    error=None if verdict in ("pass", "fail") else "the app did not start",
                )
            )
        await session.flush()
        await session.execute(
            text("UPDATE jobs SET status = 'done', finished_at = now() WHERE id = :id"),
            {"id": job_id},
        )
