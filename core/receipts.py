"""A submission's receipt (section 5.2, M7 design part 6).

The receipt pins what was submitted: the commit and its archive, what the team said it would
build (`claims_hash`) and the tests it approved (`test_spec_hash`), the rubric, and when. Its
hash is the SHA-256 of the canonical JSON of those fields, so anyone holding the receipt can
recompute it, and the stored row proves it was not changed afterwards.
"""

from collections.abc import Mapping
from datetime import UTC
from typing import Any

from core.db import models
from core.hashing import hash_json


def receipt_fields(submission: models.Submission, snapshot: models.Snapshot) -> dict[str, Any]:
    """The fields the receipt hash covers, as they are shown: IDs as strings, the time in UTC."""
    return {
        "submission_id": str(submission.id),
        "team_id": str(submission.team_id),
        "commit_sha": snapshot.commit_sha,
        "archive_sha256": snapshot.archive_sha256,
        "claims_hash": submission.claims_hash,
        "test_spec_hash": submission.test_spec_hash,
        "rubric_version": submission.rubric_version,
        "submitted_at": submission.submitted_at.astimezone(UTC).isoformat(timespec="microseconds"),
    }


def receipt_hash(fields: Mapping[str, Any]) -> str:
    return hash_json(dict(fields))


def baseline_receipt_fields(
    baseline: models.Baseline, device: models.BaselineDevice, team: models.Team
) -> dict[str, Any]:
    """The fields a kickoff scan's receipt hash covers (M9): which device, when it was sealed,
    whether it was late or incomplete, and the hash of what was sent."""
    assert baseline.sealed_at is not None, "only a sealed baseline has a receipt"
    return {
        "baseline_id": str(baseline.id),
        "team_id": str(team.id),
        "team_name": team.name,
        "device_label": device.label,
        "sealed_at": baseline.sealed_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "late": bool(baseline.late),
        "complete": bool(baseline.complete),
        "files": baseline.files or 0,
        "bytes": baseline.bytes_received,
        "manifest_sha256": baseline.manifest_sha256 or "",
        "scanner_version": baseline.scanner_version or "",
    }
