"""Which specs a run carries (design section 5): pinned approved specs, modes and leaks."""

import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db import models
from core.specs import spec_hash, test_spec_hash
from tests import spec_examples
from tests.factories import (
    T0,
    make_claims,
    make_event,
    make_snapshot,
    make_submission,
    make_team,
    make_test_spec,
)
from worker.lanes import Outcome
from worker.run_specs import NotRunnable, RunPlan, final_run_plan, self_check_plan

Sessions = async_sessionmaker[AsyncSession]


def variant(total: int) -> dict[str, Any]:
    """F01 with a different first hidden case, so it hashes differently."""
    doc = copy.deepcopy(spec_examples.F01)
    doc["final_cases"][0]["input"]["total"] = total
    return doc


@dataclass
class Scene:
    team_id: uuid.UUID
    snapshot_id: uuid.UUID


async def scene(sessions: Sessions) -> Scene:
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        team = make_team(event.id)
        session.add(team)
        await session.flush()
        snapshot = make_snapshot(team.id)
        session.add_all([snapshot, make_claims(team.id)])
        await session.flush()
        return Scene(team.id, snapshot.id)


async def add(sessions: Sessions, *rows: models.TestSpec) -> list[uuid.UUID]:
    async with sessions.begin() as session:
        session.add_all(rows)
        await session.flush()
        return [row.id for row in rows]


async def plan_for(
    sessions: Sessions,
    s: Scene,
    pinned: list[models.TestSpec] | None = None,
    digest: str | None = None,
) -> RunPlan | Outcome:
    hashes = [row.spec_hash for row in pinned or []]
    async with sessions.begin() as session:
        submission = make_submission(
            s.team_id, s.snapshot_id, test_spec_hash=digest or test_spec_hash(hashes)
        )
        session.add(submission)
        await session.flush()
        return await final_run_plan(session, submission)


def ids(plan: RunPlan | Outcome) -> set[str]:
    assert isinstance(plan, RunPlan)
    return {spec["spec_id"] for spec in plan.runnable}


# --- final runs ----------------------------------------------------------------------------------


async def test_a_final_run_carries_the_latest_approved_version_of_each_claim(
    sessions: Sessions,
) -> None:
    s = await scene(sessions)
    old = make_test_spec(s.team_id, "c1", version=1)
    new = make_test_spec(s.team_id, "c1", variant(250), version=2)
    words = make_test_spec(s.team_id, "c2", spec_examples.F07)
    await add(sessions, old, new, words)

    plan = await plan_for(sessions, s, pinned=[new, words])

    assert ids(plan) == {str(new.id), str(words.id)}
    assert isinstance(plan, RunPlan) and plan.mode == "final" and plan.not_runnable == []


async def test_a_pinned_spec_runs_even_after_it_was_superseded(sessions: Sessions) -> None:
    s = await scene(sessions)
    pinned = make_test_spec(s.team_id, "c1", status="superseded", approved_at=T0)
    pinned.approved_spec_hash = pinned.spec_hash  # approved, then a claims edit superseded it
    await add(sessions, pinned)

    assert ids(await plan_for(sessions, s, pinned=[pinned])) == {str(pinned.id)}


async def test_an_approval_of_a_different_hash_does_not_count(sessions: Sessions) -> None:
    s = await scene(sessions)
    stale = make_test_spec(s.team_id, "c1", approved_spec_hash="0" * 64)
    await add(sessions, stale)

    plan = await plan_for(sessions, s, pinned=[])

    assert ids(plan) == set()
    assert isinstance(plan, RunPlan)
    assert plan.not_runnable == [NotRunnable(stale.id, "c1", "the test was not approved")]


async def test_a_final_run_whose_tests_changed_runs_nothing(sessions: Sessions) -> None:
    s = await scene(sessions)
    await add(sessions, make_test_spec(s.team_id, "c1"))

    plan = await plan_for(sessions, s, digest="x" * 64)

    assert plan == Outcome("validation", "the approved tests do not match the submission")


async def test_a_claim_needing_clarification_is_not_runnable_with_its_reason(
    sessions: Sessions,
) -> None:
    s = await scene(sessions)
    split = make_test_spec(s.team_id, "c1")
    vague = make_test_spec(
        s.team_id,
        "c2",
        spec_examples.F07,
        status="needs_clarification",
        clarification_reason="which whitespace counts?",
    )
    await add(sessions, split, vague)

    plan = await plan_for(sessions, s, pinned=[split])

    assert ids(plan) == {str(split.id)}
    assert isinstance(plan, RunPlan)
    assert plan.not_runnable == [NotRunnable(vague.id, "c2", "which whitespace counts?")]


