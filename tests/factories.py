"""Builders for database rows used across tests. Every argument has a sensible default."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from core.db import models
from core.hashing import hash_json
from core.specs import spec_hash
from tests import spec_examples

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


CLAIM_ITEMS: list[dict[str, Any]] = [
    {
        "id": "c1",
        "text": "Splits a bill so the shares add up to the exact total.",
        "target": "POST /split",
        "example_input": '{"total": 100, "people": 3}',
        "expected_behaviour": "Shares sum to exactly 100.00.",
    },
    {
        "id": "c2",
        "text": "Counts the words in a text.",
        "target": "POST /wordcount",
        "example_input": '{"text": "a b"}',
        "expected_behaviour": "Returns words 2.",
    },
]


def make_claims(
    team_id: uuid.UUID, items: list[dict[str, Any]] | None = None, version: int = 1
) -> models.Claims:
    items = CLAIM_ITEMS if items is None else items
    return models.Claims(
        team_id=team_id, version=version, items=items, claims_hash=hash_json(items)
    )


def make_test_spec(
    snapshot_id: uuid.UUID,
    claim_id: str = "c1",
    document: dict[str, Any] | None = None,
    status: str = "approved",
    version: int = 1,
    claims_version: int = 1,
    **overrides: Any,
) -> models.TestSpec:
    """A test_specs row; approved rows carry `approved_at` and the matching approved hash."""
    doc = {**(document or spec_examples.F01), "claim_id": claim_id}
    digest = spec_hash(doc)
    values: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "claims_version": claims_version,
        "version": version,
        "kind": doc["kind"],
        "claim_id": claim_id,
        "claim_type": doc["claim_type"],
        "steps": doc["steps"],
        "practice_cases": doc["practice_cases"],
        "final_cases": doc["final_cases"],
        "spec_hash": digest,
        "status": status,
        "approved_at": T0 if status == "approved" else None,
        "approved_spec_hash": digest if status == "approved" else None,
    }
    return models.TestSpec(**{**values, **overrides})
