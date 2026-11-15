"""The judge's routes (M10): the brief renders whatever the AI did, a judge sees only their own
event, and marks and scores are theirs alone until the organiser locks them."""

import csv
import io
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select

from core.db import models
from tests.api_helpers import Api
from tests.factories import T0
from tests.helpers import Sessions, all_jobs
from tests.judging import WRITTEN, Judged, judged


class Room:
    """An event with one team that has a judged submission, and a signed-in judge."""

    organiser: httpx.AsyncClient
    judge: httpx.AsyncClient
    event: dict[str, Any]
    team: dict[str, Any]
    found: Judged

    @property
    def brief_url(self) -> str:
        return f"/api/judge/submissions/{self.found.submission_id}/brief"

    @property
    def base(self) -> str:
        return f"/api/judge/submissions/{self.found.submission_id}"


async def room(api: Api, sessions: Sessions, **kwargs: Any) -> Room:
    r = Room()
    r.organiser = await api.organiser()
    r.event = await api.event(r.organiser)
    r.team = await api.add_team(r.organiser, r.event["id"], "Rocket")
    r.found = await judged(sessions, r.team["id"], **kwargs)
    _, r.judge = await api.judge(r.organiser, r.event["id"])
    return r


async def test_the_team_list_shows_the_judged_submission(api: Api, sessions: Sessions) -> None:
    r = await room(api, sessions, written=WRITTEN)
    await api.add_team(r.organiser, r.event["id"], "Quiet")
    listed = (await r.judge.get("/api/judge/teams")).json()
    quiet, rocket = listed["teams"]
    assert (quiet["team_name"], quiet["submission"]) == ("Quiet", None)
    assert rocket["submission"]["id"] == str(r.found.submission_id)
    assert rocket["submission"]["verdicts"] == {"c1": "fail", "c2": "pass"}
    assert rocket["submission"]["brief"] == "ok"
    assert (rocket["submission"]["scored"], listed["criteria"]) == (0, 2)


async def test_a_submission_after_the_deadline_is_not_the_judged_one(
    api: Api, sessions: Sessions
) -> None:
    r = await room(api, sessions)
    late = await judged(
        sessions, r.team["id"], submitted_at=T0 + timedelta(hours=30), with_claims=False
    )
    listed = (await r.judge.get("/api/judge/teams")).json()
    assert (
        listed["teams"][0]["submission"]["id"]
        == str(r.found.submission_id)
        != str(late.submission_id)
    )


async def test_the_brief_with_the_models_text(api: Api, sessions: Sessions) -> None:
    r = await room(api, sessions, written=WRITTEN)
    brief = (await r.judge.get(r.brief_url)).json()
    assert brief["ai_status"] == "ok"
    assert brief["written"]["summary"] == WRITTEN["summary"]
    assert [c["verdict"] for c in brief["claims"]] == ["fail", "pass"]
    assert brief["claims"][0]["cases"][0]["failed"][0]["actual"] == "99.99"
    assert brief["rubric"] == r.event["rubric"] and brief["can_score"] is True


async def test_the_brief_renders_raw_when_the_ai_part_is_missing(
    api: Api, sessions: Sessions
) -> None:
    r = await room(api, sessions, audit=False)
    brief = (await r.judge.get(r.brief_url)).json()
    assert brief["ai_status"] == "none" and brief["written"]["summary"] is None
    assert brief["audit"]["status"] == "failed"
    first = brief["written"]["demo_questions"][0]
    assert first["ref"] == {"kind": "test_result", "id": str(r.found.result_ids["c1"])}
    assert len(brief["written"]["demo_questions"]) == 2
    assert [c["verdict"] for c in brief["claims"]] == ["fail", "pass"]


async def test_opening_a_brief_is_recorded_for_a_judge_only(api: Api, sessions: Sessions) -> None:
    r = await room(api, sessions)
    assert (await r.judge.get(r.brief_url)).status_code == 200
    seen = await r.organiser.get(r.brief_url)
    assert seen.status_code == 200 and seen.json()["can_score"] is False
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(models.BriefView)) == 1


async def test_a_judge_of_another_event_and_a_team_get_nothing(
    api: Api, sessions: Sessions
) -> None:
    r = await room(api, sessions)
    other = await api.event(r.organiser, name="Other Hack")
    _, stranger = await api.judge(r.organiser, other["id"], "Stranger")
    _, team = await api.team(r.organiser, r.event["id"], "Nosy")
    score = {"scores": {"impact": 3}}
    mark = {"claim_id": "c1", "mark": "verified_live"}
    for client, status in ((stranger, 404), (team, 403)):
        assert (await client.get(r.brief_url)).status_code == status
        assert (await client.put(f"{r.base}/scores", json=score)).status_code == status
        assert (await client.put(f"{r.base}/claim-marks", json=mark)).status_code == status
    assert (await stranger.get("/api/judge/teams")).json()["teams"] == []
    assert (await r.organiser.put(f"{r.base}/scores", json=score)).status_code == 403


