"""The per-person picks surface: each person's ranked missing titles (`user_suggestions`), their
standing answers (`dismissals`), what they saw and did (`pick_events`), and what they requested
(`request_log`). All four cascade with the person — a removed user's picks are theirs and nobody's.

Re-runnable: every create is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0094
Revises: 0093
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _tables()
    if "user_suggestions" not in have:
        op.create_table(
            "user_suggestions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("rank", sa.Integer, nullable=False),
            sa.Column("tmdb_id", sa.Integer, nullable=False),
            sa.Column("media_type", sa.String(16), nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("year", sa.Integer, nullable=True),
            sa.Column("genres", sa.JSON, nullable=False, server_default="[]"),
            sa.Column("rating", sa.Float, nullable=True),
            sa.Column("vote_count", sa.Integer, nullable=True),
            sa.Column("poster_path", sa.String(256), nullable=False, server_default=""),
            sa.Column("overview", sa.Text, nullable=False, server_default=""),
            sa.Column("language", sa.String(16), nullable=False, server_default=""),
            sa.Column("reason", sa.String(512), nullable=False, server_default=""),
            sa.Column("kids", sa.Boolean, nullable=False, server_default="0"),
            sa.Column("seed_tmdb_id", sa.Integer, nullable=True),
            sa.Column("seed_title", sa.String(512), nullable=True),
            sa.Column("sources", sa.String(256), nullable=False, server_default=""),
            sa.Column("run_id", sa.Integer, nullable=True),
            sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "tmdb_id", "media_type", name="uq_user_suggestion"),
        )
        op.create_index("ix_user_suggestions_user_id", "user_suggestions", ["user_id"])
    if "dismissals" not in have:
        op.create_table(
            "dismissals",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tmdb_id", sa.Integer, nullable=False),
            sa.Column("media_type", sa.String(16), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("title", sa.String(512), nullable=False, server_default=""),
            sa.Column("year", sa.Integer, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "tmdb_id", "media_type", name="uq_dismissal"),
        )
        op.create_index("ix_dismissals_user_id", "dismissals", ["user_id"])
    if "pick_events" not in have:
        op.create_table(
            "pick_events",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tmdb_id", sa.Integer, nullable=False),
            sa.Column("media_type", sa.String(16), nullable=False),
            sa.Column("event", sa.String(16), nullable=False),
            sa.Column("surface", sa.String(16), nullable=True),
            sa.Column("position", sa.Integer, nullable=True),
            sa.Column("source", sa.String(64), nullable=False, server_default=""),
            sa.Column("meta", sa.JSON, nullable=True),
            sa.Column("shown_day", sa.String(10), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "tmdb_id", "media_type", "surface", "shown_day", name="uq_pick_event_shown"),
        )
        op.create_index("ix_pick_events_user_created", "pick_events", ["user_id", "created_at"])
        op.create_index("ix_pick_events_title", "pick_events", ["tmdb_id", "media_type"])
    if "request_log" not in have:
        op.create_table(
            "request_log",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tmdb_id", sa.Integer, nullable=False),
            sa.Column("media_type", sa.String(16), nullable=False),
            sa.Column("seerr_user_id", sa.Integer, nullable=True),
            sa.Column("seerr_request_id", sa.Integer, nullable=True),
            sa.Column("seerr_status", sa.String(16), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_request_log_user_id", "request_log", ["user_id"])


def downgrade() -> None:
    have = _tables()
    for table in ("request_log", "pick_events", "dismissals", "user_suggestions"):
        if table in have:
            op.drop_table(table)
