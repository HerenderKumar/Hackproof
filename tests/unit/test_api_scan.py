"""The kickoff scanner's routes (M9, Task 12): config, start, parts, seal, and the receipt."""

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from core.db import models
from core.hashing import hash_json
from tests.api_helpers import Api
from tests.baseline_flow import (
    add_device,
    device_client,
    make_part,
    pack,
    put_part,
    scan,
    seal_body,
    shift_event,
)
from tests.fakes import FakeClock
from tests.helpers import Sessions, all_jobs
from tests.team_flow import Flow, started

BEFORE_KICKOFF = timedelta(hours=-2)  # kickoff two hours from now


async def before_kickoff(api: Api, clock: FakeClock) -> Flow:
    return await started(api, clock, kickoff_ago=BEFORE_KICKOFF)


async def open_scan(flow: Flow, code: str) -> tuple[httpx.AsyncClient, str, dict[str, Any]]:
    client = device_client(flow, code)
    config = (await client.get("/api/scan/config")).json()
    started_scan = await client.post(
        "/api/scan/start", json={"scanner_version": "1", "platform": "test"}
    )
    assert started_scan.status_code == 200, started_scan.text
    return client, started_scan.json()["baseline_id"], config


async def baselines(sessions: Sessions) -> list[models.Baseline]:
    async with sessions() as s:
        return list(
            (await s.scalars(select(models.Baseline).order_by(models.Baseline.started_at))).all()
        )


async def test_a_wrong_or_missing_code_is_refused_everywhere(api: Api, clock: FakeClock) -> None:
    flow = await before_kickoff(api, clock)
    await add_device(flow, "laptop")
    some = str(uuid.uuid4())
    for headers in ({}, {"X-Device-Code": "not-a-code"}):
        client = api.anonymous()
        client.headers.update(headers)
        answers = [
            await client.get("/api/scan/config"),
            await client.post("/api/scan/start", json={"scanner_version": "1", "platform": "x"}),
            await client.put(
                f"/api/scan/{some}/parts/1", content=b"x", headers={"X-Part-Sha256": "0"}
            ),
            await client.post(f"/api/scan/{some}/seal", json={}),
        ]
        assert [a.status_code for a in answers] == [401] * 4, headers
        assert {a.json()["detail"]["code"] for a in answers} == {"bad_device_code"}


async def test_config_states_what_will_be_collected(api: Api, clock: FakeClock) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    config = (await device_client(flow, code).get("/api/scan/config")).json()
    assert (config["team_name"], config["device_label"]) == ("Rocket", "laptop")
    assert config["event_name"] == flow.event["name"]
    assert ".py" in config["extensions"] and ".env" not in config["extensions"]
    assert (config["max_file_bytes"], config["max_bytes"]) == (1048576, 2147483648)
    assert config["part_raw_bytes"] == 3_500_000 and config["retention_days"] == 30
    assert (config["will_be_late"], config["replaces_earlier"]) == (False, False)


