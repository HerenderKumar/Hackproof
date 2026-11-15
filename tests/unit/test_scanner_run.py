"""The scanner end to end (M9, Task 14): it asks twice, uploads in parts and seals.

`main` runs in a thread against the test app served over real HTTP, so the scanner's own
urllib client, its retries and its files are what is tested.
"""

import asyncio
import io
import json
import os
import urllib.error
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from core.db import models
from core.hashing import hash_json
from tests.api_helpers import Api
from tests.baseline_flow import add_device, shift_event
from tests.fakes import FakeClock
from tests.helpers import Sessions
from tests.priorwork_fakes import block
from tests.scan_server import running
from tests.scanner_module import load
from tests.team_flow import Flow, started

scan = load()
BEFORE_KICKOFF = timedelta(hours=-2)


def tree(root: Path) -> Path:
    files = {
        "app/server.js": block(1).encode(),
        "app/util.py": b'API_TOKEN = "abcd1234efgh5678"\nx = 1\n',
        "app/node_modules/dep/index.js": b"module.exports = 1;",
        "notes.txt": b"not on the list",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


class Answers:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, question: str) -> str:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else "no"


async def run_scan(
    url: str, code: str, root: Path, out: Path, answers: Answers, **kwargs: Any
) -> tuple[int, str]:
    out.mkdir(parents=True, exist_ok=True)
    printed = io.StringIO()
    argv = ["--server", url, "--code", code, "--root", str(root), "--out", str(out)]
    status = await asyncio.to_thread(
        scan.main, argv, ask=answers, out=printed, sleep=lambda _s: None, **kwargs
    )
    return status, printed.getvalue()


async def flow_with_device(api: Api, clock: FakeClock, **kw: Any) -> tuple[Flow, str]:
    flow = await started(api, clock, **kw)
    _, code = await add_device(flow, "laptop")
    return flow, code


async def baselines(sessions: Sessions) -> list[models.Baseline]:
    async with sessions() as s:
        query = select(models.Baseline).order_by(models.Baseline.started_at)
        return list((await s.scalars(query)).all())


