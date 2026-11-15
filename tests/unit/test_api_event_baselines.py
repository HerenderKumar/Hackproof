"""The organiser's side of the kickoff baseline (M9, Task 15): starters and the devices feed."""

import io
import tarfile
import uuid
import zipfile
from datetime import timedelta

import httpx
from sqlalchemy import select

from core.db import models
from tests.api_helpers import Api
from tests.baseline_flow import add_device, scan
from tests.fakes import FakeClock
from tests.helpers import Sessions
from tests.priorwork_fakes import block
from tests.team_flow import Flow, started

MARKER = "starter_only_marker_7f3a"


def zipped(files: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return raw.getvalue()


def tarred(files: dict[str, bytes], link: tuple[str, str] | None = None) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link[0])
            info.type, info.linkname = tarfile.SYMTYPE, link[1]
            tar.addfile(info)
    return raw.getvalue()


STARTER = {
    "kit/src/index.js": (f"const {MARKER} = 1;\n" + block(1)).encode(),
    "kit/README.md": b"# a starter",
}


async def upload(
    organiser: httpx.AsyncClient, event_id: str, data: bytes, name: str = "Starter kit"
) -> httpx.Response:
    return await organiser.post(
        f"/api/events/{event_id}/starters",
        data={"name": name},
        files={"file": ("kit.bin", data, "application/octet-stream")},
    )


async def test_an_organiser_adds_lists_and_removes_a_starter(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    added = await upload(organiser, event["id"], zipped(STARTER))

    assert added.status_code == 201, added.text
    starter = added.json()
    assert (starter["name"], starter["files"]) == ("Starter kit", 1)  # the README is not code
    assert starter["runs"] > 0
    listed = (await organiser.get(f"/api/events/{event['id']}/starters")).json()
    assert [s["id"] for s in listed] == [starter["id"]]

    path = f"/api/events/{event['id']}/starters/{starter['id']}"
    assert (await organiser.delete(path)).status_code == 204
    assert (await organiser.delete(path)).status_code == 404
    assert (await organiser.get(f"/api/events/{event['id']}/starters")).json() == []


async def test_a_starter_stores_runs_and_no_code(api: Api, sessions: Sessions) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    added = await upload(organiser, event["id"], tarred(STARTER))
    assert added.status_code == 201, added.text
    assert MARKER not in added.text

    async with sessions() as s:
        stored = (await s.scalars(select(models.Starter))).one()
    assert len(stored.runs) % 8 == 0 and len(stored.runs) // 8 == added.json()["runs"]
    assert MARKER.encode() not in stored.runs
    assert stored.settings == {"k": 25, "lexer_version": "1", "hash_version": "1"}


async def test_the_same_starter_twice_is_one_starter(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    data = zipped(STARTER)
    first = await upload(organiser, event["id"], data)
    again = await upload(organiser, event["id"], data, name="Another name")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    other_event = await api.event(organiser)
    assert (await upload(organiser, other_event["id"], data)).status_code == 201


async def test_a_starter_that_is_unsafe_or_too_large_is_refused(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    big = {f"kit/{n}.js": (block(n) * 2000).encode()[:900_000] for n in range(7)}  # 6.3 MB
    cases = {
        "a link": tarred({"kit/a.js": b"x"}, link=("kit/passwd.js", "/etc/passwd")),
        "a path out": tarred({"../escape.js": b"x"}),
        "an absolute path": zipped({"/etc/cron.js": b"x"}),
        "not an archive": b"just some bytes",
    }
    for name, data in cases.items():
        refused = await upload(organiser, event["id"], data)
        assert refused.status_code == 422, name
        assert refused.json()["detail"]["code"] == "bad_starter", name
    too_large = await upload(organiser, event["id"], zipped(big))
    assert too_large.status_code == 422
    assert too_large.json()["detail"]["code"] == "starter_too_large"
    assert (await organiser.get(f"/api/events/{event['id']}/starters")).json() == []


async def scanned_flow(api: Api, clock: FakeClock) -> Flow:
    return await started(api, clock, kickoff_ago=timedelta(hours=-2))


async def test_the_feed_states_each_device_with_reasons_and_names(
    api: Api, clock: FakeClock
) -> None:
    flow = await scanned_flow(api, clock)
    other = await started(api, clock, name="Aardvark", organiser=flow.organiser, event=flow.event)
    _, code = await add_device(flow, "laptop")
    await add_device(flow, "desktop")
    unreadable = [{"path": "/Users/a/Private", "kind": "folder", "reason": "Permission denied"}]
    await scan(flow, code, {"a.js": b"let a = 1;"}, unreadable=unreadable)

    feed = (await flow.organiser.get(f"/api/events/{flow.event['id']}/baselines")).json()

    assert [t["team_name"] for t in feed["teams"]] == ["Aardvark", "Rocket"]
    assert feed["teams"][0]["devices"] == [] and feed["teams"][0]["team_id"] == str(other.team_id)
    devices = {d["label"]: d for d in feed["teams"][1]["devices"]}
    laptop = devices["laptop"]
    assert laptop["state"] == "incomplete"
    assert laptop["incomplete_reasons"] == ["1 item could not be read"]
    assert laptop["unreadable"] == unreadable
    assert laptop["summary"]["files_found"] == 1 and laptop["platform"] == "test"
    assert devices["desktop"]["state"] == "missing" and devices["desktop"]["summary"] is None


async def test_the_feed_counts_missing_members(api: Api, clock: FakeClock) -> None:
    flow = await scanned_flow(api, clock)
    imported = await api.import_csv(
        flow.organiser, flow.event["id"], "team_name,members\nComet,4\n"
    )
    assert imported.status_code == 200, imported.text
    comet_id = imported.json()["created"][0]["id"]
    link = await api.link(flow.organiser, flow.event["id"], "team", comet_id)
    client, _ = await api.redeem(link["url"])
    comet = Flow(
        api, clock, flow.organiser, flow.event, {"id": comet_id}, client, uuid.UUID(comet_id)
    )
    _, code = await add_device(comet, "laptop")
    await scan(comet, code, {"a.js": b"a"})
    await add_device(comet, "spare")  # a device without a scan does not count

    feed = (await flow.organiser.get(f"/api/events/{flow.event['id']}/baselines")).json()
    counts = {t["team_name"]: (t["members"], t["missing"]) for t in feed["teams"]}
    assert counts == {"Comet": (4, 3), "Rocket": (None, None)}


async def test_the_feed_holds_no_file_content(api: Api, clock: FakeClock) -> None:
    flow = await scanned_flow(api, clock)
    _, code = await add_device(flow, "laptop")
    secret = "device_code_marker_9c1e"
    await scan(flow, code, {"app/main.js": f"const {secret} = 2;\n".encode()})
    response = await flow.organiser.get(f"/api/events/{flow.event['id']}/baselines")
    assert response.status_code == 200
    assert secret not in response.text and "app/main.js" not in response.text


async def test_only_the_organiser_reads_the_feed_and_the_starters(
    api: Api, clock: FakeClock
) -> None:
    flow = await scanned_flow(api, clock)
    _, judge = await api.judge(flow.organiser, flow.event["id"])
    paths = [
        f"/api/events/{flow.event['id']}/baselines",
        f"/api/events/{flow.event['id']}/starters",
    ]
    for client in (flow.client, judge):
        for path in paths:
            assert (await client.get(path)).status_code == 403, path
    for path in paths:
        assert (await api.anonymous().get(path)).status_code == 401
