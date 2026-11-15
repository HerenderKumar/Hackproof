"""The migrated schema matches section 10, and submissions are immutable (invariant 3)."""

import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db import models
from tests.factories import (
    CLAIM_ITEMS,
    make_baseline,
    make_device,
    make_event,
    make_snapshot,
    make_submission,
    make_team,
    make_test_spec,
)

EXPECTED_TABLES = {
    "events", "teams", "judges", "access_links", "repo_observations", "claims", "snapshots",
    "test_specs", "submissions", "jobs", "run_results", "test_results", "audits", "briefs",
    "claim_marks", "scores", "brief_views", "llm_calls",
    # M9: the kickoff baseline (D-052)
    "baseline_devices", "baselines", "starters", "prior_work_reports", "prior_work_matches",
    "match_explanations",
}  # fmt: skip


async def test_all_tables_from_the_brief_exist(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as s:
        rows = await s.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    assert set(rows.scalars()) >= EXPECTED_TABLES


async def test_models_declare_exactly_the_columns_in_the_database(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as s:
        rows = await s.execute(
            text(
                "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
            )
        )
    in_db = {(t, c): nullable == "YES" for t, c, nullable in rows}
    in_models = {
        (table.name, column.name): bool(column.nullable)
        for table in models.Base.metadata.tables.values()
        for column in table.columns
    }
    assert in_models == in_db


async def _submission(sessions: async_sessionmaker[AsyncSession]) -> models.Submission:
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
        await s.flush()
        team = make_team(event.id)
        s.add(team)
        await s.flush()
        snapshot = make_snapshot(team.id)
        s.add(snapshot)
        await s.flush()
        submission = make_submission(team.id, snapshot.id)
        s.add(submission)
    return submission


async def test_update_on_submissions_raises(sessions: async_sessionmaker[AsyncSession]) -> None:
    submission = await _submission(sessions)
    with pytest.raises(DBAPIError, match="submissions are immutable"):
        async with sessions.begin() as s:
            await s.execute(
                text("UPDATE submissions SET claims_hash = 'tampered' WHERE id = :id"),
                {"id": submission.id},
            )
    async with sessions() as s:
        stored = await s.get(models.Submission, submission.id)
    assert stored is not None and stored.claims_hash == submission.claims_hash


async def test_delete_on_submissions_raises(sessions: async_sessionmaker[AsyncSession]) -> None:
    submission = await _submission(sessions)
    with pytest.raises(DBAPIError, match="submissions are immutable"):
        async with sessions.begin() as s:
            await s.execute(text("DELETE FROM submissions WHERE id = :id"), {"id": submission.id})
    async with sessions() as s:
        assert await s.get(models.Submission, submission.id) is not None


async def test_event_deadline_must_follow_kickoff(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    event = make_event()
    event.deadline_at = event.kickoff_at
    with pytest.raises(DBAPIError):
        async with sessions.begin() as s:
            s.add(event)


async def test_claims_hold_between_one_and_three_items(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
        await s.flush()
        team = make_team(event.id)
        s.add(team)
    item = {
        "id": "c1",
        "text": "t",
        "target": "GET /",
        "example_input": "",
        "expected_behaviour": "",
    }
    for items in ([], [item] * 4):
        with pytest.raises(DBAPIError):
            async with sessions.begin() as s:
                s.add(models.Claims(team_id=team.id, version=1, items=items, claims_hash="h"))


async def _team(sessions: async_sessionmaker[AsyncSession]) -> models.Team:
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
        await s.flush()
        team = make_team(event.id)
        s.add(team)
    return team


async def test_claims_carry_the_start_text(sessions: async_sessionmaker[AsyncSession]) -> None:
    team = await _team(sessions)
    async with sessions.begin() as s:
        await s.execute(
            text(
                "INSERT INTO claims (team_id, version, items, claims_hash) "
                "VALUES (:team, 1, CAST(:items AS jsonb), 'h')"
            ),
            {"team": team.id, "items": json.dumps(CLAIM_ITEMS[:1])},
        )
    async with sessions() as s:
        claims = await s.scalar(select(models.Claims).where(models.Claims.team_id == team.id))
    assert claims is not None
    assert (claims.problem_statement, claims.explanation) == ("", "")


async def test_a_spec_needs_a_team_but_not_a_snapshot(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    team = await _team(sessions)
    async with sessions.begin() as s:
        s.add(make_test_spec(team.id, "c1"))
    async with sessions() as s:
        stored = await s.scalar(select(models.TestSpec))
    assert stored is not None and (stored.team_id, stored.snapshot_id) == (team.id, None)

    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            s.add(make_test_spec(None, "c2"))


# --- M9: the kickoff baseline --------------------------------------------------------------


async def test_an_event_starts_with_the_twelve_extensions(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
    async with sessions() as s:
        stored = await s.get(models.Event, event.id)
    assert stored is not None
    assert stored.scan_extensions == [
        ".js", ".jsx", ".ts", ".tsx", ".py", ".c", ".h", ".cpp", ".cc", ".hpp", ".java", ".go",
    ]  # fmt: skip
    assert (stored.common_code_min_teams, stored.baseline_retention_days) == (3, 30)


async def test_a_device_has_one_live_baseline_and_one_upload(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    team = await _team(sessions)
    async with sessions.begin() as s:
        device = make_device(team.id)
        s.add(device)
        await s.flush()
        s.add(make_baseline(device.id, team.id, "sealed"))
        s.add(make_baseline(device.id, team.id, "uploading"))
        s.add(make_baseline(device.id, team.id, "superseded"))
        s.add(make_baseline(device.id, team.id, "superseded"))
    for status in ("indexed", "uploading"):
        with pytest.raises(IntegrityError):
            async with sessions.begin() as s:
                s.add(make_baseline(device.id, team.id, status))


async def test_the_job_table_takes_the_new_kinds_and_the_index_lane(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as s:
        for n, kind in enumerate(("index_baseline", "prior_work", "purge_baselines")):
            await s.execute(
                text(
                    "INSERT INTO jobs (kind, lane, priority, idempotency_key) "
                    "VALUES (:kind, 'index', 1, :key)"
                ),
                {"kind": kind, "key": f"k{n}"},
            )
    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            await s.execute(
                text(
                    "INSERT INTO jobs (kind, lane, priority, idempotency_key) "
                    "VALUES ('prior_work', 'nowhere', 1, 'k9')"
                )
            )


async def test_an_explanation_is_at_most_2000_characters(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    submission = await _submission(sessions)
    async with sessions.begin() as s:
        s.add(models.MatchExplanation(submission_id=submission.id, text="x" * 2000))
    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            s.add(models.MatchExplanation(submission_id=submission.id, text="x" * 2001))