async def test_a_scan_before_kickoff_seals_on_time_with_a_receipt_that_recomputes(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    receipt = await scan(flow, code, {"src/a.js": b"let a = 1;\n", "b.py": b"x = 2\n"})

    fields = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    assert receipt["receipt_hash"] == hash_json(fields)
    assert (receipt["late"], receipt["complete"], receipt["files"]) == (False, True, 2)
    assert receipt["team_name"] == "Rocket" and receipt["device_label"] == "laptop"
    [baseline] = await baselines(sessions)
    assert (baseline.status, baseline.receipt_hash) == ("sealed", receipt["receipt_hash"])
    assert [(j.kind, j.payload) for j in await all_jobs(sessions)] == [
        ("index_baseline", {"baseline_id": str(baseline.id)})
    ]
    assert (tmp_path / "baselines" / str(baseline.id) / "parts" / "1.bin").is_file()
    devices = (await flow.client.get("/api/team/devices")).json()["devices"]
    assert [(d["state"], d["receipt_hash"]) for d in devices] == [
        ("complete", receipt["receipt_hash"])
    ]


async def test_the_same_part_sent_twice_is_stored_once(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    client, baseline_id, _ = await open_scan(flow, code)
    part = make_part({"a.js": b"let a = 1;"})

    first = await put_part(client, baseline_id, 1, part)
    again = await put_part(client, baseline_id, 1, part)
    other = await put_part(client, baseline_id, 1, make_part({"b.js": b"b"}))

    assert (first.json()["already_had"], again.json()["already_had"]) == (False, True)
    assert other.status_code == 409 and other.json()["detail"]["code"] == "part_conflict"
    [baseline] = await baselines(sessions)
    assert len(baseline.parts) == 1 and baseline.bytes_received == len(part)


async def test_a_part_for_another_devices_baseline_is_refused(api: Api, clock: FakeClock) -> None:
    flow = await before_kickoff(api, clock)
    _, mine = await add_device(flow, "laptop")
    _, theirs = await add_device(flow, "desktop")
    _, baseline_id, _ = await open_scan(flow, mine)
    other = device_client(flow, theirs)
    refused = await put_part(other, baseline_id, 1, make_part({"a.js": b"a"}))
    assert refused.status_code == 404 and refused.json()["detail"]["code"] == "baseline_not_open"
    sealed = await other.post(
        f"/api/scan/{baseline_id}/seal", json=seal_body({"extensions": []}, {}, 0)
    )
    assert sealed.status_code == 404


async def test_a_part_after_the_seal_is_refused(api: Api, clock: FakeClock) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    receipt = await scan(flow, code, {"a.js": b"a"})
    late_part = await put_part(
        device_client(flow, code), receipt["baseline_id"], 2, make_part({"b.js": b"b"})
    )
    assert late_part.status_code == 404


async def test_a_bad_part_is_refused_and_nothing_is_written(
    api: Api, clock: FakeClock, tmp_path: Path, sessions: Sessions
) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    client, baseline_id, _ = await open_scan(flow, code)
    entry = {"path": "a.js", "size": 1, "sha256_device": "a" * 64, "sha256_uploaded": "a" * 64}
    named_wrong = pack([{**entry, "keys_blanked": 0}], [("a" * 64, b"x")])

    bad = await put_part(client, baseline_id, 1, named_wrong)
    mismatch = await client.put(
        f"/api/scan/{baseline_id}/parts/1",
        content=make_part({"a.js": b"a"}),
        headers={"X-Part-Sha256": "0" * 64},
    )

    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_part"
    assert "SHA-256" in bad.json()["detail"]["message"]
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "part_hash_mismatch"
    assert not list((tmp_path / "baselines").rglob("*.bin"))
    [baseline] = await baselines(sessions)
    assert (baseline.parts, baseline.bytes_received) == ([], 0)


async def test_a_part_over_four_mebibytes_is_refused(api: Api, clock: FakeClock) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    client, baseline_id, _ = await open_scan(flow, code)
    too_big = b"\0" * (4 * 1024 * 1024 + 1)
    refused = await put_part(client, baseline_id, 1, too_big)
    assert refused.status_code == 413 and refused.json()["detail"]["code"] == "part_too_large"


async def test_the_size_limit_closes_the_baseline(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    settings = api.app.state.settings
    first, second = make_part({"a.js": b"a" * 100}), make_part({"b.js": b"b" * 100})
    api.app.state.settings = settings.model_copy(
        update={"baseline_max_bytes": len(first) + len(second) - 1}
    )
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    client, baseline_id, config = await open_scan(flow, code)

    assert (await put_part(client, baseline_id, 1, first)).status_code == 200
    full = await put_part(client, baseline_id, 2, second)
    assert full.status_code == 409 and full.json()["detail"]["code"] == "baseline_full"

    body = seal_body(config, {"a.js": b"a" * 100}, 1)
    body["summary"]["size_limit_reached"] = True
    sealed = await client.post(f"/api/scan/{baseline_id}/seal", json=body)
    assert sealed.status_code == 200 and sealed.json()["complete"] is False
    [baseline] = await baselines(sessions)
    assert baseline.incomplete_reasons == ["size limit reached"]


async def test_a_seal_with_a_part_missing_is_refused(api: Api, clock: FakeClock) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    client, baseline_id, config = await open_scan(flow, code)
    await put_part(client, baseline_id, 1, make_part({"a.js": b"a"}))
    sealed = await client.post(
        f"/api/scan/{baseline_id}/seal", json=seal_body(config, {"a.js": b"a"}, 2)
    )
    assert sealed.status_code == 409 and sealed.json()["detail"]["code"] == "parts_missing"


async def test_a_rescan_before_kickoff_replaces_the_first_and_deletes_its_code(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    first = await scan(flow, code, {"a.js": b"first"})
    config = (await device_client(flow, code).get("/api/scan/config")).json()
    assert config["replaces_earlier"] is True
    second = await scan(flow, code, {"a.js": b"second"})

    statuses = {str(b.id): b.status for b in await baselines(sessions)}
    assert statuses == {first["baseline_id"]: "superseded", second["baseline_id"]: "sealed"}
    assert not (tmp_path / "baselines" / first["baseline_id"]).exists()
    assert (tmp_path / "baselines" / second["baseline_id"]).is_dir()


async def test_a_second_scan_after_kickoff_is_refused(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    await scan(flow, code, {"a.js": b"a"})
    await shift_event(sessions, flow.event["id"], kickoff_in=-timedelta(minutes=5))

    again = await device_client(flow, code).post(
        "/api/scan/start", json={"scanner_version": "1", "platform": "test"}
    )
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_scanned"


async def test_a_first_scan_after_kickoff_is_late(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await started(api, clock)  # kickoff three hours ago
    _, code = await add_device(flow, "laptop")
    config = (await device_client(flow, code).get("/api/scan/config")).json()
    assert config["will_be_late"] is True
    receipt = await scan(flow, code, {"a.js": b"a"})
    assert receipt["late"] is True
    [device] = (await flow.client.get("/api/team/devices")).json()["devices"]
    assert device["late"] is True


async def test_no_scan_starts_or_seals_after_the_deadline(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    client, baseline_id, config = await open_scan(flow, code)
    await put_part(client, baseline_id, 1, make_part({"a.js": b"a"}))
    await shift_event(
        sessions,
        flow.event["id"],
        kickoff_in=-timedelta(hours=5),
        deadline_in=-timedelta(minutes=1),
    )

    sealed = await client.post(
        f"/api/scan/{baseline_id}/seal", json=seal_body(config, {"a.js": b"a"}, 1)
    )
    started_again = await client.post(
        "/api/scan/start", json={"scanner_version": "1", "platform": "test"}
    )
    assert sealed.status_code == 409 and sealed.json()["detail"]["code"] == "scan_closed"
    assert started_again.status_code == 409
    assert started_again.json()["detail"]["code"] == "scan_closed"


async def test_what_could_not_be_read_makes_the_baseline_incomplete_with_reasons(
    api: Api, clock: FakeClock, sessions: Sessions
) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    unreadable = [
        {"path": "/Users/a/locked", "kind": "folder", "reason": "Permission denied"},
        {"path": "/Users/a/x.js", "kind": "file", "reason": "Operation not permitted"},
    ]
    receipt = await scan(
        flow, code, {"a.js": b"a"}, unreadable=unreadable, unreadable_more=1,
        limited_to=["/Users/a/projects"],
    )  # fmt: skip
    assert receipt["complete"] is False
    [baseline] = await baselines(sessions)
    assert baseline.incomplete_reasons == [
        "3 items could not be read",
        "limited to /Users/a/projects by the participant",
    ]
    assert baseline.unreadable == unreadable and baseline.unreadable_more == 1
    [device] = (await flow.client.get("/api/team/devices")).json()["devices"]
    assert device["state"] == "incomplete" and len(device["incomplete_reasons"]) == 2


async def test_a_seal_naming_other_extensions_than_the_events_is_refused(
    api: Api, clock: FakeClock
) -> None:
    flow = await before_kickoff(api, clock)
    _, code = await add_device(flow, "laptop")
    client, baseline_id, config = await open_scan(flow, code)
    body = seal_body({"extensions": [".js"]}, {}, 0)
    refused = await client.post(f"/api/scan/{baseline_id}/seal", json=body)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "extensions_changed"
    upper = seal_body({"extensions": [e.upper() for e in config["extensions"]]}, {}, 0)
    assert (await client.post(f"/api/scan/{baseline_id}/seal", json=upper)).status_code == 200


async def test_the_scanner_file_is_served_as_text(api: Api) -> None:
    response = await api.anonymous().get("/api/scanner/hackproof_scan.py")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/")
    assert 'SCANNER_VERSION = "1"' in response.text
