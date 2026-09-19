"""Retire the owner-only request inbox and its Radarr/Sonarr routing.

Requests are filed by each person themselves, from their own picks page, through the *seerr as THEM
(`/api/me`, migration 0094's tables). Everything that fed, stored and routed the old inbox goes:

* the `request_candidates` table's rows — the inbox itself. The table is emptied, not dropped:
  earlier frozen migrations alter it unguarded, and a drop would make them un-replayable;
* `users.request_tag` — the Arr tag a person's requests used to carry (`collections.request_tag`
  stays, retired: the frozen 0001 seed names it);
* every per-row `req_*` request override on `collections` (0074, 0075, 0084, 0085).

Three settings survive under new names, because the per-person page and the "Highest rated" row order
still need them. Their rows are RENAMED in place, values untouched — the api keys are Fernet tokens
and Fernet is not bound to the key name, so the encrypted value moves as-is:

    requests.overseerr.url     -> seerr.url
    requests.overseerr.apikey  -> seerr.apikey
    requests.mdblist.apikey    -> recommendations.mdblist.apikey

The inbox's own webhook event (`requests.waiting`) is taken out of `notify.webhook.events`.

A rename only happens when the new key has no row of its own, so re-running (or a database that
already carries the new keys) changes nothing. Every other `requests.*` row is left for
`SettingsStore.purge_legacy` to delete on boot, which runs AFTER migrations.

SQLite cannot drop a column that an index or constraint references without rebuilding the table, so
the drops go through `batch_alter_table`, as 0053 did. Every step is guarded, so the migration is
re-runnable (`tests/integration/test_migration_recovery.py`).

Revision ID: 0095
Revises: 0094
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

#: Settings rows moved to their new names, old -> new.
RENAMED_SETTINGS = {
    "requests.overseerr.url": "seerr.url",
    "requests.overseerr.apikey": "seerr.apikey",
    "requests.mdblist.apikey": "recommendations.mdblist.apikey",
}

#: (column, type) — every per-row request override. All nullable. `collections.request_tag` is NOT here:
#: the frozen 0001 default-row seed names it and runs against the head schema on crash recovery, so it
#: stays (retired, unused).
_COLLECTION_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("req_min_rating", sa.Float()),
    ("req_min_votes", sa.Integer()),
    ("req_min_demand", sa.Integer()),
    ("req_min_year", sa.Integer()),
    ("req_max_year", sa.Integer()),
    ("req_auto_send", sa.Boolean()),
    ("req_auto_min_demand", sa.Integer()),
    ("req_auto_min_rating", sa.Float()),
    ("req_max_per_row", sa.Integer()),
    ("req_radarr_quality_profile_id", sa.Integer()),
    ("req_radarr_root_folder", sa.String(length=512)),
    ("req_sonarr_quality_profile_id", sa.Integer()),
    ("req_sonarr_root_folder", sa.String(length=512)),
    ("req_sonarr_monitor", sa.String(length=32)),
    ("req_language_mode", sa.String(length=16)),
    ("req_preferred_languages", sa.JSON()),
    ("req_min_rating_other", sa.Float()),
    ("req_auto_user_tag", sa.Boolean()),
)


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _rename_settings(bind) -> None:
    for old, new in RENAMED_SETTINGS.items():
        have_new = bind.execute(sa.text("select 1 from settings where key = :k"), {"k": new}).scalar()
        if have_new is not None:
            continue  # the new key already exists — never overwrite it with the old row
        bind.execute(sa.text("update settings set key = :new where key = :old"), {"new": new, "old": old})


#: Webhook events that only the inbox raised. An owner subscribed to one would otherwise keep an
#: unknown event in `notify.webhook.events`, and the validator would refuse their next save of it.
_DEAD_EVENTS = ("requests.waiting",)


def _drop_dead_events(bind) -> None:
    raw = bind.execute(sa.text("select value from settings where key = 'notify.webhook.events'")).scalar()
    if raw is None:
        return
    value = json.loads(raw) if isinstance(raw, str) else raw
    events = value.get("v") if isinstance(value, dict) and "v" in value else value
    if not isinstance(events, list) or not any(e in _DEAD_EVENTS for e in events):
        return
    kept = [e for e in events if e not in _DEAD_EVENTS]
    bind.execute(
        sa.text("update settings set value = :v where key = 'notify.webhook.events'"),
        {"v": json.dumps({"v": kept})},
    )


def upgrade() -> None:
    bind = op.get_bind()

    _rename_settings(bind)
    _drop_dead_events(bind)

    # Emptied, not dropped: 0044/0051/0071/0085 alter this table without a table-existence guard and
    # are frozen, so a dropped table would make each of them fail on a crash-recovery replay.
    if "request_candidates" in _tables(bind):
        bind.execute(sa.text("delete from request_candidates"))

    doomed = [name for name, _type in _COLLECTION_COLUMNS if name in _columns(bind, "collections")]
    if doomed:
        with op.batch_alter_table("collections") as batch_op:
            for name in doomed:
                batch_op.drop_column(name)

    if "request_tag" in _columns(bind, "users"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("request_tag")


def downgrade() -> None:
    """Re-create the columns (nullable, empty) and the table as 0001 + 0044/0051/0071/0074/0085 left
    it. The inbox's contents and the tags cannot be restored; the settings renames are reversed."""
    bind = op.get_bind()

    for old, new in RENAMED_SETTINGS.items():
        have_old = bind.execute(sa.text("select 1 from settings where key = :k"), {"k": old}).scalar()
        if have_old is None:
            bind.execute(sa.text("update settings set key = :old where key = :new"), {"new": new, "old": old})

    if "request_tag" not in _columns(bind, "users"):
        op.add_column("users", sa.Column("request_tag", sa.String(length=64), nullable=False, server_default=""))

    existing = _columns(bind, "collections")
    for name, type_ in _COLLECTION_COLUMNS:
        if name in existing:
            continue
        op.add_column("collections", sa.Column(name, type_, nullable=True))

    if "request_candidates" not in _tables(bind):
        op.create_table(
            "request_candidates",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("tmdb_id", sa.Integer(), nullable=False),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("year", sa.Integer(), nullable=True),
            sa.Column("imdb_id", sa.String(length=16), nullable=False),
            sa.Column("poster_path", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("overview", sa.Text(), nullable=False, server_default=""),
            sa.Column("row_slug", sa.String(length=255), nullable=True),
            sa.Column("rating", sa.Float(), nullable=False),
            sa.Column("vote_count", sa.Integer(), nullable=False),
            sa.Column("language", sa.String(length=16), nullable=False, server_default=""),
            sa.Column("demand", sa.Integer(), nullable=False),
            sa.Column("tags", sa.JSON(), nullable=False),
            sa.Column("wanters", sa.JSON(), nullable=False),
            sa.Column("why", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("detail", sa.String(length=512), nullable=False),
            sa.Column("arr_slug", sa.String(length=256), nullable=True),
            sa.Column("excluded", sa.Boolean(), nullable=False),
            sa.Column("hidden", sa.Boolean(), server_default="0", nullable=False),
            sa.Column("first_seen_run_id", sa.Integer(), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tmdb_id", "media_type", name="uq_request_candidate_title"),
        )
        op.create_index(op.f("ix_request_candidates_status"), "request_candidates", ["status"], unique=False)
        op.create_index(op.f("ix_request_candidates_tmdb_id"), "request_candidates", ["tmdb_id"], unique=False)
