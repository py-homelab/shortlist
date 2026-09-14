"""A row's own summary and sort title on Plex (issue #120).

The matrix that decides every write is (the row's field: set / empty) x (the ledger: Shortlist wrote a
value / did not) x (Plex: holds the wanted value / holds Shortlist's old value / holds someone else's).
Plex's behaviour behind each assertion is recorded in tests/fixtures/pms_collection_field_edits.json.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.delivery import apply_row_details, deliver_rows
from shortlist.engine.models import EngineConfig, MediaType, Pick, RowSpec, WrittenDetails
from tests.conftest import make_profile

DISPLAY = "✨ Hidden Gems"
NOTHING_WRITTEN = WrittenDetails()


def _spec(description: str = "", prefix: str = "") -> RowSpec:
    return RowSpec(slug="gems", name_template=DISPLAY, size=5, description=description, sort_title_prefix=prefix)


def _pick(seed_title: str | None = "Fargo") -> Pick:
    return Pick(
        tmdb_id=1,
        rating_key=1001,
        title="Movie 1",
        rank=1,
        reason="",
        media_type=MediaType.MOVIE,
        seed_title=seed_title,
        seed_tmdb_id=900 if seed_title else None,
    )


def _collection(summary: str = "", title_sort: str = " Hidden Gems") -> SimpleNamespace:
    return SimpleNamespace(title=DISPLAY, summary=summary, titleSort=title_sort, ratingKey=42)


def _apply(plex, collection, spec, written=NOTHING_WRITTEN, *, picks=None, dry_run=False, library="Movies"):
    return apply_row_details(
        plex,
        collection,
        spec,
        make_profile(),
        [_pick()] if picks is None else picks,
        display=DISPLAY,
        library_name=library,
        written=written,
        dry_run=dry_run,
    )


class TestNothingManaged:
    def test_an_empty_row_never_reads_or_writes_the_collection_when_nothing_was_ever_written(self):
        """The upgrade case, and every agregarr user's: both fields empty, no ledger record. A value
        another tool set must survive, so the collection is not even looked at."""
        plex = MagicMock(spec=PlexClient)
        untouchable = MagicMock(spec=[])  # any attribute read raises

        written, changes = _apply(plex, untouchable, _spec())

        assert written == WrittenDetails()
        assert changes == {}
        plex.edit_collection_fields.assert_not_called()

    def test_a_whitespace_prefix_is_no_prefix(self):
        plex = MagicMock(spec=PlexClient)

        written, changes = _apply(plex, _collection(), _spec(prefix="   "))

        assert (written, changes) == (WrittenDetails(), {})
        plex.edit_collection_fields.assert_not_called()


class TestSettingAField:
    def test_both_fields_go_to_plex_in_one_write_and_are_recorded(self):
        plex = MagicMock(spec=PlexClient)
        collection = _collection()

        written, changes = _apply(plex, collection, _spec(description="Picked nightly", prefix="!010_"))

        plex.edit_collection_fields.assert_called_once_with(
            collection, {"summary": "Picked nightly", "titleSort": "!010_✨ Hidden Gems"}
        )
        assert written == WrittenDetails(summary="Picked nightly", title_sort="!010_✨ Hidden Gems")
        assert changes == {"description": "set", "sort_title": "set"}
        plex.reread_collection.assert_not_called()  # only a clear pays for a fresh read

    def test_a_value_plex_already_holds_is_not_rewritten_but_is_recorded(self):
        plex = MagicMock(spec=PlexClient)
        collection = _collection(summary="Picked nightly", title_sort="!010_✨ Hidden Gems")

        written, changes = _apply(plex, collection, _spec(description="Picked nightly", prefix="!010_"))

        plex.edit_collection_fields.assert_not_called()
        assert written == WrittenDetails(summary="Picked nightly", title_sort="!010_✨ Hidden Gems")
        assert changes == {}

    def test_only_the_field_that_differs_is_written(self):
        plex = MagicMock(spec=PlexClient)
        collection = _collection(summary="Picked nightly")

        _apply(plex, collection, _spec(description="Picked nightly", prefix="!010_"))

        plex.edit_collection_fields.assert_called_once_with(collection, {"titleSort": "!010_✨ Hidden Gems"})

    def test_the_description_fills_the_same_placeholders_as_the_name(self):
        plex = MagicMock(spec=PlexClient)
        collection = _collection()

        _apply(plex, collection, _spec(description="For {user}: {library_name} after {top_seed}"), library="4K")

        assert plex.edit_collection_fields.call_args.args[1] == {"summary": "For sarah: 4K after Fargo"}

    def test_a_description_keeps_its_line_breaks_when_it_names_the_library(self):
        """A row NAME collapses whitespace around `{library_name}`, which is right for one line and wrong
        for a summary someone typed over several."""
        plex = MagicMock(spec=PlexClient)
        collection = _collection()

        _apply(plex, collection, _spec(description="  Picked from {library_name}.\n\nNew every night.\n"))

        assert plex.edit_collection_fields.call_args.args[1] == {"summary": "Picked from Movies.\n\nNew every night."}

    def test_a_renamed_row_moves_its_sort_title_to_the_new_name(self):
        """A LOCKED sort title survives a rename (recorded), so Plex still holds prefix + the OLD name."""
        plex = MagicMock(spec=PlexClient)
        collection = _collection(title_sort="!010_✨ Old Name")

        written, _ = _apply(plex, collection, _spec(prefix="!010_"), WrittenDetails(title_sort="!010_✨ Old Name"))

        plex.edit_collection_fields.assert_called_once_with(collection, {"titleSort": "!010_✨ Hidden Gems"})
        assert written.title_sort == "!010_✨ Hidden Gems"

    def test_a_set_field_overwrites_a_value_someone_else_put_there(self):
        """The owner chose this value for this row; only CLEARING is careful about other people's."""
        plex = MagicMock(spec=PlexClient)
        collection = _collection(summary="set in agregarr")

        _apply(plex, collection, _spec(description="Picked nightly"))

        plex.edit_collection_fields.assert_called_once_with(collection, {"summary": "Picked nightly"})


