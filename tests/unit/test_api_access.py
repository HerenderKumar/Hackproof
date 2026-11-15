"""Access control (invariant 7, section 12.4): every route declares its roles, and they hold.

The checks walk the application's own route table, so a route added later is covered
without anyone remembering to add a test for it.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from fastapi.routing import APIRoute

from tests.api_helpers import Api, make_app
from tests.fakes import FakeClock, FakeRepoProvider
from tests.helpers import Sessions
from tests.team_flow import (
    F01_TEST,
    HIDDEN,
    approve_all,
    drafted,
    finished_self_check,
    save_claims,
    snapshot,
    started,
    with_repo,
)

ROLES = {"organiser", "judge", "team"}
# The only routes that work without a session: they are how a session starts.
PUBLIC = {("POST", "/api/auth/organiser"), ("GET", "/api/auth/redeem")}


def allowed_roles(route: APIRoute) -> frozenset[str] | None:
    """The roles a route's guard allows, or None if the route has no guard."""

    def search(dependant: Any) -> frozenset[str] | None:
        roles: frozenset[str] | None = getattr(dependant.call, "allowed_roles", None)
        if roles is not None:
            return roles
        for inner in dependant.dependencies:
            found = search(inner)
            if found is not None:
                return found
        return None

    return search(route.dependant)


