"""A UI case's files (M11 design, part 7): who may read which, and how they are served."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update

from core.db import models
from tests.api_helpers import Api
from tests.fakes import FakeClock
from tests.helpers import Sessions, enqueue
from tests.team_flow import finished_self_check, started
from tests.unit.test_api_judge import room
from tests.unit.test_api_self_checks import ready, started_check

WEBM = bytes.fromhex("1a45dfa3") + b"a video of the case" * 10
JPEG = b"\xff\xd8\xff" + b"a still"
ZIP = b"PK\x03\x04" + b"a trace"


def case_files(folder: Path) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "video.webm").write_bytes(WEBM)
    (folder / "poster.jpg").write_bytes(JPEG)
    (folder / "trace.zip").write_bytes(ZIP)
    return folder / "video.webm", folder / "trace.zip"


async def point_at(sessions: Sessions, result_id: uuid.UUID, video: Path, trace: Path) -> None:
    async with sessions.begin() as session:
        await session.execute(
            update(models.TestResult)
            .where(models.TestResult.id == result_id)
            .values(video_path=str(video), trace_path=str(trace))
        )


async def only_result(sessions: Sessions, job_id: uuid.UUID) -> uuid.UUID:
    async with sessions() as session:
        found = await session.scalar(
            select(models.TestResult.id).where(models.TestResult.job_id == job_id)
        )
        assert found is not None
        return found


# --- the judge -----------------------------------------------------------------------------------


async def test_a_judge_plays_a_final_runs_video_and_seeks_in_it(
    api: Api, sessions: Sessions, tmp_path: Path
) -> None:
    r = await room(api, sessions)
    result_id = r.found.result_ids["c1"]
    await point_at(sessions, result_id, *case_files(tmp_path / "runs" / "a" / "ui" / "s" / "1"))
    base = f"/api/judge/results/{result_id}/media"

    video = await r.judge.get(f"{base}/video")
    part = await r.judge.get(f"{base}/video", headers={"Range": "bytes=0-3"})
    poster = await r.judge.get(f"{base}/poster")
    trace = await r.judge.get(f"{base}/trace")

    assert (video.status_code, video.content) == (200, WEBM)
    assert video.headers["content-type"] == "video/webm"
    assert video.headers["x-content-type-options"] == "nosniff"
    assert video.headers["cross-origin-resource-policy"] == "same-origin"
    assert (part.status_code, part.content) == (206, WEBM[:4])
    assert part.headers["content-range"] == f"bytes 0-3/{len(WEBM)}"
    assert (poster.status_code, poster.headers["content-type"]) == (200, "image/jpeg")
    assert trace.headers["content-type"] == "application/zip"
    assert trace.headers["content-disposition"].startswith("attachment;")


async def test_a_judge_of_another_event_gets_nothing(
    api: Api, sessions: Sessions, tmp_path: Path
) -> None:
    r = await room(api, sessions)
    result_id = r.found.result_ids["c1"]
    await point_at(sessions, result_id, *case_files(tmp_path / "runs" / "a" / "ui" / "s" / "1"))
    other_event = await api.event(r.organiser)
    _, stranger = await api.judge(r.organiser, other_event["id"])

    response = await stranger.get(f"/api/judge/results/{result_id}/media/video")

    assert response.status_code == 404
    assert (await r.organiser.get(f"/api/results/{result_id}/media/video")).status_code == 200


@pytest.mark.parametrize("where", ["outside", "missing", "none"])
async def test_only_a_stored_file_inside_the_artifact_directory_is_served(
    api: Api,
    sessions: Sessions,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    where: str,
) -> None:
    r = await room(api, sessions)
    result_id = r.found.result_ids["c1"]
    if where == "outside":
        await point_at(sessions, result_id, *case_files(tmp_path_factory.mktemp("elsewhere")))
    if where == "missing":
        await point_at(sessions, result_id, tmp_path / "gone.webm", tmp_path / "gone.zip")

    for kind in ("video", "poster", "trace"):
        response = await r.judge.get(f"/api/judge/results/{result_id}/media/{kind}")
        assert response.status_code == 404, kind
    assert (await r.judge.get(f"/api/judge/results/{result_id}/media/log")).status_code == 422


# --- a team --------------------------------------------------------------------------------------


async def test_a_team_reads_its_own_self_checks_files_only(
    api: Api, clock: FakeClock, sessions: Sessions, tmp_path: Path
) -> None:
    flow, snap, spec = await ready(api, clock, sessions)
    other = await started(api, clock, name="Comet", organiser=flow.organiser, event=flow.event)
    job_id = await started_check(flow, snap.id)
    await finished_self_check(sessions, job_id, results=[(spec, "p1", "fail")])
    final = await enqueue(
        sessions, "final_run", f"final_run:{uuid.uuid4()}", team_id=flow.team_id,
        snapshot_id=snap.id,
    )  # fmt: skip
    await finished_self_check(sessions, final, results=[(spec, "f1", "fail")])
    practice, hidden = await only_result(sessions, job_id), await only_result(sessions, final)
    await point_at(sessions, practice, *case_files(tmp_path / "runs" / "p" / "ui" / "s" / "1"))
    await point_at(sessions, hidden, *case_files(tmp_path / "runs" / "f" / "ui" / "s" / "1"))

    def url(job: uuid.UUID, result: uuid.UUID) -> str:
        return f"/api/team/self-checks/{job}/results/{result}/media/video"

    own = await flow.client.get(url(job_id, practice))
    responses = {
        "B reads A's video": await other.client.get(url(job_id, practice)),
        "A reads its final run's video": await flow.client.get(url(final, hidden)),
        "A reads the final run's result through its self-check": await flow.client.get(
            url(job_id, hidden)
        ),
        "a judge's route for a self-check": await flow.organiser.get(
            f"/api/judge/results/{practice}/media/video"
        ),
    }

    assert (own.status_code, own.content) == (200, WEBM)
    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(responses, 404)