async def test_marks_are_set_changed_and_cleared(api: Api, sessions: Sessions) -> None:
    r = await room(api, sessions)

    async def mark(claim: str, value: str) -> httpx.Response:
        return await r.judge.put(f"{r.base}/claim-marks", json={"claim_id": claim, "mark": value})

    assert (await mark("c1", "verified_live")).json()["marks"] == {"c1": "verified_live"}
    assert (await mark("c1", "not_verified")).json()["marks"] == {"c1": "not_verified"}
    assert (await mark("c2", "verified_live")).status_code == 200
    assert (await mark("c1", "none")).json()["marks"] == {"c2": "verified_live"}
    assert (await mark("c9", "verified_live")).status_code == 404
    assert (await mark("c1", "passed")).status_code == 422
    assert (await r.judge.get(r.brief_url)).json()["own"]["marks"] == {"c2": "verified_live"}


async def test_scores_stay_inside_the_rubric_and_total_in_code(
    api: Api, sessions: Sessions
) -> None:
    r = await room(api, sessions)

    async def score(**values: int) -> httpx.Response:
        return await r.judge.put(f"{r.base}/scores", json={"scores": values})

    assert (await score(impact=7)).json() == {"marks": {}, "scores": {"impact": 7}, "total": 7}
    assert (await score(impact=8, craft=10)).json()["total"] == 18
    for bad in ({"impact": 11}, {"impact": -1}, {"style": 3}):
        refused = await score(**bad)
        assert refused.status_code == 422, bad
    assert (await r.judge.get(r.brief_url)).json()["own"]["total"] == 18
    # Another judge's scores are their own.
    _, second = await api.judge(r.organiser, r.event["id"], "Second")
    assert (await second.get(r.brief_url)).json()["own"] == {"marks": {}, "scores": {}, "total": 0}


async def test_locked_scores_cannot_change_and_export_as_csv(api: Api, sessions: Sessions) -> None:
    r = await room(api, sessions)
    await r.judge.put(f"{r.base}/scores", json={"scores": {"impact": 7, "craft": 4}})
    await r.judge.put(f"{r.base}/claim-marks", json={"claim_id": "c1", "mark": "not_verified"})
    await r.judge.put(f"{r.base}/claim-marks", json={"claim_id": "c2", "mark": "verified_live"})
    await api.judge(r.organiser, r.event["id"], "Idle")  # scored nothing: no row

    event = f"/api/events/{r.event['id']}"
    assert (await r.judge.post(f"{event}/lock-scores")).status_code == 403
    assert (await r.organiser.post(f"{event}/lock-scores")).json() == {"scores_locked": True}
    refused = await r.judge.put(f"{r.base}/scores", json={"scores": {"impact": 9}})
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "scores_locked")

    exported = await r.organiser.get(f"{event}/scores.csv")
    assert exported.headers["content-type"].startswith("text/csv")
    header, *body = list(csv.reader(io.StringIO(exported.text)))
    assert header == [
        "team", "submission_id", "commit_sha", "judge", "impact", "craft", "judge_total",
        "claims_verified_live", "claims_not_verified",
    ]  # fmt: skip
    [row] = body
    assert row[0] == "Rocket" and row[1] == str(r.found.submission_id)
    assert row[3:] == ["Judge Judy", "7", "4", "11", "1", "1"]
    assert (await r.judge.get(f"{event}/scores.csv")).status_code == 403


async def test_the_dashboard_and_a_rerun(api: Api, sessions: Sessions) -> None:
    r = await room(api, sessions, written=WRITTEN)
    await r.judge.get(r.brief_url)
    board = (await r.organiser.get(f"/api/events/{r.event['id']}/dashboard")).json()
    [team] = board["teams"]
    assert team["submission"]["verdicts"] == {"c1": "fail", "c2": "pass"}
    assert (team["submission"]["final_run"], team["submission"]["audit"]) == ("done", "done")
    assert team["submission"]["brief"] == "ok"
    [judge] = board["judges"]
    assert (judge["briefs_opened"], judge["submissions_scored"]) == (1, 0)
    assert {job["kind"] for job in board["jobs"]} == {"final_run", "audit", "brief"}
    assert [link["role"] for link in board["links"]] == ["judge"]
    listed = (await r.organiser.get("/api/events")).json()["events"]
    assert [event["id"] for event in listed] == [r.event["id"]]

    again = await r.organiser.post(f"/api/jobs/{r.found.audit_id}/rerun")
    assert again.status_code == 201 and again.json()["kind"] == "audit"
    twice = await r.organiser.post(f"/api/jobs/{r.found.audit_id}/rerun")
    assert twice.json()["id"] != again.json()["id"]
    jobs = [job for job in await all_jobs(sessions) if job.kind == "audit"]
    assert sorted(job.status for job in jobs) == ["done", "queued", "queued"]
    assert all(job.submission_id == r.found.submission_id for job in jobs)
    assert (await r.organiser.post(f"/api/jobs/{again.json()['id']}/rerun")).status_code == 409
    assert (await r.judge.post(f"/api/jobs/{r.found.audit_id}/rerun")).status_code == 403
    # The audit is being redone: its old row no longer counts, and the brief says so.
    brief = (await r.judge.get(r.brief_url)).json()
    assert brief["audit"]["status"] == "pending" and brief["claims"][0]["audit"] is None
