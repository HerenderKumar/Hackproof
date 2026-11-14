"""Builders for database rows used across tests. Every argument has a sensible default."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from core.db import models

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)

RUBRIC = [
    {"id": "impact", "name": "Impact", "description": "Does it matter?", "max": 10},
    {"id": "craft", "name": "Craft", "description": "Is it well built?", "max": 10},
]


def make_event(**overrides: Any) -> models.Event:
    values: dict[str, Any] = {
        "name": f"Hack {uuid.uuid4().hex[:6]}",
        "kickoff_at": T0,
        "deadline_at": T0 + timedelta(hours=24),
        "rubric": RUBRIC,
    }
    return models.Event(**{**values, **overrides})


def make_team(event_id: uuid.UUID, **overrides: Any) -> models.Team:
    values: dict[str, Any] = {"event_id": event_id, "name": f"Team {uuid.uuid4().hex[:6]}"}
    return models.Team(**{**values, **overrides})


def make_judge(event_id: uuid.UUID, **overrides: Any) -> models.Judge:
    values: dict[str, Any] = {"event_id": event_id, "name": f"Judge {uuid.uuid4().hex[:6]}"}
    return models.Judge(**{**values, **overrides})


def make_snapshot(team_id: uuid.UUID, **overrides: Any) -> models.Snapshot:
    sha = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    values: dict[str, Any] = {
        "team_id": team_id,
        "commit_sha": sha,
        "archive_sha256": uuid.uuid4().hex * 2,
        "archive_path": f"/artifacts/archives/{sha}.tar.gz",
        "commit_history": [],
    }
    return models.Snapshot(**{**values, **overrides})


def make_submission(
    team_id: uuid.UUID, snapshot_id: uuid.UUID, **overrides: Any
) -> models.Submission:
    values: dict[str, Any] = {
        "team_id": team_id,
        "snapshot_id": snapshot_id,
        "mode": "sandbox",
        "claims_version": 1,
        "claims_hash": "c" * 64,
        "test_spec_hash": "t" * 64,
        "rubric_version": 1,
        "runner_version": "1",
        "railpack_version": "0.43.0",
        "reuse_declaration": {
            "starter_code": False,
            "earlier_work": False,
            "ai_assistance": False,
            "details": "",
        },
        "idempotency_key": uuid.uuid4().hex,
        "receipt_hash": "r" * 64,
    }
    return models.Submission(**{**values, **overrides})
