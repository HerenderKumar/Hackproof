"""Initial schema: the DDL of section 10 of the brief, copied verbatim (D-028).

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    """\
CREATE TABLE events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  kickoff_at TIMESTAMPTZ NOT NULL,
  deadline_at TIMESTAMPTZ NOT NULL,
  rubric_version INT NOT NULL DEFAULT 1,
  rubric JSONB NOT NULL,                    -- [{"id","name","description","max"}]
  policy_text TEXT NOT NULL DEFAULT '',
  bulk_push_lines INT NOT NULL DEFAULT 2000,
  bulk_push_window_minutes INT NOT NULL DEFAULT 60,
  poll_interval_seconds INT NOT NULL DEFAULT 300,
  scores_locked BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (deadline_at > kickoff_at)
)""",
    """\
CREATE TABLE teams (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id UUID NOT NULL REFERENCES events(id),
  name TEXT NOT NULL,
  contact_email TEXT,
  repo_url TEXT,
  repo_provider TEXT CHECK (repo_provider IN ('github','local')),
  repository_id TEXT,                       -- GitHub numeric ID survives renames
  default_branch TEXT,
  repo_created_at TIMESTAMPTZ,
  registered_at TIMESTAMPTZ,
  rules_acknowledged_at TIMESTAMPTZ,
  health_path TEXT NOT NULL DEFAULT '/',
  UNIQUE (event_id, name)
)""",
    """\
CREATE TABLE judges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id UUID NOT NULL REFERENCES events(id),
  name TEXT NOT NULL,
  email TEXT
)""",
    """\
CREATE TABLE access_links (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id UUID NOT NULL REFERENCES events(id),
  role TEXT NOT NULL CHECK (role IN ('judge','team')),
  subject_id UUID NOT NULL,                 -- judges.id or teams.id
  token_hash TEXT NOT NULL UNIQUE,          -- sha256 hex of the token
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    """\
CREATE TABLE repo_observations (
  id BIGSERIAL PRIMARY KEY,
  team_id UUID NOT NULL REFERENCES teams(id),
  kind TEXT NOT NULL CHECK (kind IN ('kickoff','late_registration','poll')),
  observed_at TIMESTAMPTZ NOT NULL,
  head_sha TEXT,                            -- NULL for an empty repo
  previous_head_sha TEXT,
  descends_from_previous BOOLEAN,           -- NULL for kickoff rows
  lines_added INT,
  lines_total INT,
  files_changed INT,
  commits JSONB NOT NULL DEFAULT '[]'       -- new commits: [{"sha","author_date","committer_date","message"}]
)""",
    """\
CREATE INDEX ON repo_observations (team_id, observed_at)""",
    """\
CREATE TABLE claims (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id UUID NOT NULL REFERENCES teams(id),
  version INT NOT NULL,
  items JSONB NOT NULL,                     -- [{"id","text","target","example_input","expected_behaviour"}]
  claims_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (team_id, version),
  CHECK (jsonb_array_length(items) BETWEEN 1 AND 3)
)""",
    """\
CREATE TABLE snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id UUID NOT NULL REFERENCES teams(id),
  commit_sha TEXT NOT NULL,
  archive_sha256 TEXT NOT NULL,
  archive_path TEXT NOT NULL,
  commit_history JSONB NOT NULL,            -- self-reported: [{"sha","author_date","committer_date","message"}]
  lines_total INT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (team_id, commit_sha)
)""",
    """\
CREATE TABLE test_specs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_id UUID NOT NULL REFERENCES snapshots(id),
  claims_version INT NOT NULL,
  version INT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('startup','api','ui')),
  claim_id TEXT,                            -- NULL for the startup check
  claim_type TEXT NOT NULL CHECK (claim_type IN
    ('startup','calculation','create_update','classification','authentication','ui_workflow')),
  steps JSONB NOT NULL,                     -- section 14; assertions live inside steps
  practice_cases JSONB NOT NULL DEFAULT '[]',
  final_cases JSONB NOT NULL DEFAULT '[]',
  spec_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','needs_clarification','approved','superseded')),
  clarification_reason TEXT,
  approved_at TIMESTAMPTZ,
  approved_spec_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    """\
CREATE TABLE submissions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id UUID NOT NULL REFERENCES teams(id),
  snapshot_id UUID NOT NULL REFERENCES snapshots(id),
  mode TEXT NOT NULL CHECK (mode IN ('sandbox','url')),
  deploy_url TEXT,
  claims_version INT NOT NULL,
  claims_hash TEXT NOT NULL,
  test_spec_hash TEXT NOT NULL,
  rubric_version INT NOT NULL,
  runner_version TEXT NOT NULL,
  railpack_version TEXT NOT NULL,
  reuse_declaration JSONB NOT NULL,         -- {"starter_code":bool,"earlier_work":bool,"ai_assistance":bool,"details":str}
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  idempotency_key TEXT NOT NULL UNIQUE,
  receipt_hash TEXT NOT NULL
)""",
    """\
-- Immutability: block UPDATE and DELETE.
CREATE FUNCTION forbid_submission_change() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'submissions are immutable'; END; $$ LANGUAGE plpgsql""",
    """\
CREATE TRIGGER submissions_immutable BEFORE UPDATE OR DELETE ON submissions
  FOR EACH ROW EXECUTE FUNCTION forbid_submission_change()""",
    """\
