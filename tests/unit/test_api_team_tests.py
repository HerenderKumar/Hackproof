"""A team reads the tests drafted from its claims and approves them by hash (M7, part 3)."""

from datetime import timedelta
from typing import Any

import pytest

from core import queue
from core.db import models
from tests import spec_examples
from tests.api_helpers import Api
from tests.fakes import FakeClock
from tests.helpers import Sessions, all_jobs
from tests.team_flow import (
    F01_TEST,
    HIDDEN,
    START,
    Flow,
    at,
    drafted,
    move_to,
    save_claims,
    started,
)

CLAIM = START["items"][0]
OTHER = {"text": "Refuses zero people.", "target": "POST /split"}
THIRD = {"text": "Shows a page listing past splits.", "target": "/history"}


def three_claims() -> dict[str, Any]:
    return {**START, "items": [CLAIM, OTHER, THIRD]}


async def view_of(flow: Flow) -> dict[str, Any]:
    response = await flow.client.get("/api/team/tests")
    assert response.status_code == 200, response.text
    view: dict[str, Any] = response.json()
    return view


async def approve(flow: Flow, test_id: object, spec_hash: str) -> Any:
    return await flow.client.post(
        f"/api/team/tests/{test_id}/approve", json={"spec_hash": spec_hash}
    )


async def row(sessions: Sessions, spec_id: object) -> models.TestSpec:
    async with sessions() as session:
        found = await session.get(models.TestSpec, spec_id)
    assert found is not None
    return found


async def test_before_any_claims_there_are_no_tests(api: Api, clock: FakeClock) -> None:
    flow = await started(api, clock)

    assert await view_of(flow) == {"claims_version": None, "drafting": "none", "claims": []}


async def test_claims_being_drafted_say_so(api: Api, clock: FakeClock) -> None:
    flow = await started(api, clock)
    await save_claims(flow)

    view = await view_of(flow)

    assert (view["claims_version"], view["drafting"]) == (1, "queued")
    assert view["claims"] == [
        {"claim_id": "c1", "text": CLAIM["text"], "state": "drafting", "test": None}
    ]