class TestClearingAField:
    def test_a_cleared_field_hands_back_the_value_shortlist_wrote(self):
        plex = MagicMock(spec=PlexClient)
        collection = _collection(summary="Picked nightly", title_sort="!010_✨ Hidden Gems")
        plex.reread_collection.return_value = collection

        written, changes = _apply(
            plex,
            collection,
            _spec(),
            WrittenDetails(summary="Picked nightly", title_sort="!010_✨ Hidden Gems"),
        )

        # None = blank and unlock. No title is sent: rows are found by title, and re-sending a stale
        # one could undo a rename (pms_collection_field_edits.json).
        plex.edit_collection_fields.assert_called_once_with(collection, {"summary": None, "titleSort": None})
        assert written == WrittenDetails()
        assert changes == {"description": "cleared", "sort_title": "cleared"}

    def test_a_value_changed_since_shortlist_wrote_it_is_left_alone_and_forgotten(self):
        plex = MagicMock(spec=PlexClient)
        collection = _collection(summary="edited by hand in Plex", title_sort="!001_agregarr")
        plex.reread_collection.return_value = collection

        written, changes = _apply(
            plex,
            collection,
            _spec(),
            WrittenDetails(summary="Picked nightly", title_sort="!010_✨ Hidden Gems"),
        )

        plex.edit_collection_fields.assert_not_called()
        assert written == WrittenDetails(), "it is theirs now; a later clear must not claim it"
        assert changes == {}

    def test_a_clear_compares_with_what_plex_holds_now_not_the_runs_cached_listing(self):
        """The listing is read once at the top of a run, which can be hours before this row delivers.
        A value somebody set in the meantime is theirs, and the stale copy must not authorise wiping it."""
        plex = MagicMock(spec=PlexClient)
        cached = _collection(summary="Picked nightly")
        plex.reread_collection.return_value = _collection(summary="Set in agregarr at 03:30")

        written, changes = _apply(plex, cached, _spec(), WrittenDetails(summary="Picked nightly"))

        plex.reread_collection.assert_called_once_with(cached)
        plex.edit_collection_fields.assert_not_called()
        assert (written, changes) == (WrittenDetails(), {})

    def test_a_description_that_cannot_be_filled_for_this_person_is_a_cleared_one(self):
        """`{top_seed}` with nothing watched renders to nothing, exactly as a row name does (#84)."""
        plex = MagicMock(spec=PlexClient)
        collection = _collection(summary="Because you watched Fargo")
        plex.reread_collection.return_value = collection

        written, _ = _apply(
            plex,
            collection,
            _spec(description="Because you watched {top_seed}"),
            WrittenDetails(summary="Because you watched Fargo"),
            picks=[_pick(seed_title=None)],
        )

        plex.edit_collection_fields.assert_called_once_with(collection, {"summary": None})
        assert written.summary is None


