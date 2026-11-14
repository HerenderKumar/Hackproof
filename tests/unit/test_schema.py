"""The migrated schema matches section 10, and submissions are immutable (invariant 3)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db import models
from tests.factories import make_event, make_snapshot, make_submission, make_team

EXPECTED_TABLES = {
    "events", "teams", "judges", "access_links", "repo_observations", "claims", "snapshots",
    "test_specs", "submissions", "jobs", "run_results", "test_results", "audits", "briefs",
    "claim_marks", "scores", "brief_views", "llm_calls",
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
