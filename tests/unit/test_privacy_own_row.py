"""An account with an "allow only" list still sees its OWN rows (#115).

Plex hides everything an allow list does not name — a Shortlist row included, because a row carries
only its `shortlist_<slug>` label and no content rating. `admit_own_rows` adds that label as one more
alternative in each allow group, which a real server was measured to honour: the row shows on Home and
in the library, holding exactly the items the allow list admits
(`tests/fixtures/pms_share_filter_allow_lists.json`).

Checked with the independent evaluator in `test_privacy_filter_semantics.py`, under the grouping the
allow-list recording settled — `|` binds tighter than `&` — never with the parser under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine.models import UserType
from shortlist.engine.privacy import (
    AmbiguousFilterError,
    FilterCondition,
    admit_own_rows,
    clear_our_excludes,
    merge_label_excludes,
    plan_share_filter,
    serialize_filter,
    summarise_filter_diff,
    sync_user_restrictions,
)
from tests.conftest import make_profile, plextv_user
from tests.unit.test_privacy_filter_semantics import (
    visible_and_binds_tighter,
    visible_left_to_right,
    visible_or_binds_tighter,
)

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "pms_share_filter_allow_lists.json").read_text())
ME = "Shortlist_me"
OTHER = "Shortlist_other"
MY_ROW = {"label": {ME, "Shortlist"}}
OTHER_ROW = {"label": {OTHER, "Shortlist"}}
# One G-rated movie and one labelled `recommended`, standing for the 401 and 11 on the recording server.
ITEMS = {"g": {"contentRating": {"G"}}, "recommended": {"label": {"recommended"}, "contentRating": {"PG-13"}}}


class TestTheGroupingARealServerUses:
    """`test_privacy_filter_semantics` keeps two groupings alive because its recording could not tell
    them apart. This recording can, and the own-row write depends on the answer."""

    def _visible_count(self, model, raw: str) -> int:
        # 401 G-rated movies and 11 labelled `recommended` on the recording server.
        return 401 * model(raw, ITEMS["g"]) + 11 * model(raw, ITEMS["recommended"])

    def test_pipe_binding_tighter_reproduces_every_recorded_grouping_count(self):
        for case in FIXTURE["grouping"]:
            if "pipe_tighter_gives" in case:
                assert self._visible_count(visible_or_binds_tighter, case["filter"]) == case["movies_visible"]

    def test_the_recording_rules_out_left_to_right_and_and_binding_tighter(self):
        decisive = next(c for c in FIXTURE["grouping"] if c["left_to_right_would_give"] != c["movies_visible"])
        assert self._visible_count(visible_left_to_right, decisive["filter"]) != decisive["movies_visible"]
        assert self._visible_count(visible_and_binds_tighter, decisive["filter"]) != decisive["movies_visible"]

    def test_the_recorded_own_row_shapes_show_and_hide_the_row_as_measured(self):
        for case in FIXTURE["own_row"]["cases"]:
            if "rows_listed" not in case:
                continue
            raw = (
                case["filter_movies"]
                .replace("OURS-minus-ROW", f"label!={OTHER}")
                .replace("OURS", f"label!={OTHER},{ME}")
            )
            raw = raw.replace("ROW", ME)
            assert visible_or_binds_tighter(raw, MY_ROW) is (case["rows_listed"] > 0), raw


class TestAdmitOwnRows:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # A label allow list: the row's label joins the clause the owner already has.
            ("label=Kids&label!=Shortlist_other", "label=Kids,Shortlist_me&label!=Shortlist_other"),
            # A ratings allow list has no label clause, so the label is an alternative beside it.
            ("contentRating=G&label!=Shortlist_other", "contentRating=G|label=Shortlist_me&label!=Shortlist_other"),
            # Two allow groups ANDed together: BOTH need it, or the row stays hidden (recorded).
            (
                "contentRating=G&label=Kids&label!=Shortlist_other",
                "contentRating=G|label=Shortlist_me&label=Kids,Shortlist_me&label!=Shortlist_other",
            ),
            # One group holding both kinds of allow: one alternative is enough.
            (
                "label=Kids|contentRating=G&label!=Shortlist_other",
                "label=Kids,Shortlist_me|contentRating=G&label!=Shortlist_other",
            ),
            # Plex Web's encoded form keeps its own value separator.
            (
                "label=Kids%20Safe%2CFamily&label!=Shortlist_other",
                "label=Kids%20Safe%2CFamily%2CShortlist_me&label!=Shortlist_other",
            ),
        ],
    )
    def test_the_row_label_goes_into_every_allow_group(self, raw, expected):
        assert admit_own_rows(raw, ME, show=True) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "label!=Shortlist_other",  # no allow list: the row already shows
            "contentRating!=R&label!=Shortlist_other",  # an exclude is not an allow list
            "label=Kids,Shortlist_me&label!=Shortlist_other",  # already admitted
            "contentRating=G|label=Shortlist_me&label!=Shortlist_other",
            "label=Kids,shortlist_ME&label!=Shortlist_other",  # admitted, in another case
        ],
    )
    def test_a_filter_that_already_shows_the_row_is_returned_untouched(self, raw):
        assert admit_own_rows(raw, ME, show=True) is raw

    def test_an_owner_who_hid_every_shortlist_row_is_obeyed(self):
        """`label!=Shortlist` is how a co-managing tool opts out of all our rows (plex-safety rule 4). It
        is not an allow list, and nothing here may answer it with an alternative."""
        raw = "label=Kids&label!=Shortlist"
        assert visible_or_binds_tighter(admit_own_rows(raw, ME, show=True), MY_ROW) is False

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("label=Kids,Shortlist_me&label!=Shortlist_other", "label=Kids&label!=Shortlist_other"),
            ("contentRating=G|label=Shortlist_me&label!=Shortlist_other", "contentRating=G&label!=Shortlist_other"),
            ("label=Kids%2CShortlist_me", "label=Kids"),
        ],
    )
    def test_with_no_row_of_their_own_the_label_comes_back_out(self, raw, expected):
        assert admit_own_rows(raw, ME, show=False) == expected

    def test_every_other_allow_value_is_the_owners_and_stays(self):
        """Shortlist never wrote an allow value before #115, so any other one on a server is the owner's —
        a shared row or a sibling's row they chose to let this account see. Rule 3: left byte-identical,
        and the excludes still decide what shows."""
        assert admit_own_rows("label=Kids,Shortlist_other&label!=Shortlist_other", ME, show=True) == (
            "label=Kids,Shortlist_other,Shortlist_me&label!=Shortlist_other"
        )
        shared = "contentRating=G&label=Shortlist__shared_popular&label!=Shortlist_other"
        assert admit_own_rows(shared, ME, show=False) is shared

    def test_an_allow_list_the_owner_emptied_does_not_leave_only_our_label_behind(self):
        """Plex Web rewrites the whole filter from what its form shows. An owner who removes "Kids" but
        leaves our label in the list turns it into "allow only this person's rows" — the whole library
        gone. Our label never stands alone as an allow list."""
        assert admit_own_rows("label=Shortlist_me&label!=Shortlist_other", ME, show=True) == "label!=Shortlist_other"

    def test_a_raw_ampersand_in_a_label_is_refused_like_the_merge_refuses_it(self):
        with pytest.raises(AmbiguousFilterError):
            admit_own_rows("label=Kids & Family", ME, show=True)


# Owner filters: allow and exclude conditions on labels and ratings, both separators, no Shortlist label.
_value = {
    "label": st.sampled_from(["Kids", "Family", "Kids%20Safe"]),
    "contentRating": st.sampled_from(["G", "PG", "R"]),
}


@st.composite
def _owner_filter(draw) -> str:
    conditions = []
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        field = draw(st.sampled_from(["label", "contentRating"]))
        values = tuple(draw(st.lists(_value[field], min_size=1, max_size=2, unique=True)))
        conditions.append(
            FilterCondition(
                field,
                draw(st.sampled_from(["=", "!="])),
                values,
                sep=draw(st.sampled_from(["|", "&"])),
                value_seps=(draw(st.sampled_from([",", "%2C"])),) * (len(values) - 1),
            )
        )
    return serialize_filter(conditions)


_item = st.builds(
    lambda labels, rating: {"label": labels, "contentRating": {rating} if rating else set()},
    st.sets(st.sampled_from(["Kids", "Family", "Kids Safe"]), max_size=2),
    st.sampled_from(["", "G", "PG", "R"]),
)


class TestTheWholeWriteKeepsEveryPromise:
    """Through `plan_share_filter`, the one function the nightly sync writes with — including filters the
    pre-#116 merge left behind, with our exclude joined to the owner's conditions by `|`."""

    def _write(self, raw: str) -> str:
        return plan_share_filter(raw, {OTHER}, own_row_label=ME, show=True)

    @given(_owner_filter(), st.sampled_from(["none", "after", "before"]), _item)
    def test_everything_that_is_not_a_row_is_exactly_what_the_owner_allowed(self, raw, damage, item):
        assert visible_or_binds_tighter(self._write(_damaged(raw, damage)), item) is visible_or_binds_tighter(raw, item)

    @given(_owner_filter(), st.sampled_from(["none", "after", "before"]))
    def test_their_own_row_shows_and_nobody_elses_does_after_one_write(self, raw, damage):
        written = self._write(_damaged(raw, damage))
        assert visible_or_binds_tighter(written, MY_ROW) is True, written
        assert visible_or_binds_tighter(written, OTHER_ROW) is False, written

    @given(_owner_filter(), st.sampled_from(["none", "after", "before"]))
    def test_a_second_night_writes_nothing(self, raw, damage):
        once = self._write(_damaged(raw, damage))
        assert self._write(once) == once

    @given(_owner_filter())
    def test_taking_the_row_away_hands_back_the_owners_filter_byte_for_byte(self, raw):
        merged = merge_label_excludes(raw, {OTHER})
        assert admit_own_rows(admit_own_rows(merged, ME, show=True), ME, show=False) == merged


