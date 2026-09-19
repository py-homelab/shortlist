"""`users.household`: who watches under each account as of the last run (adult | family | kids, what
decided it, and the counts behind it). Nullable JSON, no backfill — NULL until a run could say.

Re-runnable because the add is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0096
Revises: 0095
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("users")}


def upgrade() -> None:
    if "household" not in _columns(op.get_bind()):
        op.add_column("users", sa.Column("household", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "household" in _columns(op.get_bind()):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("household")
