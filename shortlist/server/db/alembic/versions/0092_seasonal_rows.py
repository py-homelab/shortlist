"""A row's seasons: the calendar it follows, and how early and how late it shows each one (discussion #124).

`collections.seasons` is a JSON list of season slugs (`engine/seasons.py`). NOT NULL with a server default
of "[]", and [] means the row is not seasonal — so every existing row comes out of this migration unchanged.
`season_lead_days` / `season_after_days` are how many days before and after a season's day the row shows;
they mean nothing on a row with no seasons. No backfill.

Re-runnable because every add is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0092
Revises: 0091
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("seasons", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("season_lead_days", sa.Integer(), nullable=False, server_default="30"),
    sa.Column("season_after_days", sa.Integer(), nullable=False, server_default="0"),
)


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("collections")}


def upgrade() -> None:
    bind = op.get_bind()
    for column in _COLUMNS:
        if column.name not in _columns(bind):
            op.add_column("collections", column)


def downgrade() -> None:
    bind = op.get_bind()
    for column in reversed(_COLUMNS):
        if column.name in _columns(bind):
            op.drop_column("collections", column.name)