def _damaged(raw: str, damage: str) -> str:
    """What the pre-#116 merge left on accounts: our exclude joined to the owner's filter by `|`."""
    if damage == "none" or not raw:
        return raw
    return f"{raw}|label!={OTHER}" if damage == "after" else f"label!={OTHER}|{raw}"


class TestTheNightlySync:
    def test_an_allow_list_account_with_a_row_is_written_its_row_label(self, mock_plextv, snapshot_store):
        kid = make_profile("me", user_type=UserType.MANAGED, account_id=400)
        mock_plextv.users = [
            plextv_user(400, "me", filters={"filterMovies": "label=Kids", "filterTelevision": "contentRating=TV-Y"})
        ]
        mock_plextv.update_user_filters.side_effect = lambda _id, fields: mock_plextv.users[0].filters.update(fields)
        stored = {"me": ME, "other": OTHER}

        sync_user_restrictions(mock_plextv, kid, mock_plextv.get_user(400), stored, snapshot_store, own_label=ME)

        assert mock_plextv.update_user_filters.call_args.args == (
            400,
            {
                "filterMovies": "label=Kids,Shortlist_me&label!=Shortlist_other",
                "filterTelevision": "contentRating=TV-Y|label=Shortlist_me&label!=Shortlist_other",
            },
        )
        again = sync_user_restrictions(
            mock_plextv, kid, mock_plextv.get_user(400), stored, snapshot_store, own_label=ME
        )
        assert again is None

    def test_an_owner_exclude_before_the_allow_list_still_settles_in_one_night(self, mock_plextv, snapshot_store):
        """The order inside the sync. Merged first, `Shortlist_other` joins the owner's `label!=Kids` clause
        and the own-row `|` then lands behind it, so the next night's merge — which trusts no exclude a `|`
        follows — moves it again, and again. Admitting first leaves the merge a filter it can settle."""
        kid = make_profile("me", user_type=UserType.MANAGED, account_id=400)
        mock_plextv.users = [plextv_user(400, "me", filters={"filterMovies": "label!=Kids&contentRating=G"})]
        mock_plextv.update_user_filters.side_effect = lambda _id, fields: mock_plextv.users[0].filters.update(fields)
        stored = {"me": ME, "other": OTHER}

        sync_user_restrictions(mock_plextv, kid, mock_plextv.get_user(400), stored, snapshot_store, own_label=ME)
        settled = mock_plextv.users[0].filters["filterMovies"]
        again = sync_user_restrictions(
            mock_plextv, kid, mock_plextv.get_user(400), stored, snapshot_store, own_label=ME
        )

        assert again is None, f"a second night rewrote {settled!r}"
        assert visible_or_binds_tighter(settled, MY_ROW) is True
        assert visible_or_binds_tighter(settled, OTHER_ROW) is False

    def test_an_account_with_no_row_gets_no_allow_label(self, mock_plextv, snapshot_store):
        kid = make_profile("me", user_type=UserType.MANAGED, account_id=400)
        mock_plextv.users = [plextv_user(400, "me", filters={"filterMovies": "label=Kids"})]

        sync_user_restrictions(mock_plextv, kid, mock_plextv.get_user(400), {"other": OTHER}, snapshot_store)

        assert mock_plextv.update_user_filters.call_args.args[1]["filterMovies"] == "label=Kids&label!=Shortlist_other"

    def test_a_collections_read_that_missed_their_row_leaves_the_allow_label_alone(self, mock_plextv, snapshot_store):
        """No label for them in an enumeration we cannot vouch for is not evidence the row is gone. Taking
        the allow label out would hide their row until the next pass put it back."""
        kid = make_profile("me", user_type=UserType.MANAGED, account_id=400)
        allowed = "label=Kids,Shortlist_me&label!=Shortlist_other"
        mock_plextv.users = [plextv_user(400, "me", filters={"filterMovies": allowed, "filterTelevision": allowed})]

        wrote = sync_user_restrictions(
            mock_plextv, kid, mock_plextv.get_user(400), {"other": OTHER}, snapshot_store, own_row_label=ME
        )

        assert wrote is None

    def test_a_row_that_is_really_gone_takes_the_allow_label_with_it(self, mock_plextv, snapshot_store):
        kid = make_profile("me", user_type=UserType.MANAGED, account_id=400)
        allowed = "label=Kids,Shortlist_me&label!=Shortlist_other"
        mock_plextv.users = [plextv_user(400, "me", filters={"filterMovies": allowed})]

        wrote = sync_user_restrictions(
            mock_plextv,
            kid,
            mock_plextv.get_user(400),
            {"other": OTHER},
            snapshot_store,
            own_row_label=ME,
            collections_known=True,
        )

        assert wrote["filterMovies"][1] == "label=Kids&label!=Shortlist_other"

    def test_a_lone_own_label_is_not_dropped_before_an_old_pipe_exclude_beside_it_is_repaired(self):
        """Found by review 2026-09-13. Dropping `label=Shortlist_Me` out of its group first left
        `label!=shortlist_a|label!=Foo` behind, where an item labelled Foo passes the first alternative:
        the owner's Foo exclude switched off. The drop waits for the merge to lift our exclude."""
        raw = "label=Kids,shortlist_me|label=Fam&label=Shortlist_Me|label!=shortlist_a|label!=Foo,shortlist_b"
        foo = {"label": {"Foo", "Kids"}}

        planned = plan_share_filter(raw, {"shortlist_a", "shortlist_b"}, own_row_label=ME, show=True)

        assert visible_or_binds_tighter(planned, foo) is visible_or_binds_tighter(
            merge_label_excludes(raw, {"shortlist_a", "shortlist_b"}), foo
        ), planned
        assert visible_or_binds_tighter(planned, MY_ROW) is True
        assert plan_share_filter(planned, {"shortlist_a", "shortlist_b"}, own_row_label=ME, show=True) == planned

    def test_the_audit_line_says_the_row_was_let_through(self):
        diff = {"filterMovies": ("label=Kids&label!=Shortlist_other", "label=Kids,Shortlist_me&label!=Shortlist_other")}
        assert "own row allowed" in summarise_filter_diff(diff, "shortlist")


class TestLeavingSharingAlone:
    def test_an_allow_list_is_left_exactly_as_it_is(self, mock_plextv):
        """ "Leave this account alone": only our excludes come out. An allow value — even the account's own
        row label — may be one the owner typed (Shortlist wrote none before #115), and keeping it hides
        nothing from anyone: it only lets that person see their own row. Rule 3 wins."""
        kid = make_profile("me", user_type=UserType.MANAGED, account_id=400)
        remote = plextv_user(
            400,
            "me",
            filters={"filterMovies": "label=Kids,Shortlist__shared_popular,Shortlist_me&label!=Shortlist_other"},
        )

        written = clear_our_excludes(mock_plextv, kid, remote)

        assert written["filterMovies"][1] == "label=Kids,Shortlist__shared_popular,Shortlist_me"

    def test_clear_takes_no_own_label_to_remove(self):
        """Pinned at the signature: nothing can hand the left-alone pass a label to strip."""
        import inspect

        assert "own_row_label" not in inspect.signature(clear_our_excludes).parameters
