"""`make seed-demo`: create the demo event of section 22 and pre-run every job (M12).

The seed is one process playing the organiser, five teams and the worker. It calls the API
in-process (the routes the pages call) and runs the worker's own job handlers, both on one clock
that it holds at the kickoff, five minutes later and an hour later, then lets run. Model replies
come from recordings (`fixtures/llm`, the demo's in `fixtures/llm/demo/`); `--mode record` calls
the gateway for what has none and saves it in `fixtures/llm/demo/`.

The worker service must not run meanwhile, or it takes the seed's jobs and dates them with the
real clock: the make target stops it and starts it again afterwards.
"""

import argparse
import asyncio
import secrets
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import SecretStr
from sqlalchemy import func, select, text

from api.main import create_app
from core import queue
from core.brief_data import current_brief, load_facts
from core.db import models
from core.db.engine import Sessions, make_engine, make_sessions
from core.llm import (
    LLMClient,
    ReplayMissing,
    Transport,
    live_transport,
    recording_transport,
    replay_transport,
)
from core.providers import make_provider
from core.results import current_test_results, final_run_job
from core.settings import Settings, get_settings
from scripts.demo_event import (
    EVENT_NAME,
    JUDGE_NAME,
    KICKOFF,
    LIVE_TEAM,
    POLICY,
    RUBRIC,
    TEAMS,
    DemoTeam,
    TeamFound,
    build_repositories,
    differences,
    instants,
    move_heads,
)
from scripts.fixtures import Fixture, load_fixtures
from worker import lanes, scheduler
from worker.main import build_handlers
from worker.runtime import DockerRuntime

Mode = Literal["replay", "record"]
LANES: tuple[queue.Lane, ...] = ("watch", "heavy", "index")  # index: prior_work (M9 scanner)


class SeedError(Exception):
    """The seed could not go on; the message says what it was doing."""


class SeedClock:
    """The real time, until frozen at a moment; `live()` lets it run again."""

    def __init__(self) -> None:
        self._at: datetime | None = None

    def now(self) -> datetime:
        return self._at if self._at is not None else datetime.now(UTC)

    def freeze(self, at: datetime) -> None:
        self._at = at

    def live(self) -> None:
        self._at = None


_WAITING = text(
    """
    SELECT kind, count(*) AS n FROM jobs
    WHERE team_id = ANY(CAST(:teams AS uuid[])) AND status IN ('queued', 'running')
    GROUP BY kind ORDER BY kind
    """
)


async def drain(
    sessions: Sessions,
    handlers: Mapping[str, lanes.Handler],
    teams: Sequence[uuid.UUID],
    *,
    timeout_s: float = 1800.0,
    idle_s: float = 0.5,
) -> None:
    """Run these teams' jobs, and what they enqueue, until none is queued or running."""
    team_ids = list(teams)
    give_up = time.monotonic() + timeout_s
    while True:
        job = None
        for lane in LANES:
            async with sessions.begin() as session:
                job = await queue.claim(session, lane, teams=team_ids)
            if job is not None:
                break
        if job is not None:
            await lanes.run_job(job, sessions, handlers)
        else:
            async with sessions() as session:
                waiting = (await session.execute(_WAITING, {"teams": team_ids})).all()
            if not waiting:
                return
            if time.monotonic() > give_up:
                kinds = ", ".join(f"{row.kind} ({row.n})" for row in waiting)
                raise SeedError(f"jobs still waiting after {timeout_s:g} s: {kinds}")
            await asyncio.sleep(idle_s)