CREATE TABLE jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind TEXT NOT NULL CHECK (kind IN ('kickoff_snapshot','poll_repo','prepare_snapshot',
    'draft_specs','self_check_run','final_run','audit','brief')),
  lane TEXT NOT NULL CHECK (lane IN ('watch','heavy')),
  priority INT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','cancelled')),
  team_id UUID REFERENCES teams(id),
  snapshot_id UUID REFERENCES snapshots(id),
  submission_id UUID REFERENCES submissions(id),
  depends_on UUID REFERENCES jobs(id),
  payload JSONB NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL UNIQUE,
  attempt_id UUID,
  attempts INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 3,
  lease_until TIMESTAMPTZ,
  run_after TIMESTAMPTZ NOT NULL DEFAULT now(),
  error_class TEXT CHECK (error_class IN ('infra','app','budget','validation')),
  error_detail TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
)""",
    """\
CREATE INDEX ON jobs (lane, status, priority DESC, created_at)""",
    """\
CREATE TABLE run_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID NOT NULL REFERENCES jobs(id),
  attempt_id UUID NOT NULL,
  build_status TEXT NOT NULL CHECK (build_status IN ('ok','fail','skipped')),
  build_exit_code INT,
  build_method TEXT CHECK (build_method IN ('railpack','dockerfile')),
  startup_status TEXT NOT NULL CHECK (startup_status IN ('ok','fail','skipped')),
  startup_detail TEXT,
  image_digest TEXT,
  build_config JSONB,                       -- Railpack plan or Dockerfile info, versions
  flags_used JSONB NOT NULL DEFAULT '{}',   -- records root or read-only fallback, port used
  build_log_path TEXT,
  app_log_path TEXT,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  UNIQUE (job_id, attempt_id)
)""",
    """\
CREATE TABLE test_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID NOT NULL REFERENCES jobs(id),
  attempt_id UUID NOT NULL,
  spec_id UUID NOT NULL REFERENCES test_specs(id),
  case_id TEXT,
  verdict TEXT NOT NULL CHECK (verdict IN ('pass','fail','blocked','unverified')),
  expected JSONB,
  actual JSONB,
  assertion_results JSONB,
  duration_ms INT,
  video_path TEXT,
  trace_path TEXT,
  error TEXT
)""",
    """\
CREATE TABLE audits (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  submission_id UUID NOT NULL REFERENCES submissions(id),
  job_id UUID NOT NULL REFERENCES jobs(id),
  attempt_id UUID NOT NULL,
  content JSONB NOT NULL,                   -- validated audit (section 15.3)
  citations_dropped INT NOT NULL DEFAULT 0,
  partial BOOLEAN NOT NULL DEFAULT false,
  code_map JSONB,                           -- neighbourhoods per claim (section 16)
  code_map_status TEXT CHECK (code_map_status IN ('ok','fallback','failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    """\
CREATE TABLE briefs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  submission_id UUID NOT NULL REFERENCES submissions(id),
  job_id UUID NOT NULL REFERENCES jobs(id),
  attempt_id UUID NOT NULL,
  content JSONB NOT NULL,                   -- everything the brief page renders
  ai_status TEXT NOT NULL CHECK (ai_status IN ('ok','partial','failed')),
  model TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    """\
CREATE TABLE claim_marks (
  judge_id UUID NOT NULL REFERENCES judges(id),
  submission_id UUID NOT NULL REFERENCES submissions(id),
  claim_id TEXT NOT NULL,
  mark TEXT NOT NULL CHECK (mark IN ('verified_live','not_verified')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (judge_id, submission_id, claim_id)
)""",
    """\
CREATE TABLE scores (
  judge_id UUID NOT NULL REFERENCES judges(id),
  submission_id UUID NOT NULL REFERENCES submissions(id),
  criterion_id TEXT NOT NULL,
  value INT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (judge_id, submission_id, criterion_id)
)""",
    """\
CREATE TABLE brief_views (
  id BIGSERIAL PRIMARY KEY,
  judge_id UUID NOT NULL REFERENCES judges(id),
  submission_id UUID NOT NULL REFERENCES submissions(id),
  opened_at TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    """\
CREATE TABLE llm_calls (
  id BIGSERIAL PRIMARY KEY,
  job_id UUID REFERENCES jobs(id),
  purpose TEXT NOT NULL,                    -- draft, audit, brief
  model TEXT NOT NULL,
  input_tokens INT,
  output_tokens INT,
  duration_ms INT,
  ok BOOLEAN NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
)

TABLES: tuple[str, ...] = (
    "events",
    "teams",
    "judges",
    "access_links",
    "repo_observations",
    "claims",
    "snapshots",
    "test_specs",
    "submissions",
    "jobs",
    "run_results",
    "test_results",
    "audits",
    "briefs",
    "claim_marks",
    "scores",
    "brief_views",
    "llm_calls",
)


def _run(statement: str) -> None:
    # The DDL's comments hold JSON examples; escape their colons so they are not bind parameters.
    op.execute(sa.text(statement.replace(":", "\\:")))


def upgrade() -> None:
    for statement in STATEMENTS:
        _run(statement)


def downgrade() -> None:
    for table in reversed(TABLES):
        _run(f"DROP TABLE {table}")
    _run("DROP FUNCTION forbid_submission_change()")
