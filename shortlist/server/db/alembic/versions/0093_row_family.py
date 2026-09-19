"""A row's `family` setting: keep children's titles in ("include", the default and every existing row's
behaviour), out ("exclude"), or build the row from nothing else ("only").

One nullable=False column with a server default, so every existing row comes out unchanged. No backfill.
Re-runnable because the add is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0093
Revises: 0092
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None

_COLUMN = sa.Column("family", sa.String(16), nullable=False, server_default="include")


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("collections")}


def upgrade() -> None:
    if _COLUMN.name not in _columns(op.get_bind()):
        op.add_column("collections", _COLUMN)


def downgrade() -> None:
    if _COLUMN.name in _columns(op.get_bind()):
        op.drop_column("collections", _COLUMN.name)