def filling_transport(recorded: Transport, fresh: Transport) -> Transport:
    """Answer from the recordings; only a request with none goes to `fresh`."""

    async def send(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await recorded(body)
        except ReplayMissing:
            return await fresh(body)

    return send


def seed_transport(settings: Settings, mode: Mode) -> Transport:
    recorded = replay_transport(settings.llm_replay_dir)
    if mode == "replay":
        return recorded
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else ""
    if not key:
        raise SeedError("--mode record calls the model: set LLM_API_KEY in .env")
    live = live_transport(settings.llm_base_url, key, settings.llm_timeout_s)
    return filling_transport(recorded, recording_transport(live, settings.llm_replay_dir / "demo"))


# --- the walk ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seeded:
    event_id: uuid.UUID
    found: dict[str, TeamFound]
    links: dict[str, str]  # "Organiser", "Judge" and each team's name -> URL


def step(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


async def call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    expected: int | tuple[int, ...],
    **kwargs: Any,
) -> httpx.Response:
    """One API call as a page makes it; any other status stops the seed."""
    response = await client.request(method, path, **kwargs)
    allowed = (expected,) if isinstance(expected, int) else expected
    if response.status_code not in allowed:
        raise SeedError(f"{method} {path} answered {response.status_code}: {response.text[:500]}")
    return response


def token_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


async def seed(settings: Settings, *, mode: Mode, deadline_hours: int = 48) -> Seeded:
    record = mode == "record"
    fixtures = {f.id: f for f in load_fixtures()}

    step(f"building the five repositories in {settings.local_remotes_dir}")
    pushes = build_repositories(settings.local_remotes_dir)
    (settings.artifact_dir / "work").mkdir(parents=True, exist_ok=True)

    clock = SeedClock()
    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)
    provider = make_provider(settings, clock)
    api_settings = settings.model_copy(
        update={
            "organiser_password": _secret(),
            "session_secret": _secret(),
            "app_env": "local",
            "log_level": "WARNING",  # the steps print; the services' info logs would bury them
        }
    )
    app = create_app(api_settings, clock=clock, repo_provider=provider)
    llm = LLMClient(seed_transport(settings, mode), sessions=sessions)
    # As the worker: the index lane's prior-work comparison runs in processes of its own.
    pool = ProcessPoolExecutor(settings.index_concurrency, mp_context=get_context("spawn"))
    handlers = build_handlers(settings, sessions, clock, DockerRuntime(), llm=llm, pool=pool)
    clients: list[httpx.AsyncClient] = []

    def browser() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://seed", timeout=120
        )
        clients.append(client)
        return client

    async def organiser() -> httpx.AsyncClient:
        client = browser()
        password = api_settings.organiser_password
        assert password is not None
        await call(
            client,
            "POST",
            "/api/auth/organiser",
            204,
            json={"password": password.get_secret_value()},
        )
        return client

    async def team_browser(link: dict[str, Any]) -> httpx.AsyncClient:
        client = browser()
        await call(client, "GET", "/api/auth/redeem", 303, params={"token": token_of(link["url"])})
        return client

    try:
        # 2. Before kickoff: the event, its teams and judge; each team registers its repository.
        clock.freeze(KICKOFF - timedelta(hours=1))
        step(f"clock at {clock.now().isoformat()}: the organiser creates {EVENT_NAME!r}")
        org = await organiser()
        deadline = datetime.now(UTC) + timedelta(hours=deadline_hours)
        event = (
            await call(
                org, "POST", "/api/events", 201,
                json={
                    "name": EVENT_NAME,
                    "kickoff_at": KICKOFF.isoformat(),
                    "deadline_at": deadline.isoformat(),
                    "rubric": RUBRIC,
                    "policy_text": POLICY,
                },
            )
        ).json()  # fmt: skip
        event_id = event["id"]
        names = "".join(f"{team.name},,\n" for team in TEAMS)
        imported = await call(
            org, "POST", f"/api/events/{event_id}/teams/import", 200,
            files={"file": ("teams.csv", f"team_name,contact_email,repo_url\n{names}", "text/csv")},
        )  # fmt: skip
        team_ids = {row["name"]: row["id"] for row in imported.json()["created"]}
        if set(team_ids) != {team.name for team in TEAMS}:
            raise SeedError(f"the import made {sorted(team_ids)}")
        judge = (
            await call(
                org, "POST", f"/api/events/{event_id}/judges", 201, json={"name": JUDGE_NAME}
            )
        ).json()

        first_links: dict[str, dict[str, Any]] = {}
        for team in TEAMS:
            step(f"{team.name} opens its link and registers local://{team.repo}")
            link = (
                await call(
                    org, "POST", f"/api/events/{event_id}/links", 201,
                    json={"role": "team", "subject_id": team_ids[team.name]},
                )
            ).json()  # fmt: skip
            first_links[team.name] = link
            client = await team_browser(link)
            await call(client, "POST", "/api/team/rules-ack", 200)
            fixture = fixtures[team.fixture]
            await call(
                client, "POST", "/api/team/repo", 200,
                json={"repo_url": f"local://{team.repo}", "health_path": fixture.health_path},
            )  # fmt: skip
        ids = [uuid.UUID(team_ids[team.name]) for team in TEAMS]

        # 3. Kickoff, five minutes in, an hour in: what was pushed by then, and the watch's jobs.
        for at in instants(pushes):
            clock.freeze(at)
            move_heads(settings.local_remotes_dir, pushes, at)
            async with sessions.begin() as session:
                await scheduler.tick(session, at)
            await drain(sessions, handlers, ids)
            step(f"clock at {at.isoformat()}: the repo watch ran")

        # 4. From here the clock is real. New links, valid for 30 days.
        clock.live()
        step("clock is real again: new 30-day links, the first ones revoked")
        org = await organiser()
        links: dict[str, str] = {
            "Organiser": f"{settings.public_base_url.rstrip('/')}/org?event={event_id}"
        }
        teams: dict[str, httpx.AsyncClient] = {}
        for team in TEAMS:
            link = (
                await call(
                    org, "POST", f"/api/events/{event_id}/links", 201,
                    json={"role": "team", "subject_id": team_ids[team.name], "expires_in_days": 30},
                )
            ).json()  # fmt: skip
            teams[team.name] = await team_browser(link)
            links[team.name] = link["url"]
            await call(org, "DELETE", f"/api/links/{first_links[team.name]['id']}", 204)
        judge_link = (
            await call(
                org, "POST", f"/api/events/{event_id}/links", 201,
                json={"role": "judge", "subject_id": judge["id"], "expires_in_days": 30},
            )
        ).json()  # fmt: skip
        links["Judge"] = judge_link["url"]

        # 5. Claims; their tests are drafted (replayed) and approved.
        for team in TEAMS:
            step(f"{team.name} saves its claims")
            await call(
                teams[team.name],
                "PUT",
                "/api/team/claims",
                200,
                json=_claims(fixtures[team.fixture]),
            )
        await drain(sessions, handlers, ids)
        for team in TEAMS:
            if team.name == LIVE_TEAM and not record:
                continue
            approved = await _approve_tests(teams[team.name])
            step(f"{team.name} approves {approved} drafted test(s)")

        # 6. A snapshot of the head; 7. the submissions, then their runs, audits and briefs.
        snapshots: dict[str, str] = {}
        for team in TEAMS:
            await call(teams[team.name], "POST", "/api/team/snapshots", (200, 202), json={})
        await drain(sessions, handlers, ids)
        for team in TEAMS:
            made = await call(teams[team.name], "POST", "/api/team/snapshots", (200, 202), json={})
            snapshot_id = made.json().get("snapshot_id")
            if not snapshot_id:
                raise SeedError(f"{team.name}'s snapshot is not ready: {made.text[:300]}")
            snapshots[team.name] = snapshot_id
            step(f"{team.name} snapshot {made.json()['commit_sha'][:10]} ready")
        for team in TEAMS:
            if not (team.submits or record):
                continue
            await call(
                teams[team.name], "POST", "/api/team/submissions", (200, 201),
                json={
                    "snapshot_id": snapshots[team.name],
                    "mode": "sandbox",
                    "reuse_declaration": {
                        "starter_code": False, "earlier_work": False, "ai_assistance": False,
                        "notes": "",
                    },
                    "idempotency_key": uuid.uuid4().hex,
                },
            )  # fmt: skip
            step(f"{team.name} submits")
        step("final runs, audits and briefs (a few minutes)")
        await drain(sessions, handlers, ids)

        # 8. The claims lock: nobody starts a model call from the claims page during the demo.
        lock = datetime.now(UTC).isoformat()
        await call(org, "PATCH", f"/api/events/{event_id}", 200, json={"claims_lock_at": lock})

        found = {
            team.name: await _found(sessions, teams[team.name], uuid.UUID(team_ids[team.name]))
            for team in TEAMS
        }
        return Seeded(uuid.UUID(event_id), found, links)
    finally:
        for client in clients:
            await client.aclose()
        pool.shutdown(cancel_futures=True)
        await engine.dispose()


