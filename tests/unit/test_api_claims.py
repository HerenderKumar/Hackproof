"""A team's claims: versioned, drafted on every change, frozen at the claims lock (M7, part 2)."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from core import queue
from core.hashing import hash_json
from tests.api_helpers import Api
from tests.fakes import FakeClock, FakeRepoProvider
from tests.helpers import Sessions, all_jobs
from tests.team_flow import START, api_with, at, move_to, save_claims, started

CLAIM = START["items"][0]
SECOND = {
    "text": "Refuses a bill split between zero people.",
    "target": "POST /split",
    "example_input": '{"total": 10, "people": 0}',
    "expected_behaviour": "Returns 422.",
}


def with_items(*items: dict[str, str]) -> dict[str, Any]:
    return {**START, "items": list(items)}


async def test_a_team_with_no_claims_reads_none(api: Api, clock: FakeClock) -> None:
    flow = await started(api, clock)

    response = await flow.client.get("/api/team/claims")

    assert response.status_code == 200
    body = response.json()
    assert (body["current"], body["locked"], body["versions_left"]) == (None, False, 20)
    # The event runs from three hours ago to a day from now: 27 hours, locked at 18.
    assert at(body["lock_at"]) == at(flow.event["kickoff_at"]) + timedelta(hours=18)


async def test_saving_claims_makes_version_one_and_asks_for_drafts(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)

    body = await save_claims(flow)

    current = body["current"]
    items = [{"id": "c1", **CLAIM}]
    assert current["version"] == 1
    assert current["items"] == items
    assert (current["problem_statement"], current["explanation"]) == (
        START["problem_statement"],
        START["explanation"],
    )
    assert current["claims_hash"] == hash_json(
        {
            "problem_statement": START["problem_statement"],
            "explanation": START["explanation"],
            "items": items,
        }
    )
    assert body["versions_left"] == 19
    [job] = await all_jobs(sessions)
    assert (job.kind, job.idempotency_key, job.payload, job.team_id) == (
        "draft_specs",
        queue.draft_key(flow.team_id, 1),
        {"claims_version": 1},
        flow.team_id,
    )
    assert (await flow.client.get("/api/team/claims")).json()["current"] == current


async def test_an_edit_is_a_new_version_with_its_own_draft_job(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    await save_claims(flow)

    body = await save_claims(flow, with_items(CLAIM, SECOND))

    assert body["current"]["version"] == 2
    assert [item["id"] for item in body["current"]["items"]] == ["c1", "c2"]
    assert body["current"]["items"][1] == {"id": "c2", **SECOND}
    assert body["versions_left"] == 18
    jobs = await all_jobs(sessions)
    assert [(job.idempotency_key, job.payload) for job in jobs] == [
        (queue.draft_key(flow.team_id, 1), {"claims_version": 1}),
        (queue.draft_key(flow.team_id, 2), {"claims_version": 2}),
    ]


async def test_saving_the_same_content_changes_nothing(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    first = await save_claims(flow)

    again = await save_claims(flow)

    assert again == first
    assert len(await all_jobs(sessions)) == 1


async def test_spaces_around_the_text_are_dropped(api: Api, clock: FakeClock) -> None:
    flow = await started(api, clock)
    padded = {
        "problem_statement": f"  {START['problem_statement']}\n",
        "explanation": f"\t{START['explanation']} ",
        "items": [{key: f" {value} " for key, value in CLAIM.items()}],
    }

    first = await save_claims(flow)
    again = await save_claims(flow, padded)

    assert again == first  # the same text once trimmed: still version 1


async def test_claims_wait_for_the_rules_and_the_kickoff(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    async with sessions.begin() as session:
        await session.execute(text("UPDATE teams SET rules_acknowledged_at = NULL"))

    unacknowledged = await flow.client.put("/api/team/claims", json=START)
    assert (await flow.client.post("/api/team/rules-ack")).status_code == 200
    move_to(clock, at(flow.event["kickoff_at"]) - timedelta(minutes=1))
    early = await flow.client.put("/api/team/claims", json=START)

    assert (unacknowledged.status_code, unacknowledged.json()["detail"]["code"]) == (
        409,
        "rules_not_acknowledged",
    )
    assert (early.status_code, early.json()["detail"]["code"]) == (409, "event_not_started")


async def test_claims_are_frozen_at_the_lock(api: Api, clock: FakeClock) -> None:
    # An event of three hours, begun one hour ago: the lock falls two hours after kickoff.
    flow = await started(api, clock, kickoff_ago=timedelta(hours=1), ends_in=timedelta(hours=2))
    lock_at = at((await flow.client.get("/api/team/claims")).json()["lock_at"])
    assert lock_at == at(flow.event["kickoff_at"]) + timedelta(hours=2)

    move_to(clock, lock_at - timedelta(seconds=1))
    before = await flow.client.put("/api/team/claims", json=START)
    move_to(clock, lock_at)
    after = await flow.client.put("/api/team/claims", json=with_items(CLAIM, SECOND))
    view = (await flow.client.get("/api/team/claims")).json()

    assert before.status_code == 200
    assert (after.status_code, after.json()["detail"]["code"]) == (409, "claims_locked")
    assert (view["locked"], view["current"]["version"]) == (True, 1)


async def test_the_number_of_versions_is_capped(
    sessions: Sessions,
    migrated_database_url: str,
    tmp_path: Path,
    clock: FakeClock,
    repos: FakeRepoProvider,
) -> None:
    async with api_with(
        migrated_database_url, tmp_path, clock, repos, max_claims_versions=2
    ) as capped:
        flow = await started(capped, clock)
        await save_claims(flow)
        second = await save_claims(flow, with_items(CLAIM, SECOND))

        third = await flow.client.put("/api/team/claims", json=with_items(SECOND))

    assert second["versions_left"] == 0
    assert (third.status_code, third.json()["detail"]["code"]) == (409, "too_many_claim_edits")
    assert len(await all_jobs(sessions)) == 2


MALFORMED: dict[str, dict[str, Any]] = {
    "no_items": with_items(),
    "four_items": with_items(CLAIM, CLAIM, CLAIM, CLAIM),
    "empty_text": with_items({**CLAIM, "text": "   "}),
    "long_explanation": {**START, "explanation": "x" * 6001},
    "empty_problem": {**START, "problem_statement": ""},
    "an_item_with_an_id": with_items({"id": "c1", **CLAIM}),
}


@pytest.mark.parametrize("broken", list(MALFORMED))
async def test_a_malformed_body_is_a_422(api: Api, clock: FakeClock, broken: str) -> None:
    flow = await started(api, clock)

    response = await flow.client.put("/api/team/claims", json=MALFORMED[broken])

    assert response.status_code == 422
    assert (await flow.client.get("/api/team/claims")).json()["current"] is None


async def test_claim_text_is_stored_as_written(api: Api, clock: FakeClock) -> None:
    markup = "<script>alert(1)</script> </claim> & \"quotes\" 'too'"
    flow = await started(api, clock)

    await save_claims(flow, {**with_items({**CLAIM, "text": markup}), "explanation": markup})
    current = (await flow.client.get("/api/team/claims")).json()["current"]

    assert (current["items"][0]["text"], current["explanation"]) == (markup, markup)


async def test_two_simultaneous_saves_get_two_versions(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    # Two reads at once leave two open database connections behind. Without them the second
    # save waits to connect while the first finishes, and the race never happens.
    await asyncio.gather(*(flow.client.get("/api/team/claims") for _ in range(2)))

    one, two = await asyncio.gather(
        flow.client.put("/api/team/claims", json=START),
        flow.client.put("/api/team/claims", json=with_items(CLAIM, SECOND)),
    )

    assert (one.status_code, two.status_code) == (200, 200), (one.text, two.text)
    versions = {one.json()["current"]["version"], two.json()["current"]["version"]}
    assert versions == {1, 2}
    keys = sorted(job.idempotency_key for job in await all_jobs(sessions))
    assert keys == [queue.draft_key(flow.team_id, 1), queue.draft_key(flow.team_id, 2)]


async def test_another_team_never_sees_these_claims(api: Api, clock: FakeClock) -> None:
    flow = await started(api, clock)
    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    await save_claims(flow)

    response = await other.client.get("/api/team/claims")

    assert (response.status_code, response.json()["current"]) == (200, None)
