"""The `draft_specs` job (D-047): rows for the claims version, repeats, newer versions."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from core import queue
from core.db import models
from core.errors import InfraError
from core.llm import LLMClient
from core.settings import Settings
from core.specs import ApiSpec, spec_hash
from tests import spec_examples
from tests.factories import CLAIM_ITEMS, make_claims, make_event, make_team, make_test_spec
from tests.helpers import Sessions, claimed
from tests.llm_fakes import Scripted, draft_answer, draft_entry, unclear_entry
from worker.draft_job import make_draft_handler
from worker.lanes import Outcome

SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    llm_mode="replay",
    draft_model="draft-model",
    draft_max_tokens=4000,
)
C1, C2 = CLAIM_ITEMS
C3 = {**C2, "id": "c3"}
GOOD = draft_answer(draft_entry(spec_examples.F01), draft_entry(spec_examples.F07, "c2"))


async def team(sessions: Sessions) -> uuid.UUID:
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        row = make_team(event.id)
        session.add(row)
        await session.flush()
        return row.id


async def claims(
    sessions: Sessions, team_id: uuid.UUID, version: int, *items: dict[str, Any]
) -> None:
    async with sessions.begin() as session:
        session.add(
            make_claims(
                team_id,
                list(items),
                version,
                problem_statement="Splitting bills fairly is fiddly.",
                explanation="A bill splitter for flatmates.",
            )
        )


async def team_with_claims(sessions: Sessions, *items: dict[str, Any]) -> uuid.UUID:
    team_id = await team(sessions)
    await claims(sessions, team_id, 1, *items)
    return team_id


async def draft_job(sessions: Sessions, team_id: uuid.UUID | None, version: Any = 1) -> queue.Job:
    payload = {} if version is None else {"claims_version": version}
    key = queue.draft_key(team_id or uuid.uuid4(), version if isinstance(version, int) else 0)
    return await claimed(sessions, "draft_specs", key=key, payload=payload, team_id=team_id)


async def run(sessions: Sessions, transport: Scripted, job: queue.Job) -> Outcome | None:
    handler = make_draft_handler(sessions, LLMClient(transport, sessions=sessions), SETTINGS)
    return await handler(job)


async def specs(sessions: Sessions) -> list[models.TestSpec]:
    async with sessions() as session:
        rows = await session.scalars(
            select(models.TestSpec).order_by(
                models.TestSpec.claims_version, models.TestSpec.claim_id
            )
        )
        return list(rows)


def stored(document: dict[str, Any]) -> dict[str, Any]:
    return ApiSpec.model_validate(document).model_dump(by_alias=True)


# --- what is stored ------------------------------------------------------------------------------


async def test_drafts_are_stored_for_the_claims_version(sessions: Sessions) -> None:
    team_id = await team_with_claims(sessions, C1)
    transport = Scripted(draft_answer(draft_entry(spec_examples.F01)))

    assert await run(sessions, transport, await draft_job(sessions, team_id)) is None

    (row,) = await specs(sessions)
    document = stored(spec_examples.F01)
    assert (row.team_id, row.snapshot_id, row.claims_version, row.version) == (team_id, None, 1, 1)
    assert (row.kind, row.claim_id, row.claim_type, row.status) == (
        "api", "c1", "calculation", "draft",
    )  # fmt: skip
    assert (
        stored(
            {
                "kind": row.kind,
                "claim_id": row.claim_id,
                "claim_type": row.claim_type,
                "steps": row.steps,
                "practice_cases": row.practice_cases,
                "final_cases": row.final_cases,
            }
        )
        == document
    )
    assert row.spec_hash == spec_hash(document)
    assert (row.clarification_reason, row.approved_at, row.approved_spec_hash) == (None,) * 3


async def test_the_model_is_asked_with_the_teams_start_text(sessions: Sessions) -> None:
    team_id = await team_with_claims(sessions, C1)
    transport = Scripted(draft_answer(draft_entry(spec_examples.F01)))

    await run(sessions, transport, await draft_job(sessions, team_id))

    (body,) = transport.bodies
    (message,) = transport.user_messages()
    assert (body["model"], body["max_tokens"]) == ("draft-model", 4000)
    assert "<problem_statement>\nSplitting bills fairly is fiddly.\n</problem_statement>" in message
    assert "<explanation>\nA bill splitter for flatmates.\n</explanation>" in message
    assert '<claim id="c1">' in message and "<target>POST /split</target>" in message


async def test_a_claim_needing_clarification_is_stored_with_its_reason_and_no_steps(
    sessions: Sessions,
) -> None:
    team_id = await team_with_claims(sessions, C1)
    transport = Scripted(draft_answer(unclear_entry("The claim names no endpoint to call.")))

    assert await run(sessions, transport, await draft_job(sessions, team_id)) is None

    (row,) = await specs(sessions)
    assert (row.status, row.clarification_reason) == (
        "needs_clarification", "The claim names no endpoint to call.",
    )  # fmt: skip
    assert (row.kind, row.claim_id, row.claim_type, row.version) == ("api", "c1", "calculation", 1)
    assert (row.steps, row.practice_cases, row.final_cases) == ([], [], [])
    assert row.spec_hash == spec_hash(
        {"kind": "api", "claim_type": "calculation", "steps": [], "practice_cases": [],
         "final_cases": []}
    )  # fmt: skip


async def test_ui_claims_get_no_row(sessions: Sessions) -> None:
    team_id = await team_with_claims(sessions, C1, C2)
    ui = {"claim_id": "c1", "claim_type": "ui_workflow", "status": "draft"}
    transport = Scripted(draft_answer(ui, draft_entry(spec_examples.F07, "c2")))

    assert await run(sessions, transport, await draft_job(sessions, team_id)) is None

    assert [(row.claim_id, row.kind, row.status) for row in await specs(sessions)] == [
        ("c2", "api", "draft")
    ]


async def test_model_calls_are_recorded_against_the_job(sessions: Sessions) -> None:
    team_id = await team_with_claims(sessions, C1)
    job = await draft_job(sessions, team_id)

    await run(sessions, Scripted(draft_answer(draft_entry(spec_examples.F01))), job)

    async with sessions() as session:
        calls = list(await session.scalars(select(models.LlmCall)))
    assert [(call.job_id, call.purpose, call.model, call.ok) for call in calls] == [
        (job.id, "draft", "draft-model", True)
    ]


# --- repeats and newer versions ------------------------------------------------------------------


async def test_a_repeated_attempt_writes_nothing_and_calls_no_model(sessions: Sessions) -> None:
    team_id = await team_with_claims(sessions, C1, C2)
    job = await draft_job(sessions, team_id)
    await run(sessions, Scripted(GOOD), job)
    again = Scripted()

    assert await run(sessions, again, job) is None

    assert again.bodies == []
    assert [(row.claim_id, row.version) for row in await specs(sessions)] == [("c1", 1), ("c2", 1)]


class Meanwhile(Scripted):
    """A model call during which something else happens, as another attempt or an edit would."""

    def __init__(self, during: Any, *replies: dict[str, Any]) -> None:
        super().__init__(*replies)
        self.during = during

    async def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        await self.during()
        return await super().__call__(body)


async def test_two_attempts_drafting_at_once_store_one_set_of_rows(sessions: Sessions) -> None:
    team_id = await team_with_claims(sessions, C1, C2)
    job = await draft_job(sessions, team_id)

    async def the_other_attempt_finishes() -> None:
        await run(sessions, Scripted(GOOD), job)

    assert await run(sessions, Meanwhile(the_other_attempt_finishes, GOOD), job) is None

    assert [(row.claim_id, row.version) for row in await specs(sessions)] == [("c1", 1), ("c2", 1)]


async def test_a_newer_claims_version_makes_the_job_do_nothing(sessions: Sessions) -> None:
    team_id = await team_with_claims(sessions, C1)
    await claims(sessions, team_id, 2, C1, C2)
    transport = Scripted()

    assert await run(sessions, transport, await draft_job(sessions, team_id, 1)) is None

    assert transport.bodies == []
    assert await specs(sessions) == []


async def test_claims_edited_while_drafting_leave_nothing_for_the_older_version(
    sessions: Sessions,
) -> None:
    team_id = await team_with_claims(sessions, C1, C2)

    async def the_team_edits_its_claims() -> None:
        await claims(sessions, team_id, 2, C1)

    transport = Meanwhile(the_team_edits_its_claims, GOOD)

    assert await run(sessions, transport, await draft_job(sessions, team_id, 1)) is None

    assert await specs(sessions) == []


async def test_earlier_versions_are_superseded_and_versions_count_up(sessions: Sessions) -> None:
    team_id, other_team = await team_with_claims(sessions, C1, C2), await team(sessions)
    await claims(sessions, team_id, 2, C1, C3)
    async with sessions.begin() as session:
        session.add(make_test_spec(team_id, "c1", status="approved"))
        session.add(make_test_spec(team_id, "c2", spec_examples.F07, status="draft"))
        session.add(make_test_spec(other_team, "c1", status="approved"))
    transport = Scripted(
        draft_answer(draft_entry(spec_examples.F01), draft_entry(spec_examples.F07, "c3"))
    )

    assert await run(sessions, transport, await draft_job(sessions, team_id, 2)) is None

    rows = await specs(sessions)
    assert {
        (row.team_id, row.claims_version, row.claim_id, row.version, row.status) for row in rows
    } == {
        (team_id, 1, "c1", 1, "superseded"),
        (team_id, 1, "c2", 1, "superseded"),
        (team_id, 2, "c1", 2, "draft"),
        (team_id, 2, "c3", 1, "draft"),
        (other_team, 1, "c1", 1, "approved"),
    }
    assert len(rows) == 5
    # What a submission pinned stays readable: the approval is kept on the superseded row.
    assert all(
        row.approved_at is not None for row in rows if row.claim_id == "c1" and row.version == 1
    )


# --- failures ------------------------------------------------------------------------------------


@pytest.mark.parametrize("fault", ["no_version", "not_a_number", "unknown_version", "no_team"])
async def test_a_job_without_its_claims_version_is_a_validation_error(
    sessions: Sessions, fault: str
) -> None:
    team_id = await team_with_claims(sessions, C1)
    version = {"no_version": None, "not_a_number": "1", "unknown_version": 7, "no_team": 1}[fault]
    job = await draft_job(sessions, None if fault == "no_team" else team_id, version)
    transport = Scripted()

    outcome = await run(sessions, transport, job)

    assert outcome is not None and outcome.error_class == "validation"
    assert transport.bodies == []
    assert await specs(sessions) == []


async def test_a_gateway_failure_stores_nothing_and_is_left_to_the_job_loop(
    sessions: Sessions,
) -> None:
    team_id = await team_with_claims(sessions, C1)

    with pytest.raises(InfraError, match="HTTP 500"):
        await run(
            sessions,
            Scripted(InfraError("the model gateway answered HTTP 500")),
            await draft_job(sessions, team_id),
        )

    assert await specs(sessions) == []
