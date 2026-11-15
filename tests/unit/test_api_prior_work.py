"""Reading the prior-work report, and explaining it (M9, Task 17)."""

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text

from core import queue
from core.db import models
from tests.api_helpers import BASELINE_KEY, Api
from tests.baseline_flow import indexed_baseline, shift_event, submitted
from tests.fakes import FakeClock
from tests.helpers import Sessions, claimed, enqueue
from tests.priorwork_fakes import block
from tests.team_flow import Flow, started
from tests.unit.test_api_access import ROLES, allowed_roles, api_routes
from worker import lanes
from worker.prior_work_job import make_prior_work_handler
from worker.purge_job import make_purge_handler

KEY = bytes.fromhex(BASELINE_KEY)
MARKER = "const device_only_marker_e41 = 1;\n"
OLD = MARKER + block(1)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(1) as executor:
        yield executor


@dataclass
class Reported:
    flow: Flow
    submission: Any
    judge: httpx.AsyncClient


async def run_prior_work(
    api: Api, sessions: Sessions, pool: ThreadPoolExecutor, submission: Any, key: str | None = None
) -> None:
    job = await claimed(
        sessions, "prior_work", key=key, submission_id=submission.id, team_id=submission.team_id
    )
    handler = make_prior_work_handler(sessions, pool, api.app.state.settings)
    await lanes.run_job(job, sessions, {"prior_work": handler})


async def reported(
    api: Api,
    clock: FakeClock,
    sessions: Sessions,
    tmp_path: Path,
    pool: ThreadPoolExecutor,
    *,
    final: dict[str, bytes] | None = None,
    opened: bool = True,
    name: str = "Rocket",
    flow: Flow | None = None,
) -> Reported:
    """A team with a device scan, a submission made an hour before the deadline, its report
    made, and (when `opened`) the deadline passed by the database's clock."""
    if flow is None:
        flow = await started(api, clock, name=name)
    else:
        flow = await started(api, clock, name=name, organiser=flow.organiser, event=flow.event)
    await indexed_baseline(sessions, tmp_path, KEY, flow.team_id, {"old/lib.js": OLD.encode()})
    files = final or {"src/app.js": (OLD + block(2)).encode()}
    submission = await submitted(
        sessions, tmp_path, flow.team_id, files,
        submitted_at=datetime.now(UTC) - timedelta(hours=1),
    )  # fmt: skip
    await run_prior_work(api, sessions, pool, submission)
    if opened:
        await shift_event(
            sessions, flow.event["id"], kickoff_in=-timedelta(hours=5),
            deadline_in=-timedelta(minutes=1),
        )  # fmt: skip
    _, judge = await api.judge(flow.organiser, flow.event["id"])
    return Reported(flow, submission, judge)


def judge_path(r: Reported) -> str:
    return f"/api/judge/submissions/{r.submission.id}/prior-work"


async def test_a_team_gets_nothing_before_the_deadline(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool, opened=False)
    team = r.flow.client
    answers = [
        await team.get("/api/team/prior-work"),
        await team.get(f"/api/team/prior-work/matches/{uuid.uuid4()}"),
        await team.put("/api/team/prior-work/explanations", json={"match_key": "", "text": "x"}),
    ]
    assert [a.status_code for a in answers] == [409, 409, 409]
    for answer in answers:
        assert answer.json() == {
            "detail": {
                "code": "report_not_open",
                "message": "The prior-work report opens once the deadline has passed.",
            }
        }
    assert (await r.judge.get(judge_path(r))).status_code == 200  # judges read it at once


