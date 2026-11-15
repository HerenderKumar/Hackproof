"""The team's own pages: rules acknowledgement and repository registration (section 5.2)."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from core.errors import InfraError
from core.repo import InvalidRepoUrl
from tests.api_helpers import Api, make_app
from tests.factories import T0, make_submission
from tests.fakes import FakeClock, FakeRepoProvider
from tests.helpers import Sessions
from tests.team_flow import F01_TEST, START, drafted, save_claims, snapshot, started

REPO = "https://github.com/acme/rocket"


async def signed_in_team(
    api: Api, name: str = "Rocket"
) -> tuple[dict[str, Any], httpx.AsyncClient]:
    organiser = await api.organiser()
    event = await api.event(organiser)
    return await api.team(organiser, event["id"], name)


async def acknowledged_team(api: Api, name: str = "Rocket") -> httpx.AsyncClient:
    _, client = await signed_in_team(api, name)
    assert (await client.post("/api/team/rules-ack")).status_code == 200
    return client


async def test_a_team_sees_itself_and_its_event(api: Api) -> None:
    team, client = await signed_in_team(api)

    response = await client.get("/api/team")

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["name"]) == (team["id"], "Rocket")
    assert body["event"]["name"] == "Winter Hack"
    assert body["event"]["policy_text"].startswith("Declare")
    assert (body["rules_acknowledged_at"], body["repo"], body["health_path"]) == (None, None, "/")


async def test_acknowledging_the_rules_is_recorded_once(api: Api, clock: FakeClock) -> None:
    _, client = await signed_in_team(api)
    first_time = clock.now()

    first = await client.post("/api/team/rules-ack")
    clock.advance(hours=1)
    second = await client.post("/api/team/rules-ack")

    stamp = first_time.isoformat().replace("+00:00", "Z")
    assert (first.status_code, first.json()["rules_acknowledged_at"]) == (200, stamp)
    assert second.json()["rules_acknowledged_at"] == stamp


async def test_the_rules_come_before_the_repository(api: Api, repos: FakeRepoProvider) -> None:
    repos.add(REPO)
    _, client = await signed_in_team(api)

    response = await client.post("/api/team/repo", json={"repo_url": REPO})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "rules_not_acknowledged"
    assert repos.calls == []


async def test_a_public_repository_is_registered(
    api: Api, repos: FakeRepoProvider, clock: FakeClock, sessions: Sessions
) -> None:
    info = repos.add(REPO, url="https://github.com/acme/Rocket", default_branch="trunk")
    client = await acknowledged_team(api)

    response = await client.post("/api/team/repo", json={"repo_url": REPO})

    assert response.status_code == 200
    assert response.json()["repo"] == {
        "url": "https://github.com/acme/Rocket",
        "provider": "github",
        "default_branch": "trunk",
        "registered_at": clock.now().isoformat().replace("+00:00", "Z"),
        "late": False,
    }
    async with sessions() as s:
        row = (
            await s.execute(text("SELECT repository_id, repo_created_at, health_path FROM teams"))
        ).one()
    assert (row.repository_id, row.repo_created_at, row.health_path) == (
        info.id, info.created_at, "/",
    )  # fmt: skip


async def test_a_team_can_set_its_health_path(api: Api, repos: FakeRepoProvider) -> None:
    repos.add(REPO)
    client = await acknowledged_team(api)

    response = await client.post(
        "/api/team/repo", json={"repo_url": REPO, "health_path": "/healthz?full=1"}
    )

    assert response.json()["health_path"] == "/healthz?full=1"


@pytest.mark.parametrize(
    "path", ["healthz", "//evil.example/", "http://evil.example/", "/a b", "/" + "x" * 200, "@evil"]
)
async def test_a_health_path_must_be_a_plain_path(
    api: Api, repos: FakeRepoProvider, path: str
) -> None:
    repos.add(REPO)
    client = await acknowledged_team(api)

    response = await client.post("/api/team/repo", json={"repo_url": REPO, "health_path": path})

    assert response.status_code == 422
    assert (await client.get("/api/team")).json()["repo"] is None


async def test_repositories_that_cannot_be_used_are_refused(
    api: Api, repos: FakeRepoProvider
) -> None:
    repos.add("https://github.com/acme/secret", is_public=False)
    repos.fail("https://gitlab.com/acme/rocket", InvalidRepoUrl("use a GitHub URL"))
    repos.fail("https://github.com/acme/flaky", InfraError("GitHub rate limit reached"))
    client = await acknowledged_team(api)

    async def register(url: str) -> tuple[int, str]:
        response = await client.post("/api/team/repo", json={"repo_url": url})
        return response.status_code, response.json()["detail"]["code"]

    assert await register("https://github.com/acme/secret") == (422, "repo_private")
    assert await register("https://github.com/acme/missing") == (422, "repo_not_found")
    assert await register("https://gitlab.com/acme/rocket") == (422, "repo_url_invalid")
    assert await register("https://github.com/acme/flaky") == (503, "repo_provider_unavailable")
    assert (await client.get("/api/team")).json()["repo"] is None


async def test_the_repository_can_be_replaced_before_kickoff(
    api: Api, repos: FakeRepoProvider
) -> None:
    repos.add(REPO)
    repos.add("https://github.com/acme/rocket-2")
    client = await acknowledged_team(api)
    await client.post("/api/team/repo", json={"repo_url": REPO})

    response = await client.post(
        "/api/team/repo", json={"repo_url": "https://github.com/acme/rocket-2"}
    )

    assert response.status_code == 200
    assert response.json()["repo"]["url"] == "https://github.com/acme/rocket-2"


async def test_registering_after_kickoff_is_allowed_and_marked_late(
    api: Api, repos: FakeRepoProvider, clock: FakeClock
) -> None:
    repos.add(REPO)
    client = await acknowledged_team(api)
    clock.at = T0 + timedelta(minutes=10)

    response = await client.post("/api/team/repo", json={"repo_url": REPO})

    assert response.status_code == 200
    assert response.json()["repo"]["late"] is True


async def test_the_repository_cannot_be_changed_after_kickoff(
    api: Api, repos: FakeRepoProvider, clock: FakeClock
) -> None:
    repos.add(REPO)
    repos.add("https://github.com/acme/rocket-2")
    client = await acknowledged_team(api)
    await client.post("/api/team/repo", json={"repo_url": REPO})
    clock.at = T0

    response = await client.post(
        "/api/team/repo", json={"repo_url": "https://github.com/acme/rocket-2"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "repo_locked"
    assert (await client.get("/api/team")).json()["repo"]["url"] == REPO


async def test_two_teams_cannot_register_the_same_repository(
    api: Api, repos: FakeRepoProvider
) -> None:
    repos.add(REPO)
    organiser = await api.organiser()
    event = await api.event(organiser)
    _, rocket = await api.team(organiser, event["id"], "Rocket")
    _, comet = await api.team(organiser, event["id"], "Comet")
    for client in (rocket, comet):
        await client.post("/api/team/rules-ack")
    await rocket.post("/api/team/repo", json={"repo_url": REPO})

    response = await comet.post("/api/team/repo", json={"repo_url": REPO})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "repo_taken"


async def test_a_repo_url_from_the_import_is_offered_as_a_suggestion(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket", repo_url=REPO)
    link = await api.link(organiser, event["id"], "team", team["id"])
    client, _ = await api.redeem(link["url"])

    body = (await client.get("/api/team")).json()

    assert (body["repo"], body["suggested_repo_url"]) == (None, REPO)


async def test_a_team_only_ever_sees_and_changes_itself(api: Api, repos: FakeRepoProvider) -> None:
    repos.add(REPO)
    organiser = await api.organiser()
    event = await api.event(organiser)
    rocket_team, rocket = await api.team(organiser, event["id"], "Rocket")
    comet_team, comet = await api.team(organiser, event["id"], "Comet")

    await rocket.post("/api/team/rules-ack")
    await rocket.post("/api/team/repo", json={"repo_url": REPO})

    mine = (await rocket.get("/api/team")).json()
    theirs = (await comet.get("/api/team")).json()
    assert (mine["id"], mine["repo"]["url"]) == (rocket_team["id"], REPO)
    assert (theirs["id"], theirs["repo"], theirs["rules_acknowledged_at"]) == (
        comet_team["id"], None, None,
    )  # fmt: skip


# --- the overview: the clock, the claims lock and the team's progress (M7) -------------------

EMPTY_PROGRESS = {
    "has_claims": False,
    "tests_approved": 0,
    "tests_total": 0,
    "has_snapshot": False,
    "has_submitted": False,
}


async def test_the_overview_gives_the_time_and_the_claims_lock(api: Api, clock: FakeClock) -> None:
    _, client = await signed_in_team(api)

    body = (await client.get("/api/team")).json()

    assert datetime.fromisoformat(body["now"]) == clock.now()
    assert datetime.fromisoformat(body["event"]["claims_lock_at"]) == T0 + timedelta(hours=16)
    assert body["progress"] == EMPTY_PROGRESS


async def test_the_overview_counts_the_teams_progress(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    items = [START["items"][0], {"text": "Refuses zero."}, {"text": "Is fair."}]
    await save_claims(flow, {**START, "items": items})
    unclear = {**F01_TEST, "claim_id": "c3", "status": "needs_clarification", "reason": "How?"}
    first, _, _ = await drafted(sessions, flow, F01_TEST, {**F01_TEST, "claim_id": "c2"}, unclear)
    approved = await flow.client.post(
        f"/api/team/tests/{first.id}/approve", json={"spec_hash": first.spec_hash}
    )
    snap = await snapshot(sessions, flow)
    async with sessions.begin() as session:
        session.add(make_submission(flow.team_id, snap.id))

    mine = (await flow.client.get("/api/team")).json()["progress"]
    theirs = (await other.client.get("/api/team")).json()["progress"]

    assert approved.status_code == 200
    assert mine == {
        "has_claims": True,
        "tests_approved": 1,
        "tests_total": 2,
        "has_snapshot": True,
        "has_submitted": True,
    }
    assert theirs == EMPTY_PROGRESS


async def test_the_overview_states_the_model_door_s_terms_when_it_is_on(
    api: Api,
    clock: FakeClock,
    repos: FakeRepoProvider,
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    flow = await started(api, clock)
    assert (await flow.client.get("/api/team")).json()["model_door"] is None

    app = make_app(
        migrated_database_url, tmp_path, clock, repos,
        model_door="prune", door_models="gpt-4.1-nano,claude-haiku-4-5", door_max_calls=20,
        door_egress_hosts="api.weather.test",
    )  # fmt: skip
    on = Api(app)
    try:
        flow = await started(on, clock, name="Door team")
        assert (await flow.client.get("/api/team")).json()["model_door"] == {
            "models": ["gpt-4.1-nano", "claude-haiku-4-5"], "run_budget_usd": 0.05,
            "max_calls": 20, "outside_hosts": ["api.weather.test"],
        }  # fmt: skip
        assert "prune_" not in (await flow.client.get("/api/team")).text
    finally:
        await on.aclose()
        await app.state.engine.dispose()