async def test_a_scan_shows_its_summary_asks_twice_and_seals(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    _, code = await flow_with_device(api, clock, kickoff_ago=BEFORE_KICKOFF)
    answers = Answers("yes", "yes")
    async with running(api.app) as url:
        status, printed = await run_scan(
            url, code, tree(tmp_path / "tree"), tmp_path / "out", answers
        )

    assert status == 0, printed
    assert len(answers.asked) == 2
    assert "files to send:   2" in printed and "skipped, dependency: 1" in printed
    assert "keys blanked:    1" in printed and "Sealed." in printed
    [baseline] = await baselines(sessions)
    assert baseline.status == "sealed" and baseline.files == 2 and baseline.late is False
    receipt = json.loads((tmp_path / "out" / scan.RECEIPT).read_text())
    fields = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    assert receipt["receipt_hash"] == hash_json(fields) == baseline.receipt_hash


async def test_anything_but_yes_uploads_nothing(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    _, code = await flow_with_device(api, clock, kickoff_ago=BEFORE_KICKOFF)
    async with running(api.app) as url:
        first = await run_scan(url, code, tree(tmp_path / "t"), tmp_path / "o1", Answers("no"))
        second = await run_scan(url, code, tmp_path / "t", tmp_path / "o2", Answers("yes", "nah"))
    assert (first[0], second[0]) == (2, 2)
    assert "Nothing was scanned" in first[1] and "Nothing was sent" in second[1]
    assert await baselines(sessions) == []


async def test_a_limited_walk_is_incomplete_and_says_why(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    _, code = await flow_with_device(api, clock, kickoff_ago=BEFORE_KICKOFF)
    root = tree(tmp_path / "tree")
    async with running(api.app) as url:
        status, printed = await run_scan(url, code, root, tmp_path / "out", Answers("yes", "yes"))
    assert status == 0, printed
    [baseline] = await baselines(sessions)
    assert baseline.complete is False
    assert baseline.incomplete_reasons == [f"limited to {root} by the participant"]


async def test_a_rescan_before_kickoff_replaces_the_first(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    _, code = await flow_with_device(api, clock, kickoff_ago=BEFORE_KICKOFF)
    root = tree(tmp_path / "tree")
    async with running(api.app) as url:
        await run_scan(url, code, root, tmp_path / "o1", Answers("yes", "yes"))
        status, printed = await run_scan(url, code, root, tmp_path / "o2", Answers("yes", "yes"))
    assert status == 0 and "replaces the earlier one" in printed
    assert [b.status for b in await baselines(sessions)] == ["superseded", "sealed"]


async def test_a_scan_after_kickoff_is_marked_late_and_was_told_so(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    _, code = await flow_with_device(api, clock)  # kickoff three hours ago
    async with running(api.app) as url:
        status, printed = await run_scan(
            url, code, tree(tmp_path / "t"), tmp_path / "o", Answers("yes", "yes")
        )
    assert status == 0 and "will be marked late" in printed
    assert json.loads((tmp_path / "o" / scan.RECEIPT).read_text())["late"] is True


async def test_a_scan_after_the_deadline_is_refused_with_the_reason(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    flow, code = await flow_with_device(api, clock)
    await shift_event(
        sessions, flow.event["id"], kickoff_in=-timedelta(hours=5), deadline_in=-timedelta(hours=1)
    )
    answers = Answers("yes", "yes")
    async with running(api.app) as url:
        status, printed = await run_scan(url, code, tree(tmp_path / "t"), tmp_path / "o", answers)
    assert status == 1 and "deadline has passed" in printed
    assert answers.asked == [] and await baselines(sessions) == []


class DropsFirstPart:
    """An opener whose first part upload loses its connection, as a flaky network would."""

    def __init__(self) -> None:
        self.inner = scan.urllib.request.build_opener()
        self.dropped = 0

    def open(self, request: Any, timeout: float) -> Any:
        if request.get_method() == "PUT" and not self.dropped:
            self.dropped += 1
            raise urllib.error.URLError("connection reset by peer")
        return self.inner.open(request, timeout=timeout)


async def test_a_dropped_connection_is_retried_and_the_scan_still_seals(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    _, code = await flow_with_device(api, clock, kickoff_ago=BEFORE_KICKOFF)
    opener = DropsFirstPart()
    async with running(api.app) as url:
        status, printed = await run_scan(
            url, code, tree(tmp_path / "t"), tmp_path / "o", Answers("yes", "yes"), opener=opener
        )
    assert status == 0, printed
    assert opener.dropped == 1
    [baseline] = await baselines(sessions)
    assert baseline.status == "sealed" and len(baseline.parts) == 1


async def test_the_size_limit_seals_what_it_has(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    root = tmp_path / "t"
    root.mkdir()
    sizes = []
    for n in range(3):
        (root / f"{n}.js").write_text(block(n + 40))
        sizes.append((root / f"{n}.js").stat().st_size)
    limit = sizes[0] + sizes[1] - 1  # the first file fits, the second would pass the limit
    api.app.state.settings = api.app.state.settings.model_copy(update={"baseline_max_bytes": limit})
    _, code = await flow_with_device(api, clock, kickoff_ago=BEFORE_KICKOFF)
    async with running(api.app) as url:
        status, printed = await run_scan(url, code, root, tmp_path / "o", Answers("yes", "yes"))
    assert status == 0, printed
    assert "size limit was reached" in printed
    [baseline] = await baselines(sessions)
    assert "size limit reached" in baseline.incomplete_reasons
    assert baseline.files == 1


async def test_the_file_list_and_the_receipt_are_the_only_files_written(
    api: Api, clock: FakeClock, tmp_path: Path
) -> None:
    _, code = await flow_with_device(api, clock, kickoff_ago=BEFORE_KICKOFF)
    root = tree(tmp_path / "tree")
    before = sorted(os.walk(root))
    async with running(api.app) as url:
        status, _ = await run_scan(url, code, root, tmp_path / "out", Answers("yes", "yes"))
    assert status == 0
    assert sorted(os.walk(root)) == before
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(
        [scan.FILE_LIST, scan.RECEIPT]
    )
    listed = (tmp_path / "out" / scan.FILE_LIST).read_text()
    assert "server.js" in listed and "notes.txt" not in listed and "node_modules" not in listed


def test_a_plain_http_server_that_is_not_local_is_refused(tmp_path: Path) -> None:
    printed = io.StringIO()
    argv = ["--server", "http://hackproof.example.com", "--code", "x", "--out", str(tmp_path)]
    assert scan.main(argv, ask=Answers("yes"), out=printed) == 1
    assert "not https" in printed.getvalue()
    assert list(tmp_path.iterdir()) == []


async def test_a_wrong_code_ends_with_the_servers_message(api: Api, tmp_path: Path) -> None:
    async with running(api.app) as url:
        status, printed = await run_scan(url, "wrong-code", tmp_path, tmp_path / "o", Answers())
    assert status == 1 and "This device code is not valid." in printed