def walk(routes: Any) -> Iterator[APIRoute]:
    """Every API route, however deeply the routers that hold it are nested."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        else:
            inner = getattr(route, "original_router", route)
            yield from walk(getattr(inner, "routes", []))


def api_routes(api: Api) -> list[tuple[str, APIRoute]]:
    found = [
        (method, route)
        for route in walk(api.app.routes)
        if route.path.startswith("/api/")
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]
    # Guard against a walk that silently finds nothing and makes every check below pass.
    assert len(found) >= 13, found
    return found


def concrete(path: str) -> str:
    """The path with every `{parameter}` replaced by an ID that matches nothing."""
    parts = [str(uuid.uuid4()) if p.startswith("{") else p for p in path.split("/")]
    return "/".join(parts)


async def test_every_api_route_declares_its_roles(api: Api) -> None:
    unguarded = {(m, r.path) for m, r in api_routes(api) if allowed_roles(r) is None}

    assert unguarded == PUBLIC


async def test_nothing_else_is_served_under_api_outside_local_development(
    api: Api, migrated_database_url: str, tmp_path: Path
) -> None:
    # The interactive docs describe every route. They are a development aid only.
    local = api.anonymous()
    assert (await local.get("/api/docs")).status_code == 200
    assert (await local.get("/api/openapi.json")).status_code == 200

    app = make_app(
        migrated_database_url, tmp_path, FakeClock(api.app.state.clock.now()), FakeRepoProvider(),
        app_env="production",
    )  # fmt: skip
    production = Api(app).anonymous()
    statuses = [
        (await production.get(path)).status_code
        for path in ("/api/docs", "/api/openapi.json", "/redoc", "/docs")
    ]
    await production.aclose()
    await app.state.engine.dispose()

    assert statuses == [404, 404, 404, 404]


async def test_no_guarded_route_answers_without_a_session(api: Api) -> None:
    anonymous = api.anonymous()

    checked = 0
    for method, route in api_routes(api):
        if (method, route.path) in PUBLIC:
            continue
        response = await anonymous.request(method, concrete(route.path))
        assert response.status_code == 401, (method, route.path)
        checked += 1
    assert checked >= 11


async def test_no_route_answers_a_role_it_does_not_allow(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    _, team = await api.team(organiser, event["id"])
    _, judge = await api.judge(organiser, event["id"])
    clients: dict[str, httpx.AsyncClient] = {"organiser": organiser, "judge": judge, "team": team}

    checked = 0
    for method, route in api_routes(api):
        allowed = allowed_roles(route)
        if allowed is None:
            continue
        for role in ROLES - allowed:
            response = await clients[role].request(method, concrete(route.path))
            assert response.status_code == 403, (role, method, route.path)
            assert response.json()["detail"]["code"] == "forbidden"
            checked += 1
    assert checked >= 20


async def test_a_judge_cannot_use_the_organisers_routes(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")
    _, judge = await api.judge(organiser, event["id"])
    event_url = f"/api/events/{event['id']}"

    attempts = [
        await judge.post("/api/events", json={}),
        await judge.get(event_url),
        await judge.patch(event_url, json={"name": "Mine now"}),
        await judge.post(f"{event_url}/teams/import", files={"file": ("t.csv", b"team_name\nX\n")}),
        await judge.post(f"{event_url}/judges", json={"name": "Friend"}),
        await judge.post(f"{event_url}/links", json={"role": "team", "subject_id": team["id"]}),
        await judge.delete(f"/api/links/{uuid.uuid4()}"),
    ]

    assert [r.status_code for r in attempts] == [403] * len(attempts)
    assert (await organiser.get(event_url)).json()["name"] == "Winter Hack"


async def test_a_team_cannot_use_the_organisers_or_another_roles_routes(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    other = await api.add_team(organiser, event["id"], "Comet")
    _, team = await api.team(organiser, event["id"], "Rocket")

    make_link = await team.post(
        f"/api/events/{event['id']}/links", json={"role": "team", "subject_id": other["id"]}
    )
    read_event = await team.get(f"/api/events/{event['id']}")

    assert (make_link.status_code, read_event.status_code) == (403, 403)


async def test_the_organiser_and_judges_have_no_team_page(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    _, judge = await api.judge(organiser, event["id"])

    assert (await organiser.get("/api/team")).status_code == 403
    assert (await judge.get("/api/team")).status_code == 403


# --- the team routes (M7): hidden cases never leave, and a team sees only itself -------------


async def test_no_team_response_ever_holds_a_hidden_case(
    api: Api, clock: FakeClock, repos: FakeRepoProvider, sessions: Sessions, tmp_path: Path
) -> None:
    flow = await started(api, clock)
    bodies: list[str] = []

    async def keep(response: httpx.Response) -> None:
        await response.aread()
        bodies.append(response.text)

    flow.client.event_hooks["response"].append(keep)
    client = flow.client
    await save_claims(flow)
    [spec] = await drafted(sessions, flow, F01_TEST)
    await client.get("/api/team/tests")
    await approve_all(flow)
    await with_repo(flow, repos)
    await client.post("/api/team/snapshots", json={})
    snap = await snapshot(sessions, flow)
    await client.get("/api/team/snapshots")
    started_check = await client.post(
        f"/api/team/snapshots/{snap.id}/self-check", json={"client_request_id": "request-0001"}
    )
    job_id = uuid.UUID(started_check.json()["job_id"])
    log = tmp_path / "runs" / "check" / "app.log"
    log.parent.mkdir(parents=True)
    log.write_text("POST /split 200\n")
    await finished_self_check(
        sessions, job_id, results=[(spec, "p1", "pass")], logs={"app": log, "build": log}
    )
    await client.get(f"/api/team/self-checks/{job_id}")
    await client.get(f"/api/team/self-checks/{job_id}/logs/app")
    await client.post(
        "/api/team/submissions",
        json={
            "snapshot_id": str(snap.id),
            "mode": "sandbox",
            "reuse_declaration": {
                "starter_code": False, "earlier_work": False, "ai_assistance": False,
            },
            "idempotency_key": "submit-key-0001",
        },
    )  # fmt: skip
    await client.get("/api/team/submissions/current")
    await client.get("/api/team")
    await client.get("/api/team/claims")

    assert len(bodies) == 14  # every request above
    assert [b for b in bodies if "final_cases" in b or str(HIDDEN) in b] == []


async def test_every_team_route_answers_only_for_its_own_team(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    flow = await started(api, clock)
    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    await save_claims(flow)
    [spec] = await drafted(sessions, flow, F01_TEST)
    snap = await snapshot(sessions, flow)
    started_check = await flow.client.post(
        f"/api/team/snapshots/{snap.id}/self-check", json={"client_request_id": "request-0001"}
    )
    job_id = started_check.json()["job_id"]
    log = tmp_path / "runs" / "check" / "app.log"
    log.parent.mkdir(parents=True)
    log.write_text("POST /split 200\n")
    await finished_self_check(sessions, uuid.UUID(job_id), logs={"app": log, "build": log})
    b = other.client

    refused = {
        "approve A's test": await b.post(
            f"/api/team/tests/{spec.id}/approve", json={"spec_hash": spec.spec_hash}
        ),
        "self-check A's snapshot": await b.post(
            f"/api/team/snapshots/{snap.id}/self-check", json={"client_request_id": "request-0002"}
        ),
        "read A's self-check": await b.get(f"/api/team/self-checks/{job_id}"),
        "read A's build log": await b.get(f"/api/team/self-checks/{job_id}/logs/build"),
        "read A's app log": await b.get(f"/api/team/self-checks/{job_id}/logs/app"),
        "submit A's snapshot": await b.post(
            "/api/team/submissions",
            json={
                "snapshot_id": str(snap.id),
                "mode": "sandbox",
                "reuse_declaration": {
                    "starter_code": False, "earlier_work": False, "ai_assistance": False,
                },
                "idempotency_key": "submit-key-0001",
            },
        ),
    }  # fmt: skip

    assert {name: r.status_code for name, r in refused.items()} == {
        "approve A's test": 404,
        "self-check A's snapshot": 404,
        "read A's self-check": 404,
        "read A's build log": 404,
        "read A's app log": 404,
        "submit A's snapshot": 404,
    }
    assert (await b.get("/api/team/claims")).json()["current"] is None
    assert (await b.get("/api/team/tests")).json()["claims"] == []
    assert (await b.get("/api/team/snapshots")).json() == {"snapshots": [], "pending": []}
    assert (await b.get("/api/team/submissions/current")).status_code == 404
    assert (await b.get("/api/team")).json()["id"] == str(other.team_id)
