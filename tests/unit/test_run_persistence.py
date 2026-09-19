from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import UserRunReport
from shortlist.server.db.models import Base
from shortlist.server.services.run_persistence import _cost_blob


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


class TestCostBlob:
    def test_seconds_become_integer_milliseconds(self):
        report = UserRunReport(username="alex", slug="alex")
        report.setup_s = 421.0
        report.row_timing = {"picked-for-you": {"duration_s": 12.04, "blocked_s": 0.31}}
        report.pool_costs = [
            {
                "label": "movie · tmdb, llm_web",
                "tokens": 15917,
                "exa_searches": 3,
                "duration_s": 398.0,
                "rows": ["picked-for-you"],
            }
        ]
        blob = _cost_blob(report)
        assert blob["setup_ms"] == 421000
        assert blob["rows"]["picked-for-you"] == {"duration_ms": 12040, "blocked_ms": 310}
        assert blob["pools"][0]["duration_ms"] == 398000
        assert blob["pools"][0]["tokens"] == 15917
        assert "duration_s" not in blob["pools"][0]

    def test_a_report_that_measured_nothing_persists_null(self):
        """Not `{}`: an empty blob would render as a real measurement of zero. A user who never
        reached the gather (no rows due) genuinely has nothing recorded."""
        assert _cost_blob(UserRunReport(username="alex", slug="alex")) is None