def _secret() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(32))


def _claims(fixture: Fixture) -> dict[str, Any]:
    return {
        "problem_statement": fixture.start["problem_statement"],
        "explanation": fixture.start["explanation"],
        "items": [{k: v for k, v in claim.items() if k != "id"} for claim in fixture.claims],
    }


async def _approve_tests(client: httpx.AsyncClient) -> int:
    view = (await call(client, "GET", "/api/team/tests", 200)).json()
    if view["drafting"] != "done":
        raise SeedError(f"drafting ended {view['drafting']}")
    approved = 0
    for claim in view["claims"]:
        if claim["state"] != "draft":
            continue
        test = claim["test"]
        await call(
            client, "POST", f"/api/team/tests/{test['id']}/approve", 200,
            json={"spec_hash": test["spec_hash"]},
        )  # fmt: skip
        approved += 1
    return approved


async def _found(sessions: Sessions, client: httpx.AsyncClient, team_id: uuid.UUID) -> TeamFound:
    view = (await call(client, "GET", "/api/team/tests", 200)).json()
    unapproved = sum(1 for claim in view["claims"] if claim["state"] == "draft")
    async with sessions() as session:
        submission = await session.scalar(
            select(models.Submission)
            .where(models.Submission.team_id == team_id)
            .order_by(models.Submission.submitted_at.desc())
            .limit(1)
        )
        facts = brief = None
        videos = 0
        if submission is not None:
            facts = await load_facts(session, submission.id)
            row = await current_brief(session, submission.id)
            if row is not None:
                brief = {"ai_status": row.ai_status, "content": row.content}
            run_job = await final_run_job(session, submission.id)
            if run_job is not None:
                results = await current_test_results(session, run_job.id)
                paths = [Path(r.video_path) for r in results if r.video_path]
                videos = sum(await asyncio.gather(*(asyncio.to_thread(p.is_file) for p in paths)))
        jobs = (
            await session.execute(
                select(
                    models.Job.kind,
                    models.Job.status,
                    models.Job.error_class,
                    models.Job.error_detail,
                )
                .where(models.Job.team_id == team_id)
                .order_by(models.Job.created_at)
            )
        ).all()
        observed = await session.scalars(
            select(models.RepoObservation)
            .where(models.RepoObservation.team_id == team_id)
            .order_by(models.RepoObservation.observed_at, models.RepoObservation.id)
        )
        observations = [
            (
                o.kind,
                o.observed_at,
                o.lines_added if o.lines_added is not None else o.lines_total or 0,
            )
            for o in observed
        ]
        snapshots = await session.scalar(
            select(func.count())
            .select_from(models.Snapshot)
            .where(models.Snapshot.team_id == team_id)
        )
    return TeamFound(
        facts=facts,
        brief=brief,
        jobs=[(kind, str(status), error_class) for kind, status, error_class, _ in jobs],
        job_details={kind: detail[:300] for kind, _, _, detail in jobs if detail},
        observations=observations,
        unapproved_tests=unapproved,
        snapshots=int(snapshots or 0),
        videos=videos,
    )


