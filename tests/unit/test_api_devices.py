"""A team's devices for the kickoff scan (M9, Task 12)."""

from datetime import timedelta

from sqlalchemy import select

from api.auth import hash_token
from core.db import models
from tests.api_helpers import Api
from tests.baseline_flow import add_device, scan, shift_event
from tests.fakes import FakeClock
from tests.helpers import Sessions
from tests.team_flow import at, move_to, started


async def test_a_team_adds_a_device_and_gets_its_code_once(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)
    response = await flow.client.post("/api/team/devices", json={"label": "  Asha's laptop "})

    assert response.status_code == 201, response.text
    body = response.json()
    code = body["code"]
    assert body["device"]["label"] == "Asha's laptop" and body["device"]["state"] == "missing"
    assert code in body["command"] and body["command"].startswith("python3 hackproof_scan.py")
    assert body["download_url"].endswith("/api/scanner/hackproof_scan.py")

    listed = await flow.client.get("/api/team/devices")
    assert listed.status_code == 200
    assert code not in listed.text
    view = listed.json()
    assert [d["label"] for d in view["devices"]] == ["Asha's laptop"]
    assert view["can_add"] is True and ".py" in view["extensions"]
    async with sessions() as s:
        stored = (await s.scalars(select(models.BaselineDevice))).one()
    assert stored.code_hash == hash_token(code) and code not in stored.code_hash


async def test_a_team_has_at_most_twelve_devices_with_distinct_labels(
    api: Api, clock: FakeClock
) -> None:
    flow = await started(api, clock)
    for n in range(12):
        await add_device(flow, f"device {n}")
    taken = await flow.client.post("/api/team/devices", json={"label": "DEVICE 3"})
    assert taken.status_code == 409  # twelve already: the limit is checked first
    assert taken.json()["detail"]["code"] == "too_many_devices"
    assert (await flow.client.get("/api/team/devices")).json()["can_add"] is False

    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    await add_device(other, "laptop")
    again = await other.client.post("/api/team/devices", json={"label": "Laptop"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "device_label_taken"
    for label in ("", "   ", "x" * 61):
        bad = await other.client.post("/api/team/devices", json={"label": label})
        assert bad.status_code == 422, label


async def test_a_device_without_a_baseline_can_be_removed_and_one_with_cannot(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock, kickoff_ago=timedelta(hours=-2))
    await shift_event(sessions, flow.event["id"], kickoff_in=timedelta(hours=2))
    spare, _ = await add_device(flow, "spare")
    scanned, code = await add_device(flow, "laptop")
    await scan(flow, code, {"a.js": b"let a = 1;"})

    removed = await flow.client.delete(f"/api/team/devices/{spare['id']}")
    kept = await flow.client.delete(f"/api/team/devices/{scanned['id']}")

    assert removed.status_code == 204
    assert kept.status_code == 409 and kept.json()["detail"]["code"] == "device_has_baseline"
    labels = [d["label"] for d in (await flow.client.get("/api/team/devices")).json()["devices"]]
    assert labels == ["laptop"]


async def test_team_a_cannot_see_or_remove_team_bs_devices(api: Api, clock: FakeClock) -> None:
    a = await started(api, clock)
    b = await started(api, clock, name="Comet", organiser=a.organiser, event=a.event)
    device, _ = await add_device(a, "laptop")

    assert (await b.client.get("/api/team/devices")).json()["devices"] == []
    refused = await b.client.delete(f"/api/team/devices/{device['id']}")
    assert refused.status_code == 404
    assert len((await a.client.get("/api/team/devices")).json()["devices"]) == 1


async def test_no_device_is_added_after_the_deadline(api: Api, clock: FakeClock) -> None:
    flow = await started(api, clock, ends_in=timedelta(hours=2))  # inside the session's 12 h
    move_to(clock, at(flow.event["deadline_at"]) + timedelta(minutes=1))
    late = await flow.client.post("/api/team/devices", json={"label": "laptop"})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "deadline_passed"
    assert (await flow.client.get("/api/team/devices")).json()["can_add"] is False


async def test_a_device_code_never_looks_like_an_option(api: Api, clock: FakeClock) -> None:
    # token_urlsafe can start with "-", which `--code <code>` would read as another option.
    flow = await started(api, clock)
    codes = [(await add_device(flow, f"device {n}"))[1] for n in range(12)]
    assert all(code.startswith("hpd-") and len(code) > 40 for code in codes)
