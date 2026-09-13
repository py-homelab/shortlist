"""A row's own Plex summary and sort-title prefix, and what Shortlist last wrote of them (issue #120).

`collections.description` and `collections.sort_title_prefix` are the owner's settings. Both are NOT
NULL with a server default of "", and "" means Shortlist leaves that field on Plex alone — so every
existing row comes out of this migration unchanged, and a summary or sort title another tool (agregarr,
Kometa) put on a Shortlist row survives the upgrade.

`deliveries.summary_written` and `deliveries.title_sort_written` record, per collection, the value
Shortlist last wrote. NULL means it wrote none, which is what every existing collection is. They exist
so that clearing a setting hands back only a value Plex still holds exactly as Shortlist wrote it,
and never one a person or another tool put there. No backfill: Shortlist has never written either field.

Re-runnable because every add is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0091
Revises: 0090
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("collections", sa.Column("description", sa.Text(), nullable=False, server_default="")),
    ("collections", sa.Column("sort_title_prefix", sa.String(64), nullable=False, server_default="")),
    ("deliveries", sa.Column("summary_written", sa.Text(), nullable=True)),
    ("deliveries", sa.Column("title_sort_written", sa.Text(), nullable=True)),
)


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table, column in _COLUMNS:
        if column.name not in _columns(bind, table):
            op.add_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    for table, column in reversed(_COLUMNS):
        if column.name in _columns(bind, table):
            op.drop_column(table, column.name)
