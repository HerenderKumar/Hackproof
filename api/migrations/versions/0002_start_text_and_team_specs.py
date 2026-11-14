"""The start text lives on the claims version; tests belong to the team's claims (D-047).

A team's claims version now carries its problem statement and explanation, the text tests are
drafted from. Tests are drafted before any code exists, so a `test_specs` row is owned by the team
and its claims version, and its snapshot is optional.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE claims ADD COLUMN problem_statement TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE claims ADD COLUMN explanation TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE test_specs ADD COLUMN team_id UUID REFERENCES teams(id)",
    "UPDATE test_specs s SET team_id = n.team_id FROM snapshots n WHERE s.snapshot_id = n.id",
    "ALTER TABLE test_specs ALTER COLUMN team_id SET NOT NULL",
    "ALTER TABLE test_specs ALTER COLUMN snapshot_id DROP NOT NULL",
    "CREATE INDEX test_specs_team_claims ON test_specs (team_id, claims_version)",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX test_specs_team_claims",
    # Rows drafted without a snapshot cannot exist under 0001's schema.
    "DELETE FROM test_specs WHERE snapshot_id IS NULL",
    "ALTER TABLE test_specs ALTER COLUMN snapshot_id SET NOT NULL",
    "ALTER TABLE test_specs DROP COLUMN team_id",
    "ALTER TABLE claims DROP COLUMN explanation",
    "ALTER TABLE claims DROP COLUMN problem_statement",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(sa.text(statement))


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(sa.text(statement))