# --- the report ----------------------------------------------------------------------------------


def _row(team: DemoTeam, seen: TeamFound) -> list[str]:
    facts = seen.facts or {}
    claims = facts.get("claims", [])
    verdicts = " ".join(f"{c['id']}={c['verdict']}" for c in claims) or "not submitted"
    audit = (
        " ".join(f"{c['id']}={(c.get('audit') or {}).get('implementation') or '-'}" for c in claims)
        or "-"
    )
    found_signals = ", ".join(s["kind"] for s in facts.get("signals", [])) or "-"
    brief = "-"
    if seen.brief is not None:
        source = (seen.brief["content"] or {}).get("questions_from")
        brief = f"{seen.brief['ai_status']}, questions from {source}"
    return [team.name, verdicts, audit, found_signals, brief]


def report(seeded: Seeded, problems: list[str]) -> str:
    header = ["Team", "Verdicts", "Audit", "Signals", "Brief"]
    rows = [header] + [_row(team, seeded.found[team.name]) for team in TEAMS]
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    lines.append("")
    if problems:
        lines.append(f"{len(problems)} difference(s) from the design:")
        lines += [f"  - {p}" for p in problems]
    else:
        lines.append("The demo is as designed.")
    lines.append("")
    lines.append(f"Organiser: {seeded.links['Organiser']}  (password: ORGANISER_PASSWORD in .env)")
    lines.append(f"Judge ({JUDGE_NAME}): {seeded.links['Judge']}")
    for team in TEAMS:
        lines.append(f"{team.name}: {seeded.links[team.name]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["replay", "record"], default="replay")
    parser.add_argument("--deadline-hours", type=int, default=48)
    args = parser.parse_args(argv)
    settings = get_settings()
    mode: Mode = args.mode
    try:
        seeded = asyncio.run(seed(settings, mode=mode, deadline_hours=args.deadline_hours))
    except SeedError as exc:
        print(f"seed-demo failed: {exc}", file=sys.stderr, flush=True)
        return 1
    problems = differences(seeded.found, live_team_submitted=mode == "record")
    text_out = report(seeded, problems)
    print(text_out, flush=True)
    links = Path(".artifacts/demo/links.txt")
    links.parent.mkdir(parents=True, exist_ok=True)
    links.write_text(
        "\n".join(f"{name}: {url}" for name, url in seeded.links.items()) + "\n", encoding="utf-8"
    )
    print(f"\nLinks written to {links}", flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