async def test_after_the_deadline_a_team_reads_what_the_judge_reads(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    team = (await r.flow.client.get("/api/team/prior-work")).json()
    judge = (await r.judge.get(judge_path(r))).json()

    assert team["status"] == "ready" and team["matches_total"] == 1
    for field in ("totals", "files", "matches", "devices", "submission_id"):
        assert team[field] == judge[field], field
    assert (team["can_explain"], judge["can_explain"]) == (True, False)
    assert 0 < team["totals"]["percent"] < 100
    assert [d["label"] for d in team["devices"]] == ["laptop"]


async def test_a_match_is_read_as_two_excerpts_with_their_line_numbers(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    final = {"src/app.js": ("// new header\n" * 8 + OLD + block(2)).encode()}
    r = await reported(api, clock, sessions, tmp_path, pool, final=final)
    [match] = (await r.flow.client.get("/api/team/prior-work")).json()["matches"]

    for client, path in (
        (r.flow.client, f"/api/team/prior-work/matches/{match['id']}"),
        (r.judge, f"/api/judge/prior-work/matches/{match['id']}"),
    ):
        excerpts = (await client.get(path)).json()
        assert excerpts["old"]["first_line"] == 1 and excerpts["old"]["text"].startswith(
            MARKER.strip()
        )
        assert excerpts["final"]["first_line"] == 9 - 5  # five lines of context before line 9
        assert MARKER.strip() in excerpts["final"]["text"]


async def explain(r: Reported, key: str, words: str) -> httpx.Response:
    return await r.flow.client.put(
        "/api/team/prior-work/explanations", json={"match_key": key, "text": words}
    )


async def test_a_team_explains_a_match_and_the_judge_sees_it_with_the_match(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    [match] = (await r.flow.client.get("/api/team/prior-work")).json()["matches"]

    saved = await explain(r, match["match_key"], "Our own library from last year's course.")
    whole = await explain(r, "", "We said so in the reuse declaration.")

    assert saved.status_code == 200 and whole.status_code == 200
    judge = (await r.judge.get(judge_path(r))).json()
    assert judge["matches"][0]["explanation"] == "Our own library from last year's course."
    assert judge["explanation"] == "We said so in the reuse declaration."
    assert judge["matches"][0]["explained_at"] is not None


async def test_each_save_is_a_new_row_and_the_newest_shows(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    [match] = (await r.flow.client.get("/api/team/prior-work")).json()["matches"]
    for words in ("first", "second", "third"):
        await explain(r, match["match_key"], words)
    assert (await r.judge.get(judge_path(r))).json()["matches"][0]["explanation"] == "third"
    await explain(r, match["match_key"], "")
    assert (await r.judge.get(judge_path(r))).json()["matches"][0]["explanation"] is None
    async with sessions() as s:
        assert await s.scalar(text("SELECT count(*) FROM match_explanations")) == 4


async def test_an_explanation_survives_a_rerun_of_the_report(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    [match] = (await r.flow.client.get("/api/team/prior-work")).json()["matches"]
    await explain(r, match["match_key"], "from a tutorial")

    await run_prior_work(api, sessions, pool, r.submission, key=f"prior_work:rerun:{uuid.uuid4()}")

    [again] = (await r.judge.get(judge_path(r))).json()["matches"]
    assert again["id"] != match["id"] and again["match_key"] == match["match_key"]
    assert again["explanation"] == "from a tutorial"


async def test_no_explanation_is_saved_once_scores_are_locked(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    async with sessions.begin() as s:
        await s.execute(
            text("UPDATE events SET scores_locked = true WHERE id = :id"),
            {"id": r.flow.event["id"]},
        )
    refused = await explain(r, "", "too late")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "scores_locked"
    assert (await r.flow.client.get("/api/team/prior-work")).json()["can_explain"] is False


async def test_an_explanation_of_an_unknown_match_or_over_2000_characters_is_refused(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    unknown = await explain(r, "f" * 32, "what match?")
    too_long = await explain(r, "", "x" * 2001)
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "match_not_found"
    assert too_long.status_code == 422
    assert (await explain(r, "", "x" * 2000)).status_code == 200


async def test_team_a_cannot_read_team_bs_report_or_matches(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    a = await reported(api, clock, sessions, tmp_path, pool, opened=False)
    b = await reported(api, clock, sessions, tmp_path, pool, name="Comet", flow=a.flow)
    [match_a] = (await a.judge.get(judge_path(a))).json()["matches"]

    b_reads = (await b.flow.client.get("/api/team/prior-work")).json()
    assert b_reads["submission_id"] == str(b.submission.id)
    other = await b.flow.client.get(f"/api/team/prior-work/matches/{match_a['id']}")
    assert other.status_code == 404
    explained = await explain(b, match_a["match_key"], "not ours")
    assert explained.status_code == 404


async def test_a_judge_of_another_event_reads_nothing(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    [match] = (await r.judge.get(judge_path(r))).json()["matches"]
    other_event = await api.event(r.flow.organiser)
    _, stranger = await api.judge(r.flow.organiser, other_event["id"])
    assert (await stranger.get(judge_path(r))).status_code == 404
    assert (await stranger.get(f"/api/judge/prior-work/matches/{match['id']}")).status_code == 404
    assert (await r.flow.organiser.get(judge_path(r))).status_code == 200


async def test_matches_are_read_in_pages(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    async with sessions.begin() as s:
        [report_id, baseline_id] = (
            await s.execute(
                text(
                    "SELECT r.id, m.baseline_id FROM prior_work_reports r "
                    "JOIN prior_work_matches m ON m.report_id = r.id"
                )
            )
        ).one()
        for n in range(1, 250):
            s.add(
                models.PriorWorkMatch(
                    report_id=report_id, ordinal=n, match_key=f"{n:032d}", kind="unchanged",
                    late=False, tokens=30, final_path=f"f{n}.js", final_start_line=1,
                    final_end_line=2, baseline_id=baseline_id, device_label="laptop",
                    old_path="o.js", old_start_line=1, old_end_line=2, final_excerpt_line=1,
                    old_excerpt_line=1,
                )
            )  # fmt: skip
    pages = [
        (await r.judge.get(judge_path(r), params={"offset": offset})).json()
        for offset in (0, 100, 200)
    ]
    assert [len(p["matches"]) for p in pages] == [100, 100, 50]
    assert {p["matches_total"] for p in pages} == {250}
    ordinals = [m["ordinal"] for p in pages for m in p["matches"]]
    assert ordinals == list(range(250))
    assert (await r.judge.get(judge_path(r), params={"limit": 201})).status_code == 422


async def test_a_report_still_being_made_is_pending_and_a_failed_job_is_failed(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    flow = await started(api, clock)
    submission = await submitted(sessions, tmp_path, flow.team_id, {"a.js": b"a"})
    _, judge = await api.judge(flow.organiser, flow.event["id"])
    path = f"/api/judge/submissions/{submission.id}/prior-work"
    job_id = await enqueue(
        sessions, "prior_work", queue.prior_work_key(submission.id), submission_id=submission.id
    )
    assert (await judge.get(path)).json()["status"] == "pending"
    async with sessions.begin() as s:
        await s.execute(text("UPDATE jobs SET status = 'failed' WHERE id = :id"), {"id": job_id})
    failed = (await judge.get(path)).json()
    assert (failed["status"], failed["totals"], failed["matches"]) == ("failed", None, [])


async def test_only_the_match_routes_return_baseline_text(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    [match] = (await r.judge.get(judge_path(r))).json()["matches"]
    team = await r.flow.client.get("/api/team")
    ids = {
        "submission_id": str(r.submission.id), "match_id": match["id"],
        "event_id": r.flow.event["id"], "team_id": team.json()["id"],
    }  # fmt: skip
    clients: dict[str, httpx.AsyncClient] = {
        "organiser": r.flow.organiser, "judge": r.judge, "team": r.flow.client,
    }  # fmt: skip
    match_routes = {
        "/api/team/prior-work/matches/{match_id}",
        "/api/judge/prior-work/matches/{match_id}",
    }
    marker = MARKER.strip()

    seen_in: set[str] = set()
    for method, route in api_routes(api):
        if method != "GET":
            continue
        path = _fill(route, ids)
        roles = allowed_roles(route) or ROLES
        for role in roles:
            answer = await clients[role].get(path)
            if marker in answer.text:
                seen_in.add(route.path)
    assert seen_in == match_routes


def _fill(route: APIRoute, ids: dict[str, str]) -> str:
    parts = []
    for part in route.path.split("/"):
        if part.startswith("{"):
            parts.append(ids.get(part.strip("{}"), str(uuid.uuid4())))
        else:
            parts.append(part)
    return "/".join(parts)


async def test_a_purged_event_returns_no_excerpt_and_says_when_the_code_was_deleted(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    r = await reported(api, clock, sessions, tmp_path, pool)
    before = (await r.judge.get(judge_path(r))).json()
    [match] = before["matches"]
    job = await claimed(sessions, "purge_baselines", payload={"event_id": r.flow.event["id"]})
    await make_purge_handler(sessions, api.app.state.settings)(job)

    for client, path in (
        (r.flow.client, f"/api/team/prior-work/matches/{match['id']}"),
        (r.judge, f"/api/judge/prior-work/matches/{match['id']}"),
    ):
        gone = await client.get(path)
        assert gone.status_code == 410 and gone.json()["detail"]["code"] == "code_deleted"
    for view in (
        (await r.judge.get(judge_path(r))).json(),
        (await r.flow.client.get("/api/team/prior-work")).json(),
    ):
        assert view["code_deleted_at"] is not None
        assert (view["totals"], view["matches"][0]["final_path"]) == (
            before["totals"], match["final_path"],
        )  # fmt: skip
