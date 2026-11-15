"""The kickoff baseline (M9, D-052): devices, their scans, starter code and prior-work reports.

A device belongs to a team and is reached only with its own code, whose hash is kept. A device
has at most one live baseline (sealed, indexed or failed) and at most one upload under way; a
new scan supersedes the live one. Baseline code itself lives on disk, encrypted; the tables
hold its bookkeeping, the reports, and encrypted excerpts of matches.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_KINDS_BEFORE = (
    "'kickoff_snapshot','poll_repo','prepare_snapshot','draft_specs','self_check_run',"
    "'final_run','audit','brief'"
)
_KINDS_AFTER = _KINDS_BEFORE + ",'index_baseline','prior_work','purge_baselines'"

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE events ADD COLUMN scan_extensions JSONB NOT NULL DEFAULT "
    """'[".js",".jsx",".ts",".tsx",".py",".c",".h",".cpp",".cc",".hpp",".java",".go"]'""",
    "ALTER TABLE events ADD COLUMN common_code_min_teams INT NOT NULL DEFAULT 3 "
    "CHECK (common_code_min_teams >= 2)",
    "ALTER TABLE events ADD COLUMN baseline_retention_days INT NOT NULL DEFAULT 30 "
    "CHECK (baseline_retention_days BETWEEN 1 AND 365)",
    "ALTER TABLE teams ADD COLUMN members INT CHECK (members BETWEEN 1 AND 50)",
    """
    CREATE TABLE baseline_devices (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      team_id UUID NOT NULL REFERENCES teams(id),
      label TEXT NOT NULL,
      code_hash TEXT NOT NULL UNIQUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (team_id, label)
    )
    """,
    """
    CREATE TABLE baselines (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      device_id UUID NOT NULL REFERENCES baseline_devices(id),
      team_id UUID NOT NULL REFERENCES teams(id),
      status TEXT NOT NULL CHECK (status IN
        ('uploading','sealed','indexed','failed','superseded','purged')),
      started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      sealed_at TIMESTAMPTZ,
      late BOOLEAN,
      complete BOOLEAN,
      incomplete_reasons JSONB NOT NULL DEFAULT '[]',
      parts JSONB NOT NULL DEFAULT '[]',
      bytes_received BIGINT NOT NULL DEFAULT 0,
      files INT,
      summary JSONB,
      unreadable JSONB NOT NULL DEFAULT '[]',
      unreadable_more INT NOT NULL DEFAULT 0,
      scanner_version TEXT,
      platform TEXT,
      extensions JSONB,
      index_settings JSONB,
      index_summary JSONB,
      manifest_sha256 TEXT,
      receipt_hash TEXT,
      error_detail TEXT
    )
    """,
    "CREATE INDEX baselines_team_status ON baselines (team_id, status)",
    "CREATE UNIQUE INDEX baselines_one_live_per_device ON baselines (device_id) "
    "WHERE status IN ('sealed','indexed','failed')",
    "CREATE UNIQUE INDEX baselines_one_upload_per_device ON baselines (device_id) "
    "WHERE status = 'uploading'",
    """
    CREATE TABLE starters (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      event_id UUID NOT NULL REFERENCES events(id),
      name TEXT NOT NULL,
      archive_sha256 TEXT NOT NULL,
      files INT NOT NULL,
      runs BYTEA NOT NULL,
      settings JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (event_id, archive_sha256)
    )
    """,
    """
    CREATE TABLE prior_work_reports (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      submission_id UUID NOT NULL REFERENCES submissions(id),
      job_id UUID NOT NULL REFERENCES jobs(id),
      attempt_id UUID NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('ready','no_baseline','failed')),
      detail TEXT,
      totals JSONB,
      files JSONB NOT NULL DEFAULT '[]',
      settings JSONB NOT NULL DEFAULT '{}',
      baseline_ids JSONB NOT NULL DEFAULT '[]',
      purged_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (job_id, attempt_id)
    )
    """,
    "CREATE INDEX prior_work_reports_submission ON prior_work_reports (submission_id, created_at)",
    """
    CREATE TABLE prior_work_matches (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      report_id UUID NOT NULL REFERENCES prior_work_reports(id),
      ordinal INT NOT NULL,
      match_key TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN
        ('identical_file','unchanged','edited','possibly_related')),
      late BOOLEAN NOT NULL,
      tokens INT NOT NULL,
      score REAL,
      final_path TEXT NOT NULL,
      final_start_line INT NOT NULL,
      final_end_line INT NOT NULL,
      baseline_id UUID NOT NULL REFERENCES baselines(id),
      device_label TEXT NOT NULL,
      old_path TEXT NOT NULL,
      old_start_line INT NOT NULL,
      old_end_line INT NOT NULL,
      also_in INT NOT NULL DEFAULT 0,
      final_excerpt BYTEA,
      old_excerpt BYTEA,
      final_excerpt_line INT NOT NULL,
      old_excerpt_line INT NOT NULL,
      UNIQUE (report_id, ordinal)
    )
    """,
    """
    CREATE TABLE match_explanations (
      id BIGSERIAL PRIMARY KEY,
      submission_id UUID NOT NULL REFERENCES submissions(id),
      match_key TEXT NOT NULL DEFAULT '',
      text TEXT NOT NULL CHECK (char_length(text) <= 2000),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX match_explanations_latest ON match_explanations "
    "(submission_id, match_key, id DESC)",
    "ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check",
    f"ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ({_KINDS_AFTER}))",
    "ALTER TABLE jobs DROP CONSTRAINT jobs_lane_check",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_lane_check CHECK (lane IN ('watch','heavy','index'))",
)

DOWNGRADE: tuple[str, ...] = (
    "DELETE FROM jobs WHERE lane = 'index'",
    "ALTER TABLE jobs DROP CONSTRAINT jobs_lane_check",
    "ALTER TABLE jobs ADD CONSTRAINT jobs_lane_check CHECK (lane IN ('watch','heavy'))",
    "ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check",
    f"ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ({_KINDS_BEFORE}))",
    "DROP TABLE match_explanations",
    "DROP TABLE prior_work_matches",
    "DROP TABLE prior_work_reports",
    "DROP TABLE starters",
    "DROP TABLE baselines",
    "DROP TABLE baseline_devices",
    "ALTER TABLE teams DROP COLUMN members",
    "ALTER TABLE events DROP COLUMN baseline_retention_days",
    "ALTER TABLE events DROP COLUMN common_code_min_teams",
    "ALTER TABLE events DROP COLUMN scan_extensions",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(sa.text(statement))


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(sa.text(statement))