async def test_a_drafted_test_is_shown_without_its_hidden_cases(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await save_claims(flow)
    [spec] = await drafted(sessions, flow, F01_TEST)

    response = await flow.client.get("/api/team/tests")

    view = response.json()
    assert view["drafting"] == "done"
    [claim] = view["claims"]
    assert (claim["claim_id"], claim["state"]) == ("c1", "draft")
    test = claim["test"]
    assert test["id"] == str(spec.id)
    assert (test["claim_type"], test["version"], test["spec_hash"]) == (
        "calculation",
        1,
        spec.spec_hash,
    )
    assert test["steps"] == spec_examples.F01["steps"]
    assert test["practice_cases"] == spec_examples.F01["practice_cases"]
    assert (test["reason"], test["approved_at"]) == (None, None)
    assert "final_cases" not in response.text
    assert str(HIDDEN) not in response.text


async def test_each_state_a_claim_can_be_in(api: Api, clock: FakeClock, sessions: Sessions) -> None:
    flow = await started(api, clock)
    await save_claims(flow, three_claims())
    unclear = {**F01_TEST, "status": "needs_clarification", "reason": "Say what fair means."}
    await drafted(sessions, flow, unclear, {**F01_TEST, "claim_id": "c3"})

    states = [(c["claim_id"], c["state"]) for c in (await view_of(flow))["claims"]]
    reason = (await view_of(flow))["claims"][0]["test"]["reason"]
    await save_claims(flow, {**three_claims(), "explanation": "A second try."})
    await drafted(sessions, flow, job="failed")
    failed = await view_of(flow)

    assert states == [("c1", "needs_clarification"), ("c2", "ui_pending"), ("c3", "draft")]
    assert reason == "Say what fair means."
    assert failed["drafting"] == "failed"
    assert [c["state"] for c in failed["claims"]] == ["drafting_failed"] * 3


async def test_only_the_current_claims_version_is_shown(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await save_claims(flow)
    await drafted(sessions, flow, F01_TEST)

    await save_claims(flow, {**START, "items": [CLAIM, OTHER]})
    view = await view_of(flow)

    assert (view["claims_version"], view["drafting"]) == (2, "queued")
    assert [(c["claim_id"], c["state"], c["test"]) for c in view["claims"]] == [
        ("c1", "drafting", None),
        ("c2", "drafting", None),
    ]


async def test_a_claim_shows_its_newest_test(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await save_claims(flow)
    older, newer = await drafted(sessions, flow, F01_TEST, F01_TEST)

    [claim] = (await view_of(flow))["claims"]

    assert (older.version, newer.version) == (1, 2)
    assert (claim["test"]["id"], claim["test"]["version"]) == (str(newer.id), 2)


async def test_approving_records_the_hash(api: Api, clock: FakeClock, sessions: Sessions) -> None:
    flow = await started(api, clock)
    await save_claims(flow)
    [spec] = await drafted(sessions, flow, F01_TEST)

    response = await approve(flow, spec.id, spec.spec_hash)

    assert response.status_code == 200, response.text
    [claim] = response.json()["claims"]
    assert claim["state"] == "approved"
    assert at(claim["test"]["approved_at"]) == clock.now()
    stored = await row(sessions, spec.id)
    assert (stored.status, stored.approved_spec_hash, stored.approved_at) == (
        "approved",
        spec.spec_hash,
        clock.now(),
    )


async def test_approving_again_changes_nothing(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await save_claims(flow)
    [spec] = await drafted(sessions, flow, F01_TEST)
    first = await approve(flow, spec.id, spec.spec_hash)
    approved_at = (await row(sessions, spec.id)).approved_at

    clock.advance(minutes=5)
    again = await approve(flow, spec.id, spec.spec_hash)

    assert (first.status_code, again.status_code) == (200, 200)
    assert (await row(sessions, spec.id)).approved_at == approved_at


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("wrong_hash", "hash_mismatch"),
        ("needs_clarification", "not_approvable"),
        ("superseded", "not_approvable"),
    ],
)
async def test_what_cannot_be_approved(
    api: Api, clock: FakeClock, sessions: Sessions, case: str, code: str
) -> None:
    flow = await started(api, clock)
    await save_claims(flow)
    status = "needs_clarification" if case == "needs_clarification" else "draft"
    [spec] = await drafted(sessions, flow, {**F01_TEST, "status": status})
    if case == "superseded":
        await save_claims(flow, {**START, "explanation": "Changed after the draft."})
        await drafted(sessions, flow, F01_TEST)
    digest = "0" * 64 if case == "wrong_hash" else spec.spec_hash

    response = await approve(flow, spec.id, digest)

    assert (response.status_code, response.json()["detail"]["code"]) == (409, code)
    assert (await row(sessions, spec.id)).approved_at is None


async def test_another_teams_test_is_not_found(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    await save_claims(flow)
    [spec] = await drafted(sessions, flow, F01_TEST)

    response = await approve(other, spec.id, spec.spec_hash)

    assert (response.status_code, response.json()["detail"]["code"]) == (404, "test_not_found")
    assert (await row(sessions, spec.id)).status == "draft"
    assert (await view_of(other))["claims"] == []


async def test_an_unknown_test_is_not_found(api: Api, clock: FakeClock) -> None:
    flow = await started(api, clock)

    response = await approve(flow, "00000000-0000-0000-0000-000000000000", "0" * 64)

    assert response.status_code == 404


async def test_approval_goes_on_after_the_lock_until_the_deadline(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    # Three hours, begun one hour ago: locked two hours after kickoff, over at three.
    flow = await started(api, clock, kickoff_ago=timedelta(hours=1), ends_in=timedelta(hours=2))
    await save_claims(flow, {**START, "items": [CLAIM, OTHER]})
    first, second = await drafted(sessions, flow, F01_TEST, {**F01_TEST, "claim_id": "c2"})

    move_to(clock, at(flow.event["claims_lock_at"]))
    at_the_lock = await approve(flow, first.id, first.spec_hash)
    move_to(clock, at(flow.event["deadline_at"]) + timedelta(seconds=1))
    too_late = await approve(flow, second.id, second.spec_hash)

    assert at_the_lock.status_code == 200
    assert (too_late.status_code, too_late.json()["detail"]["code"]) == (409, "deadline_passed")
    assert (await row(sessions, second.id)).status == "draft"


async def test_a_failed_draft_can_be_asked_for_again(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await save_claims(flow)
    await drafted(sessions, flow, job="failed")

    response = await flow.client.post("/api/team/tests/redraft")

    assert response.status_code == 200, response.text
    assert response.json()["drafting"] == "queued"
    jobs = await all_jobs(sessions)
    assert [(j.kind, j.idempotency_key, j.payload, j.team_id) for j in jobs[1:]] == [
        (
            "draft_specs",
            f"{queue.draft_key(flow.team_id, 1)}:retry1",
            {"claims_version": 1},
            flow.team_id,
        )
    ]


async def test_redrafting_needs_a_failed_draft_and_is_capped(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    nothing_yet = await flow.client.post("/api/team/tests/redraft")
    await save_claims(flow)
    queued = await flow.client.post("/api/team/tests/redraft")
    await drafted(sessions, flow, job="done")
    done = await flow.client.post("/api/team/tests/redraft")

    for _ in range(3):  # max_redrafts
        await drafted(sessions, flow, job="failed")
        assert (await flow.client.post("/api/team/tests/redraft")).status_code == 200
    await drafted(sessions, flow, job="failed")
    capped = await flow.client.post("/api/team/tests/redraft")

    for response in (nothing_yet, queued, done):
        assert (response.status_code, response.json()["detail"]["code"]) == (
            409,
            "nothing_to_redraft",
        )
    assert (capped.status_code, capped.json()["detail"]["code"]) == (409, "too_many_redrafts")
    assert len(await all_jobs(sessions)) == 4
