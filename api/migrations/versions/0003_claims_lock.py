"""An event can set when its teams' claims freeze (D-047 point 6).

`claims_lock_at` is optional: left NULL, the lock falls two thirds of the way from kickoff to
deadline (`core.events.claims_lock_at`). When set, it lies inside the event.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE events ADD COLUMN claims_lock_at TIMESTAMPTZ",
    "ALTER TABLE events ADD CONSTRAINT events_claims_lock_in_event CHECK "
    "(claims_lock_at IS NULL OR (claims_lock_at >= kickoff_at AND claims_lock_at <= deadline_at))",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE events DROP CONSTRAINT events_claims_lock_in_event",
    "ALTER TABLE events DROP COLUMN claims_lock_at",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(sa.text(statement))


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(sa.text(statement))