class TestNeverFatal:
    def test_a_failed_write_keeps_the_old_record_and_does_not_raise(self):
        plex = MagicMock(spec=PlexClient)
        plex.edit_collection_fields.side_effect = RuntimeError("(500) internal_server_error")
        previous = WrittenDetails(summary="old")

        written, changes = _apply(plex, _collection(summary="old"), _spec(description="new"), previous)

        assert written == previous, "Plex still holds 'old', so a later clear must still recognise it"
        assert changes == {}

    def test_a_dry_run_reports_the_change_and_writes_nothing(self):
        plex = MagicMock(spec=PlexClient)
        previous = WrittenDetails()

        written, changes = _apply(plex, _collection(), _spec(description="new", prefix="!1_"), previous, dry_run=True)

        plex.edit_collection_fields.assert_not_called()
        assert written == previous
        assert changes == {"description": "set", "sort_title": "set"}

    def test_a_dry_run_of_a_row_that_does_not_exist_yet_still_reports(self):
        plex = MagicMock(spec=PlexClient)

        _, changes = _apply(plex, None, _spec(description="new"), dry_run=True)

        assert changes == {"description": "set"}


class TestEditCollectionFields:
    def test_a_value_is_locked_and_none_is_blanked_and_unlocked_in_one_put(self):
        client = PlexClient.__new__(PlexClient)
        collection = MagicMock()
        collection.title = DISPLAY

        client.edit_collection_fields(collection, {"summary": "Picked nightly", "titleSort": None})

        collection.edit.assert_called_once_with(
            **{"summary.value": "Picked nightly", "summary.locked": 1, "titleSort.value": "", "titleSort.locked": 0}
        )

    def test_the_runs_cached_collection_is_not_rewritten_in_memory(self):
        """Nothing may claim a value Plex does not hold: a blanked sort title is rebuilt by Plex from the
        title, not left blank. A retried delivery re-sends a SET (~0.01s) and reports it again; a retried
        CLEAR finds the field already handed back and reports nothing, which the ledger shrugs off and the
        first attempt's log line still records."""
        client = PlexClient.__new__(PlexClient)
        collection = SimpleNamespace(title=DISPLAY, summary="old", titleSort="!1_old", edit=MagicMock())

        client.edit_collection_fields(collection, {"summary": "new", "titleSort": None})

        assert (collection.summary, collection.titleSort) == ("old", "!1_old")


class TestDeliverRowsWiring:
    def _plex(self) -> tuple[MagicMock, SimpleNamespace, list[str]]:
        movies = SimpleNamespace(title="Movies", type="movie", key=1)
        plex = MagicMock(spec=PlexClient)
        plex.sections.return_value = [movies]
        plex.fetch_items.return_value = ([], [])
        plex.matches_section.return_value = True
        order: list[str] = []
        existing = MagicMock()
        existing.title = "✨ Hidden Gems"
        existing.summary = ""
        existing.titleSort = " Hidden Gems"
        existing.ratingKey = 42
        existing.items.return_value = []
        existing.labels = [SimpleNamespace(tag="Shortlist_sarah"), SimpleNamespace(tag="Shortlist")]
        plex.find_owned_collections.return_value = [existing]
        plex.stored_label.return_value = "Shortlist_sarah"
        plex.edit_collection_fields.side_effect = lambda *a, **k: order.append("details")
        return plex, movies, order

    def test_the_write_lands_after_the_first_row_exclude_and_is_recorded_for_the_ledger(
        self, engine_config: EngineConfig
    ):
        """After `on_label_stored`, so a person's first row is already hidden from everyone else before
        anything cosmetic is written to it (plex-safety rule 1)."""
        from shortlist.engine.delivery import row_marker

        plex, movies, order = self._plex()
        plex.find_owned_collections.return_value[0].title = DISPLAY + row_marker(make_profile().plex_account_id)
        breakdown: list[dict] = []
        spec = _spec(description="Picked nightly", prefix="!010_")

        deliver_rows(
            plex,
            make_profile(),
            [_pick()],
            engine_config,
            spec,
            sections=[movies],
            stored_labels={},
            breakdown=breakdown,
            written_details={"1": WrittenDetails()},
            on_label_stored=lambda: order.append("exclude"),
        )

        assert order == ["exclude", "details"]
        assert breakdown[0]["summary_written"] == "Picked nightly"
        assert breakdown[0]["title_sort_written"] == "!010_✨ Hidden Gems"
        assert breakdown[0]["details_changed"] == {"description": "set", "sort_title": "set"}
