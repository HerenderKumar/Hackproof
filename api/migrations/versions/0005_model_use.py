"""The model door's summary of each run (Prune design, B3): `run_results.model_use`.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE run_results ADD COLUMN model_use JSONB"))


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE run_results DROP COLUMN model_use"))
