"""A submission's receipt: its fields and a hash anyone can recompute from them (M7, part 6)."""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from core.hashing import hash_json
from core.receipts import receipt_fields, receipt_hash
from tests.factories import make_snapshot, make_submission

TEAM = uuid.UUID("11111111-1111-1111-1111-111111111111")
SUBMISSION = uuid.UUID("22222222-2222-2222-2222-222222222222")
SNAPSHOT = uuid.UUID("33333333-3333-3333-3333-333333333333")
AT = datetime(2026, 11, 14, 9, 30, 15, 120000, tzinfo=UTC)


def rows() -> tuple[object, object]:
    snapshot = make_snapshot(TEAM, id=SNAPSHOT, commit_sha="a" * 40, archive_sha256="b" * 64)
    submission = make_submission(
        TEAM,
        SNAPSHOT,
        id=SUBMISSION,
        claims_hash="c" * 64,
        test_spec_hash="d" * 64,
        rubric_version=2,
        submitted_at=AT,
    )
    return submission, snapshot


FIELDS = {
    "submission_id": "22222222-2222-2222-2222-222222222222",
    "team_id": "11111111-1111-1111-1111-111111111111",
    "commit_sha": "a" * 40,
    "archive_sha256": "b" * 64,
    "claims_hash": "c" * 64,
    "test_spec_hash": "d" * 64,
    "rubric_version": 2,
    "submitted_at": "2026-11-14T09:30:15.120000+00:00",
}


def test_the_fields_of_a_receipt() -> None:
    submission, snapshot = rows()

    assert receipt_fields(submission, snapshot) == FIELDS  # type: ignore[arg-type]


def test_the_time_is_written_in_utc_whatever_zone_it_was_read_in() -> None:
    submission, snapshot = rows()
    submission.submitted_at = AT.astimezone(timezone(timedelta(hours=5, minutes=30)))  # type: ignore[attr-defined]

    fields = receipt_fields(submission, snapshot)  # type: ignore[arg-type]

    assert fields["submitted_at"] == "2026-11-14T09:30:15.120000+00:00"


def test_the_hash_is_the_canonical_json_of_the_fields() -> None:
    assert receipt_hash(FIELDS) == hash_json(FIELDS)


@pytest.mark.parametrize("field", list(FIELDS))
def test_changing_any_field_changes_the_hash(field: str) -> None:
    changed = {**FIELDS, field: 3 if field == "rubric_version" else f"{FIELDS[field]}x"}

    assert receipt_hash(changed) != receipt_hash(FIELDS)