async def test_a_final_run_never_carries_the_practice_case(sessions: Sessions) -> None:
    s = await scene(sessions)
    split = make_test_spec(s.team_id, "c1")
    await add(sessions, split)

    plan = await plan_for(sessions, s, pinned=[split])

    assert isinstance(plan, RunPlan)
    (spec,) = plan.runnable
    assert spec["practice_cases"] == []
    assert [case["id"] for case in spec["final_cases"]] == ["f1", "f2", "f3"]
    assert spec_hash(spec_examples.F01 | {"claim_id": "c1"}) == split.spec_hash


async def test_startup_rows_are_never_part_of_a_run(sessions: Sessions) -> None:
    s = await scene(sessions)
    startup = {
        "kind": "startup",
        "claim_type": "startup",
        "steps": [{"health_path": "/"}],
        "practice_cases": [],
        "final_cases": [],
    }
    await add(sessions, make_test_spec(s.team_id, document=startup, claim_id=None))

    plan = await plan_for(sessions, s, pinned=[])

    assert ids(plan) == set()
    assert isinstance(plan, RunPlan) and plan.not_runnable == []


async def test_a_final_run_takes_its_teams_specs_for_the_submissions_claims_version(
    sessions: Sessions,
) -> None:
    s = await scene(sessions)
    pinned = make_test_spec(s.team_id, "c1")  # no snapshot: tests belong to the claims (D-047)
    later = make_test_spec(s.team_id, "c1", variant(250), version=2, claims_version=2)
    await add(sessions, pinned, later)

    plan = await plan_for(sessions, s, pinned=[pinned])  # the submission pins claims version 1

    assert ids(plan) == {str(pinned.id)}
    assert isinstance(plan, RunPlan) and plan.not_runnable == []


async def test_another_teams_specs_are_never_carried(sessions: Sessions) -> None:
    s, other = await scene(sessions), await scene(sessions)
    mine = make_test_spec(s.team_id, "c1")
    theirs = make_test_spec(other.team_id, "c2", spec_examples.F07)
    await add(sessions, mine, theirs)

    final = await plan_for(sessions, s, pinned=[mine])
    async with sessions() as session:
        practice = await self_check_plan(session, s.team_id, None)

    assert ids(final) == {str(mine.id)} and ids(practice) == {str(mine.id)}
    assert isinstance(final, RunPlan) and final.not_runnable == []
    assert practice.not_runnable == []


# --- self-checks ---------------------------------------------------------------------------------


async def test_a_self_check_never_carries_a_hidden_case(sessions: Sessions) -> None:
    s = await scene(sessions)
    await add(sessions, make_test_spec(s.team_id, "c1", status="draft"))

    async with sessions() as session:
        plan = await self_check_plan(session, s.team_id, None)

    assert plan.mode == "practice"
    text = json.dumps(plan.runnable)
    assert all(spec["final_cases"] == [] for spec in plan.runnable)
    for case in spec_examples.F01["final_cases"]:
        assert case["id"] not in {c["id"] for spec in plan.runnable for c in spec["practice_cases"]}
        assert json.dumps(case["input"]) not in text


async def test_a_self_check_runs_the_listed_specs_of_its_own_team_only(
    sessions: Sessions,
) -> None:
    s, other = await scene(sessions), await scene(sessions)
    mine = make_test_spec(s.team_id, "c1", status="draft")
    theirs = make_test_spec(other.team_id, "c1", status="draft")
    await add(sessions, mine, theirs)

    async with sessions() as session:
        plan = await self_check_plan(session, s.team_id, [mine.id, theirs.id])

    assert ids(plan) == {str(mine.id)}


async def test_a_self_check_without_a_list_runs_the_current_specs(sessions: Sessions) -> None:
    s = await scene(sessions)
    older_claims = make_test_spec(s.team_id, "c1", status="draft", claims_version=1)
    first = make_test_spec(s.team_id, "c1", status="draft", claims_version=2, version=1)
    latest = make_test_spec(
        s.team_id, "c1", variant(250), status="approved", claims_version=2, version=2
    )
    gone = make_test_spec(s.team_id, "c2", spec_examples.F07, status="superseded", claims_version=2)
    vague = make_test_spec(
        s.team_id,
        "c3",
        spec_examples.F07,
        status="needs_clarification",
        claims_version=2,
        clarification_reason="what is a word?",
    )
    await add(sessions, older_claims, first, latest, gone, vague)

    async with sessions() as session:
        plan = await self_check_plan(session, s.team_id, None)

    assert ids(plan) == {str(latest.id)}
    assert plan.not_runnable == [NotRunnable(vague.id, "c3", "what is a word?")]