class TestTheShelfEventsANightlyRunEmits:
    """`_emit_hub_ordering_events` — the RUN path, which `jobs._audit_hub_orderings` mirrors for the
    on-demand handlers.

    Tested separately from the jobs path because they are separate emitters with separate scope
    names, and `docs/guides.md` tells owners to read THIS one back after a nightly run
    (`/api/events/log?scope=run.hub_unplaced` — the change log has no screen yet). The jobs-path
    tests in `test_jobs.py` cannot see a regression here.
    """

    @staticmethod
    def _emit(entries: list[dict], *, dry_run: bool = False) -> list[tuple]:
        from types import SimpleNamespace
        from unittest.mock import patch

        from shortlist.server.services import run_persistence as rp

        seen: list[tuple] = []
        report = SimpleNamespace(hub_orderings=entries, dry_run=dry_run)
        with patch.object(rp, "add_audit", lambda session, scope, level, **f: seen.append((scope, level, f))):
            rp._emit_hub_ordering_events(None, 7, report)
        return seen

    def test_a_placement_that_could_not_be_applied_gets_its_own_scope_and_no_verified(self):
        """`verified` answers "we asked Plex and it stuck". Nothing was asked here, so answering it
        would be a fabrication — and the separate scope is what keeps `_shelf_contention`'s bounded
        window holding only the repeated moves it counts."""
        seen = self._emit([{"library": "Movies", "placed": False, "moved": [], "reason": "anchor not found"}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_unplaced", "warning")]
        fields = seen[0][2]
        assert fields["reason"] == "anchor not found" and fields["verified"] is None
        assert fields["library"] == "Movies" and fields["run_id"] == 7

    def test_a_move_still_uses_the_ordinary_scope(self):
        seen = self._emit([{"library": "Movies", "moved": ["Picked for You"], "verified": True}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_order", "info")]

    def test_an_unverified_move_is_a_warning(self):
        """A shelf we asked for and did not get — the SFLIX case the whole audit was rebuilt around."""
        seen = self._emit([{"library": "Movies", "moved": ["Picked for You"], "verified": False}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_order", "warning")]

    def test_a_dry_run_is_never_a_warning_on_either_scope(self):
        """A preview asked Plex for nothing, so neither kind is an alarm."""
        seen = self._emit(
            [
                {"library": "Movies", "placed": False, "moved": [], "reason": "anchor not found"},
                {"library": "TV", "moved": ["row"], "verified": False},
            ],
            dry_run=True,
        )

        assert [(a[0], a[1]) for a in seen] == [("run.hub_unplaced", "info"), ("run.hub_order", "info")]


class TestPicksCarryTheBuiltAtStamp:
    """`built_at` has to survive the write as well as the read.

    The read back has a test (`test_previous_picks_carries_the_built_at_stamp`), but nothing
    exercised the WRITE: drop `built_at=pick.built_at` from `_persist_user_report` and every stamp is
    silently NULL, every carried row reads as "unknown", and the idle hold is inert on a real server
    with the whole suite green.
    """

    def test_a_persisted_pick_keeps_the_stamp_the_engine_put_on_it(self, sessions):
        from shortlist.engine.models import MediaType, Pick, UserRunReport
        from shortlist.server.db.models import PickRow, Run, User
        from shortlist.server.services.run_persistence import _persist_user_report

        built = datetime(2026, 8, 20, 3, 30, tzinfo=UTC)
        with sessions() as session:
            user = User(plex_account_id=1, username="sarah", slug="sarah", enabled=True)
            run = Run(trigger="manual", status="ok", dry_run=False, stats={})
            session.add_all([user, run])
            session.commit()
            report = UserRunReport(username="sarah", slug="sarah", status="ok")
            report.picks = [
                Pick(
                    tmdb_id=100,
                    rating_key=1,
                    title="T100",
                    rank=1,
                    reason="",
                    media_type=MediaType.MOVIE,
                    collection_slug="picked",
                    section_key="1",
                    built_at=built,
                )
            ]

            _persist_user_report(session, run.id, user, report, dry_run=False)
            session.commit()

            stored = session.query(PickRow).one()
            assert stored.built_at is not None, "the stamp was dropped on the way into the database"
            assert stored.built_at.replace(tzinfo=stored.built_at.tzinfo or UTC) == built


class TestTheRequestSurfaceIsPersisted:
    """A person's missing titles are rewritten whole per run — and left alone by a run that produced
    none, so an engine hiccup never blanks their page."""

    def _missing(self, *ids: int) -> list[dict]:
        return [
            {
                "rank": i + 1,
                "tmdb_id": t,
                "media_type": "movie",
                "title": f"T{t}",
                "genres": ["Drama"],
                "reason": "Because",
                "kids": t == 3,
                "sources": ["engine:e"],
                "seed_tmdb_id": 9,
                "seed_title": "S",
            }
            for i, t in enumerate(ids)
        ]

    def test_written_whole_replaced_whole_and_kept_when_a_run_has_none(self, sessions):
        from shortlist.engine.models import UserRunReport
        from shortlist.server.db.models import Run, User, UserSuggestion
        from shortlist.server.services.run_persistence import _persist_user_report

        with sessions() as session:
            user = User(plex_account_id=1, username="sarah", slug="sarah", enabled=True)
            runs = [Run(trigger="manual", status="ok", dry_run=False, stats={}) for _ in range(4)]
            session.add_all([user, *runs])
            session.commit()
            run = runs[0]

            report = UserRunReport(username="sarah", slug="sarah", status="ok", missing=self._missing(1, 2, 3))
            _persist_user_report(session, run.id, user, report, dry_run=False)
            session.commit()
            rows = session.query(UserSuggestion).order_by(UserSuggestion.rank).all()
            assert [(r.tmdb_id, r.rank, r.kids, r.sources) for r in rows] == [
                (1, 1, False, "engine:e"),
                (2, 2, False, "engine:e"),
                (3, 3, True, "engine:e"),
            ]
            assert rows[0].run_id == run.id and rows[0].genres == ["Drama"]

            report = UserRunReport(username="sarah", slug="sarah", status="ok", missing=self._missing(5))
            _persist_user_report(session, runs[1].id, user, report, dry_run=False)
            session.commit()
            assert [r.tmdb_id for r in session.query(UserSuggestion).all()] == [5]

            report = UserRunReport(username="sarah", slug="sarah", status="cold_start", missing=[])
            _persist_user_report(session, runs[2].id, user, report, dry_run=False)
            session.commit()
            assert [r.tmdb_id for r in session.query(UserSuggestion).all()] == [5]

            report = UserRunReport(username="sarah", slug="sarah", status="ok", missing=self._missing(7))
            _persist_user_report(session, runs[3].id, user, report, dry_run=True)
            session.commit()
            assert [r.tmdb_id for r in session.query(UserSuggestion).all()] == [5]  # a dry run writes nothing


class TestTheLedgerRecordsWhatWasWrittenToASummaryAndSortTitle:
    """Issue #120. The ledger's record is what lets clearing a row's field hand back ONLY what Shortlist
    wrote — so the persist must forget a record the run cleared, and must keep one a run never reached."""

    def _entry(self, **details) -> dict:
        return {"row_slug": "gems", "library_key": "1", "rating_key": 42, "row_title": "Gems", **details}

    def test_a_record_the_run_wrote_is_stored_and_one_it_cleared_is_forgotten(self, sessions):
        from shortlist.server.db.models import Delivery
        from shortlist.server.services.run_persistence import _record_deliveries

        with sessions() as session:
            _record_deliveries(session, "sarah", [self._entry(summary_written="Hi", title_sort_written="!1_Gems")])
            row = session.get(Delivery, ("gems", "sarah", "1"))
            assert (row.summary_written, row.title_sort_written) == ("Hi", "!1_Gems")

            _record_deliveries(session, "sarah", [self._entry(summary_written=None, title_sort_written=None)])
            assert (row.summary_written, row.title_sort_written) == (None, None)

    def test_an_entry_without_the_keys_keeps_the_record(self, sessions):
        """A legacy breakdown, or a library delivery never reached the description step for, says
        nothing about what Plex holds — forgetting would strand a value Shortlist really wrote."""
        from shortlist.server.db.models import Delivery
        from shortlist.server.services.run_persistence import _record_deliveries

        with sessions() as session:
            _record_deliveries(session, "sarah", [self._entry(summary_written="Hi", title_sort_written=None)])
            _record_deliveries(session, "sarah", [self._entry()])
            assert session.get(Delivery, ("gems", "sarah", "1")).summary_written == "Hi"
