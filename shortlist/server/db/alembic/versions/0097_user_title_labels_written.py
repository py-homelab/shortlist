"""`users.title_labels_written`: Shortlist's own record of which of the owner's title labels it wrote
into which share-filter field of this account (`{"filterMovies": {"admit": [...], "hide": [...]}}`).
Nullable JSON, no backfill — NULL until a privacy pass writes one.

A column of its own rather than a key in `prefs`, deliberately. The record decides which labels may
ever be REMOVED from someone's Plex restriction — it is the only thing that tells a label Shortlist
added from one the owner typed — and `prefs` is rewritten whole by every settings PATCH. A PATCH that
read `prefs` a moment before a privacy pass saved the record would write the old record back over it,
and a lost record turns Shortlist's labels into "the owner's" for good. SQLAlchemy updates only the
columns that changed, so as separate columns neither writer can touch the other's.

Re-runnable because the add is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0097
Revises: 0096
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("users")}


def upgrade() -> None:
    if "title_labels_written" not in _columns(op.get_bind()):
        op.add_column("users", sa.Column("title_labels_written", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "title_labels_written" in _columns(op.get_bind()):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("title_labels_written")
