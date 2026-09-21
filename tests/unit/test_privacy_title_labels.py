"""The owner's own admit / hide labels in a share filter (`privacy.plan_title_labels`).

Ratings say "safe"; a label says "for this person". An ADMIT label lets a hand-picked title through an
allow list that would otherwise hide it; a HIDE label keeps one out though the list admits it.

Checked with the independent evaluator in `test_privacy_filter_semantics.py`, under the grouping a real
server was measured to use (`|` binds tighter than `&`) — never with the parser under test. That an
OR'd label admits TITLES and not only collections is itself recorded: `contentRating=G|label=recommended`
showed 412 movies, the 401 rated G plus the 11 labelled (`pms_share_filter_allow_lists.json`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine.models import TitleLabels
from shortlist.engine.privacy import (
    AmbiguousFilterError,
    plan_account_filter,
    plan_share_filter,
    plan_title_labels,
)
from tests.unit.test_privacy_filter_semantics import _conditions, _holds, visible_or_binds_tighter
from tests.unit.test_privacy_own_row import _damaged, _item, _owner_filter

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "pms_share_filter_allow_lists.json").read_text())
ME, OTHER = "Shortlist_me", "Shortlist_other"
ADMIT, HIDE = "Hand Picked", "Not For Kids"
MY_ROW = {"label": {ME, "Shortlist"}}
OTHER_ROW = {"label": {OTHER, "Shortlist"}}


def rows_only(raw: str) -> str:
    """What the nightly sync writes for an account with NO title labels."""
    return plan_share_filter(raw, {OTHER}, own_row_label=ME, show=True)


def plan(raw: str, *, admit=(ADMIT,), hide=(HIDE,), ledger=((), ())):
    """The whole write for one field, through the one function `sync_user_restrictions` plans with.

    Returns ``(filter, ledger)``. A ledger is given, and comes back, as ``(admit, hide)`` when that is
    all there is to it, and as the stored dict when a `hide_copy` is involved — so the common case
    reads at a glance and the rare one is still exact."""
    given = ledger if isinstance(ledger, dict) else {"admit": ledger[0], "hide": ledger[1]}
    planned, wrote = plan_account_filter(
        raw, {OTHER}, own_row_label=ME, show=True, admit=admit, hide=hide, written=given
    )
    if "hide_copy" in wrote:
        return planned, wrote
    return planned, (wrote.get("admit", ()), wrote.get("hide", ()))


def write(raw: str, **kw) -> str:
    return plan(raw, **kw)[0]


def with_labels(item: dict, *labels: str) -> dict:
    return {**item, "label": {*item.get("label", set()), *labels}}


def past_the_allow_lists(raw: str, item: dict) -> bool:
    """Whether the owner's filter shows `item` once every group holding an ALLOW condition is taken as
    satisfied — what an admit label is for. Groups with no allow condition still apply in full.

    Deliberately the code's own rule, stated once more: what it cannot check is whether that rule is
    the right one. `TestWhereAnAdmitLabelDoesReach` pins the cases where it is arguable, by hand."""
    groups: list[list] = []
    for condition in _conditions(raw):
        if condition[0] == "&" or not groups:
            groups.append([condition])
        else:
            groups[-1].append(condition)
    return all(any(c[2] == "=" for c in group) or any(_holds(c, item) for c in group) for group in groups)


# Owner filters that may ALREADY carry the very labels being configured — typed into Plex by hand, as
# an allow value or an exclude, in either encoding and any case. The first version of this feature
# never generated one, and so never saw that it treated them as its own and deleted them.
_value_with_ours = {
    "label": st.sampled_from(["Kids", "Family", "Hand Picked", "Hand%20Picked", "hand picked", "Not%20For%20Kids"]),
    "contentRating": st.sampled_from(["G", "PG", "R"]),
}


@st.composite
def _any_owner_filter(draw) -> str:
    from shortlist.engine.privacy import FilterCondition, serialize_filter

    conditions = []
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        field = draw(st.sampled_from(["label", "contentRating"]))
        values = tuple(draw(st.lists(_value_with_ours[field], min_size=1, max_size=2, unique=True)))
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


class TestWhatARealServerRecorded:
    def test_an_ord_label_admits_titles_not_only_collections(self):
        """The premise of the whole feature, read off the recording rather than assumed."""
        case = next(c for c in FIXTURE["grouping"] if c["filter"] == "contentRating=G|label=recommended&OURS")

        assert case["movies_visible"] == 401 + 11


class TestWhatARealServerDidWithTheLabelsShortlistWrote:
    """`pms_share_filter_title_labels.json`: the filters py.10's own privacy pass wrote for a child's
    profile, and what that profile could then open. The TV half was only expected until this was
    recorded — a label on a SHOW carries every one of its episodes, in both directions."""

    RECORDED = json.loads((Path(__file__).parents[1] / "fixtures" / "pms_share_filter_title_labels.json").read_text())

    @pytest.mark.parametrize(
        "kind, field", [("movies_as_account", "filterMovies"), ("shows_as_account", "filterTelevision")]
    )
    def test_the_evaluator_these_tests_trust_agrees_with_the_server_title_by_title(self, kind, field):
        raw = self.RECORDED["filters_as_written"][field].replace("<12 Shortlist_ values>", "Shortlist_someone")
        for title in self.RECORDED[kind]:
            item = {"contentRating": {title["rating"]} if title["rating"] else set()}
            item = with_labels(item, title["label"]) if title["label"] else item
            opened = title.get("by_key", title.get("show")) == 200

            assert visible_or_binds_tighter(raw, item) is opened, title["title"]

    def test_a_labelled_show_takes_all_its_episodes_with_it_both_ways(self):
        for show in self.RECORDED["shows_as_account"]:
            if show["label"] == "KidsAllow":
                assert (show["allLeaves"], show["episodes_seen"], show["episode_by_key"]) == (
                    200,
                    show["episodes"],
                    200,
                )
            if show["label"] == "KidsDeny":
                assert (show["show"], show["allLeaves"], show["episodes_seen"], show["episode_by_key"]) == (
                    404,
                    404,
                    0,
                    404,
                )

    def test_the_planner_writes_that_filter_from_the_one_the_profile_had(self):
        had = "contentRating=TV-Y%2CTV-Y7%2CTV-Y7-FV|label=Shortlist_me&label!=Shortlist_other"

        planned, ledger = plan(had, admit=("KidsAllow",), hide=("KidsDeny",))

        assert (
            planned
            == "contentRating=TV-Y%2CTV-Y7%2CTV-Y7-FV|label=Shortlist_me%2CKidsAllow&label!=Shortlist_other%2CKidsDeny"
        )
        assert ledger == (("KidsAllow",), ("KidsDeny",))


class TestTheShapePavelAskedFor:
    """A child's profile restricted by rating: Planet Earth II (TV-G, outside the list) let in by hand,
    2001: A Space Odyssey (G, inside it) kept out by hand."""

    KIDS_TV = "contentRating=TV-Y%2CTV-Y7%2CTV-Y7-FV"
    KIDS_MOVIES = "contentRating=G%2CPG"

    def test_the_written_filter_reads_the_way_it_was_meant_and_is_recorded_as_ours(self):
        planned, ledger = plan(self.KIDS_TV)

        assert planned == (
            "contentRating=TV-Y%2CTV-Y7%2CTV-Y7-FV|label=Shortlist_me%2CHand%20Picked"
            "&label!=Shortlist_other%2CNot%20For%20Kids"
        )
        assert ledger == ((ADMIT,), (HIDE,))

    def test_a_labelled_title_outside_the_ratings_gets_in(self):
        planet_earth = {"contentRating": {"TV-G"}, "label": {ADMIT}}

        assert visible_or_binds_tighter(rows_only(self.KIDS_TV), planet_earth) is False
        assert visible_or_binds_tighter(write(self.KIDS_TV), planet_earth) is True

    def test_a_labelled_title_inside_the_ratings_is_kept_out(self):
        space_odyssey = {"contentRating": {"G"}, "label": {HIDE}}

        assert visible_or_binds_tighter(rows_only(self.KIDS_MOVIES), space_odyssey) is True
        assert visible_or_binds_tighter(write(self.KIDS_MOVIES), space_odyssey) is False

    def test_hide_beats_admit_on_a_title_carrying_both(self):
        both = {"contentRating": {"TV-G"}, "label": {ADMIT, HIDE}}

        assert visible_or_binds_tighter(write(self.KIDS_TV), both) is False

    def test_an_account_with_no_allow_list_is_given_no_allow_list_and_nothing_is_recorded(self):
        """Adults: nothing is hidden by rating, so there is nothing to admit a title INTO. Writing
        `label=Hand Picked` there would CREATE an allow list — the whole library hidden but for the
        labelled titles."""
        planned, ledger = plan("", hide=())

        assert "label=" not in planned.replace("label!=", "")
        assert visible_or_binds_tighter(planned, {"contentRating": {"R"}}) is True
        assert ledger == ((), ())

    def test_a_label_plex_cannot_read_is_refused_not_written_around(self):
        # Admit-only on purpose: with a hide label the exclude merge would refuse it anyway, and this
        # planner's own check could be deleted without any test noticing.
        with pytest.raises(AmbiguousFilterError):
            plan_title_labels("label=Rock&Roll", admit=(ADMIT,), own_row_label=ME)

    def test_no_labels_and_no_ledger_is_the_same_object_back(self):
        raw = rows_only(self.KIDS_TV)

        assert plan_title_labels(raw, own_row_label=ME)[0] is raw


class TestALabelTheOwnerTypedIsTheOwners:
    """The ledger is the only thing that tells a label Shortlist wrote from one the owner typed into
    Plex — no prefix marks these, they are the owner's own words. `label=Kids` is the commonest allow
    list there is. Inferring "ours" from the settings deleted it, and showed a child the whole library."""

    def test_naming_a_label_the_allow_list_already_is_changes_nothing_and_claims_nothing(self):
        planned, ledger = plan("label=Kids", admit=("Kids",), hide=())

        assert planned == rows_only("label=Kids")
        assert ledger == ((), ())
        assert visible_or_binds_tighter(planned, {"contentRating": {"R"}}) is False  # still an allow list

    def test_an_and_between_their_label_and_their_ratings_stays_an_and(self):
        """`label=Kids&contentRating=G,PG` — Kids AND G/PG. Treated as ours, the label was folded into
        the ratings group as an alternative, and the AND became an OR."""
        raw = "label=Kids&contentRating=G,PG"

        planned = write(raw, admit=("Kids",), hide=())

        assert visible_or_binds_tighter(planned, {"contentRating": {"G"}}) is False
        assert visible_or_binds_tighter(planned, {"contentRating": {"G"}, "label": {"Kids"}}) is True

    @pytest.mark.parametrize("theirs", ["label=Kids", "label=kids", "label=Kids,Family", "contentRating=G|label=Kids"])
    def test_taking_the_setting_away_never_takes_their_label_away(self, theirs):
        _, ledger = plan(theirs, admit=("Kids",), hide=())

        assert write(theirs, admit=(), hide=(), ledger=ledger) == rows_only(theirs)

    @pytest.mark.parametrize("theirs", ["contentRating=G&label!=Scary", "contentRating=G|label!=Scary"])
    def test_nor_an_exclude_they_wrote_whether_or_not_plex_applies_it_where_it_sits(self, theirs):
        """The second is the plexapi shape: their exclude sits behind a `|`, where Plex ignores it.
        Shortlist adds ONE copy where Plex applies it and records it as a copy beside theirs — so
        retiring removes the copy it added and leaves theirs exactly where they put it."""
        planned, ledger = plan(theirs, admit=(), hide=("Scary",))
        assert visible_or_binds_tighter(planned, {"contentRating": {"G"}, "label": {"Scary"}}) is False

        assert write(planned, admit=(), hide=(), ledger=ledger) == rows_only(theirs)

    def test_a_copy_beside_theirs_is_recorded_as_one(self):
        _, ledger = plan("contentRating=G|label!=Scary", admit=(), hide=("Scary",))

        assert ledger == {"hide_copy": ("Scary",)}

    def test_a_hide_label_with_no_copy_anywhere_is_wholly_shortlists_wherever_the_merge_put_it(self):
        """The merge writes into the first ENFORCED `label!=` clause — here the owner's own leading one.
        The night that person gets a row, an own-row `|` appears later, that clause stops counting as
        enforced, and the merge writes a second copy at the end. Retiring only the enforced copy left
        the first in the owner's clause for ever, hiding titles no setting explained."""
        theirs = "label!=Family&contentRating=G"
        no_row = plan_account_filter(theirs, {OTHER}, own_row_label=None, show=False, hide=("Kids",))
        assert no_row[0] == "label!=Family,Shortlist_other,Kids&contentRating=G"

        with_row, ledger = plan(no_row[0], admit=(), hide=("Kids",), ledger=no_row[1])
        assert with_row.count("Kids") == 2  # the merge's second copy, where Plex now applies it
        retired = write(with_row, admit=(), hide=(), ledger=ledger)

        assert "Kids" not in retired
        assert retired == rows_only(theirs)

    def test_retiring_a_copy_takes_back_ONE_copy_even_when_theirs_has_become_enforced_too(self):
        """Their `label!=Scary&contentRating=G` is unenforced only while OUR own-row `|` follows it. The
        night that person's row goes, both copies sit where Plex applies them — and "remove it from
        every enforced clause" then deleted the one the owner typed, showing a Scary G title their
        original filter hid. Found by review, 2026-09-21."""
        theirs = "label!=Scary&contentRating=G"
        beside, ledger = plan(theirs, admit=(), hide=("Scary",))
        assert ledger == {"hide_copy": ("Scary",)}
        row_gone, ledger = plan_account_filter(
            beside, {OTHER}, own_row_label=ME, show=False, hide=("Scary",), written=ledger
        )
        assert row_gone == "label!=Scary&contentRating=G&label!=Shortlist_other,Scary"

        retired, ledger = plan_account_filter(row_gone, {OTHER}, own_row_label=ME, show=False, written=ledger)

        assert retired == "label!=Scary&contentRating=G&label!=Shortlist_other"
        assert ledger == {}
        assert visible_or_binds_tighter(retired, {"contentRating": {"G"}, "label": {"Scary"}}) is False

    def test_nor_when_the_row_goes_and_the_label_is_unticked_the_same_night(self):
        beside, ledger = plan("label!=Scary&contentRating=G", admit=(), hide=("Scary",))

        retired, _ = plan_account_filter(beside, {OTHER}, own_row_label=ME, show=False, written=ledger)

        assert retired == "label!=Scary&contentRating=G&label!=Shortlist_other"

    def test_a_copy_folded_into_theirs_by_a_save_in_plex_is_theirs(self):
        """Plex's own form rewrites a restriction from what it shows — each label once, excludes after
        allows — so a save there can fold their `label!=Scary` and ours into one clause. Un-ticking
        the label then took back "our" copy: the only exclusion left, which they had before Shortlist.
        One copy is never taken; it stops being listed. Found by review, 2026-09-21."""
        _, ledger = plan("label!=Scary&contentRating=G", admit=(), hide=("Scary",))
        assert ledger == {"hide_copy": ("Scary",)}
        resaved = "contentRating=G&label=Shortlist_me&label!=Scary"

        healed, ledger = plan(resaved, admit=(), hide=("Scary",), ledger=ledger)
        retired, ledger = plan(healed, admit=(), hide=(), ledger=ledger)

        assert visible_or_binds_tighter(retired, {"contentRating": {"G"}, "label": {"Scary"}}) is False, retired
        assert ledger == ((), ())

    def test_a_stale_ledger_entry_is_dropped_not_acted_on(self):
        """A save in Plex Web wipes what Shortlist wrote. The record of having written it must not
        outlive the writing: nothing is there to remove, so nothing is removed and the entry goes."""
        planned, ledger = plan("contentRating=G", admit=(), hide=(), ledger=((ADMIT,), (HIDE,)))

        assert planned == rows_only("contentRating=G")
        assert ledger == ((), ())


class TestEveryPromiseOverAnyOwnerFilter:
    """Hypothesis writes the owner's filter — allow and exclude conditions, both separators, both
    encodings, with and without the damage the pre-#116 merge left behind, and with or without the
    configured labels already in it by the owner's own hand."""

    @given(_any_owner_filter(), st.sampled_from(["none", "after", "before"]), _item)
    def test_a_title_carrying_neither_label_is_exactly_what_the_owner_allowed(self, raw, damage, item):
        assert visible_or_binds_tighter(write(_damaged(raw, damage)), item) is visible_or_binds_tighter(raw, item)

    @given(_any_owner_filter(), st.sampled_from(["none", "after", "before"]), _item)
    def test_a_hide_labelled_title_is_never_visible(self, raw, damage, item):
        assert visible_or_binds_tighter(write(_damaged(raw, damage)), with_labels(item, HIDE)) is False

    @given(_owner_filter(), st.sampled_from(["none", "after", "before"]), _item)
    def test_an_admit_labelled_title_gets_past_the_allow_lists_and_nothing_else(self, raw, damage, item):
        """Past the ALLOW lists only — see `past_the_allow_lists`. Over filters that do not already
        mention the label: where the owner has arranged it themselves, theirs is the arrangement."""
        labelled = with_labels(item, ADMIT)
        written = write(_damaged(raw, damage), hide=())

        assert visible_or_binds_tighter(written, labelled) is past_the_allow_lists(raw, labelled), written

    @given(_any_owner_filter(), st.sampled_from(["none", "after", "before"]))
    def test_their_own_row_still_shows_and_nobody_elses_does(self, raw, damage):
        written = write(_damaged(raw, damage))

        assert visible_or_binds_tighter(written, MY_ROW) is True, written
        assert visible_or_binds_tighter(written, OTHER_ROW) is False, written

    @given(_any_owner_filter(), st.sampled_from(["none", "after", "before"]))
    def test_a_second_night_writes_nothing_and_records_nothing_new(self, raw, damage):
        once, ledger = plan(_damaged(raw, damage))

        assert plan(once, ledger=ledger) == (once, ledger)

    @given(_any_owner_filter())
    def test_taking_the_labels_away_hands_back_exactly_what_there_was_without_them(self, raw):
        """Byte for byte, whatever the owner's filter already held — INCLUDING these very labels."""
        labelled, ledger = plan(raw)

        assert plan(labelled, admit=(), hide=(), ledger=ledger) == (rows_only(raw), ((), ()))

    @given(_any_owner_filter())
    def test_a_label_never_written_is_never_removed(self, raw):
        """No ledger, no removal — the whole of rule 3 in one line."""
        assert write(raw, admit=(), hide=()) == rows_only(raw)


class TestWhereAnAdmitLabelDoesReach:
    """Pinned by hand, because `past_the_allow_lists` IS the code's rule and cannot argue with it."""

    def test_an_exclude_anded_with_the_allow_list_still_holds(self):
        """Plex Web's form: `contentRating=G&contentRating!=R`. The exclude is its own group."""
        written = write("contentRating=G,PG&contentRating!=R", hide=())

        assert visible_or_binds_tighter(written, {"contentRating": {"R"}, "label": {ADMIT}}) is False

    def test_an_exclude_ored_INTO_an_allow_group_is_part_of_that_group(self):
        """`contentRating!=R|label=Kids` reads "not R, or labelled Kids" — one allow group. An admit
        label is one more alternative in it, so a labelled R title shows. Documented, not hidden: the
        docs say an admit label gets past "every allow list, including an exclusion joined to one
        with OR"."""
        written = write("contentRating!=R|label=Kids", hide=())

        assert visible_or_binds_tighter(written, {"contentRating": {"R"}, "label": {ADMIT}}) is True
        assert visible_or_binds_tighter(written, {"contentRating": {"R"}}) is False  # and nobody else


class TestTheLastAllowListIsNeverRemoved:
    """THE INVARIANT: planning title labels never takes a field from having an allow list to none.

    `label=Shortlist_me,Hand Picked` with the label in the ledger is what Plex Web leaves when an owner
    clears the ratings and keeps the labels its form shows. It is ALSO, character for character, an
    owner saying "allow only Hand Picked". The first version of this took the label out as left-behind
    litter; the row planner then removed its own label from the now-empty group; and the child saw the
    whole library. It fails closed now: shown only the labelled titles, which the owner can see and fix."""

    ONLY_OURS = "label=Shortlist_me%2CHand%20Picked&label!=Shortlist_other"

    def test_a_written_label_that_is_the_only_allow_list_left_is_left_and_handed_to_the_owner(self):
        planned, ledger = plan(self.ONLY_OURS, hide=(), ledger=((ADMIT,), ()))

        assert planned == self.ONLY_OURS
        assert ledger == ((), ())  # the owner's from now on: never removed, whatever the settings say
        assert visible_or_binds_tighter(planned, {"contentRating": {"R"}}) is False

    def test_nor_when_the_setting_is_taken_away_the_same_night(self):
        planned, ledger = plan(self.ONLY_OURS, admit=(), hide=(), ledger=((ADMIT,), ()))

        assert planned == self.ONLY_OURS and ledger == ((), ())

    def test_a_group_emptied_beside_one_that_remains_is_still_healed(self):
        """What the emptied-group rule is FOR: Plex Web saved the label as its own ANDed group, which
        hides every rated title that is not also labelled. The ratings remain, so the label goes back
        to being an alternative inside them — in one write."""
        anded = "contentRating=G%2CPG&label=Shortlist_me%2CHand%20Picked&label!=Shortlist_other"

        planned, ledger = plan(anded, hide=(), ledger=((ADMIT,), ()))

        assert planned == "contentRating=G%2CPG|label=Shortlist_me%2CHand%20Picked&label!=Shortlist_other"
        assert ledger == ((ADMIT,), ())

    @pytest.mark.parametrize(
        "raw",
        [
            "label=Shortlist_me%2CHand%20Picked",
            "label=Shortlist_me,Hand Picked&label!=Shortlist_other",
            "label=Shortlist_me%2CHand%20Picked&label=Shortlist_me%2CFamily",
        ],
    )
    def test_nor_across_two_nights_when_the_first_could_not_name_their_row_label(self, raw):
        """`own_row_label` is None on a pass that could not enumerate the server's collections. Setting
        aside only THAT label, `label=Shortlist_me,Hand Picked` still looked restricted with the title
        label gone — and the next night, its row label known again, the row planner took
        `Shortlist_me` out too. Any `shortlist_` label is set aside now. Found by review, 2026-09-21."""
        written = {"admit": (ADMIT, "Family")}
        blind, ledger = plan_account_filter(raw, {OTHER}, own_row_label=None, show=True, written=written)

        planned, _ = plan_account_filter(blind, {OTHER}, own_row_label=ME, show=True, written=ledger)

        assert visible_or_binds_tighter(planned, {"contentRating": {"R"}}) is False, planned

    @pytest.mark.parametrize("theirs", ["label=Shortlist__shared_x", "label=Shortlist_sibling"])
    def test_an_allow_list_of_somebody_elses_row_is_an_allow_list_and_a_label_beside_it_comes_back_out(self, theirs):
        """The row planner removes THIS account's row label and no other, so such a list stays — and
        counting it as nothing made the invariant trip on every retire: the label was handed to the
        owner, admitting its titles for good with no setting on screen. Found by review, 2026-09-21."""
        planned, ledger = plan(theirs, admit=("Docs",), hide=())
        assert "Docs" in planned and ledger == (("Docs",), ())

        retired, ledger = plan(planned, admit=(), hide=(), ledger=ledger)

        assert retired == rows_only(theirs) and "Docs" not in retired
        assert ledger == ((), ())

    @given(_any_owner_filter(), st.booleans(), st.booleans(), st.booleans())
    def test_over_any_filter_any_ledger_and_any_setting(self, raw, configured, listed_admit, listed_hide):
        """The property itself. `rows_only` is the baseline: whatever allow list the row planner alone
        would leave, the title planner leaves one too."""

        def restricts(planned: str) -> bool:
            return any(c[2] == "=" and c[3] != {ME.casefold()} for c in _conditions(planned))

        ledger = ((ADMIT,) if listed_admit else (), (HIDE,) if listed_hide else ())
        admit, hide = ((ADMIT,), (HIDE,)) if configured else ((), ())

        planned = write(raw, admit=admit, hide=hide, ledger=ledger)

        assert restricts(planned) or not restricts(rows_only(raw)), planned


class TestLabelsThatDifferOnlyInSpelling:
    def test_a_live_label_and_a_recorded_one_that_differ_only_in_case_are_one_label(self):
        """Plex matches tags caselessly, and Plex Web percent-encodes. Read as two labels, the recorded
        one would be retired and the configured one added beside it, every night."""
        settled = "contentRating=G|label=Shortlist_me%2CHand%20Picked&label!=Shortlist_other"

        planned, ledger = plan(settled, admit=("hand picked",), hide=(), ledger=(("Hand Picked",), ()))

        assert planned == settled
        assert ledger == (("hand picked",), ())


class _Ledger:
    """The record `DbTitleLabelLedger` keeps, in memory — and in what ORDER it was written."""

    def __init__(self, order: list[str]):
        self.saved: dict[int, dict] = {}
        self._order = order

    def save(self, plex_account_id: int, written: dict) -> None:
        self._order.append("ledger")
        self.saved[plex_account_id] = written

    def labels(self, admit=(), hide=()) -> TitleLabels:
        written = {
            field: {kind: tuple(values) for kind, values in kinds.items()}
            for field, kinds in self.saved.get(400, {}).items()
        }
        return TitleLabels(admit=tuple(admit), hide=tuple(hide), written=written)


class TestTheNightlySync:
    """Through `sync_user_restrictions`, the labels and the ledger handed in BY ACCOUNT — the way
    `pipeline._privacy_sync_phase` does for a whole audience of stub profiles."""

    KIDS = {  # noqa: RUF012 — a literal, never mutated
        "filterMovies": "contentRating=G%2CPG",
        "filterTelevision": "contentRating=TV-Y%2CTV-Y7%2CTV-Y7-FV",
    }

    @pytest.fixture
    def account(self, mock_plextv):
        from tests.conftest import plextv_user

        order: list[str] = []
        mock_plextv.users = [plextv_user(400, "me", filters=dict(self.KIDS))]

        def _write(_id, fields):
            order.append("plex")
            mock_plextv.users[0].filters.update(fields)

        mock_plextv.update_user_filters.side_effect = _write
        return _Ledger(order), order

    def _sync(self, mock_plextv, snapshot_store, ledger, *, admit=(), hide=(), **kw):
        from shortlist.engine.models import UserType
        from shortlist.engine.privacy import sync_user_restrictions
        from tests.conftest import make_profile

        return sync_user_restrictions(
            mock_plextv,
            make_profile("me", user_type=UserType.MANAGED, account_id=400),
            mock_plextv.get_user(400),
            {"me": ME, "other": OTHER},
            snapshot_store,
            own_label=ME,
            labels=ledger.labels(admit, hide),
            ledger=ledger,
            **kw,
        )

    def test_both_libraries_get_both_labels_and_a_second_night_writes_nothing(
        self, mock_plextv, snapshot_store, account
    ):
        ledger, order = account

        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,), hide=(HIDE,))

        assert mock_plextv.users[0].filters["filterMovies"] == (
            "contentRating=G%2CPG|label=Shortlist_me%2CHand%20Picked&label!=Shortlist_other%2CNot%20For%20Kids"
        )
        assert "Hand%20Picked" in mock_plextv.users[0].filters["filterTelevision"]
        assert ledger.saved[400] == {
            "filterMovies": {"admit": [ADMIT], "hide": [HIDE]},
            "filterTelevision": {"admit": [ADMIT], "hide": [HIDE]},
        }
        order.clear()
        assert self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,), hide=(HIDE,)) is None
        assert order == []

    def test_the_record_is_written_AHEAD_of_the_filter(self, mock_plextv, snapshot_store, account):
        """Like the snapshot. A write that lands with no record behind it is a label that looks like the
        owner's for ever and can never be taken back out; a record of a label that never landed costs
        one pass, which finds it missing and adds it. (A RETIRE keeps its record until the write has
        landed — see the next test but one.)"""
        ledger, order = account

        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,))

        assert order == ["ledger", "plex"]

    def test_a_retired_label_stays_on_record_until_it_has_actually_left_the_filter(
        self, mock_plextv, snapshot_store, account
    ):
        """The record goes ahead of the write in BOTH directions. Cleared first, a retire that plex.tv
        refused (or a restart between the two lines) left both labels in the filter with nothing
        listing them: the owner's for ever, still admitting titles, and no setting on screen to explain
        it. Found by review, 2026-09-21."""
        ledger, order = account
        self._sync(mock_plextv, snapshot_store, ledger)
        without = dict(mock_plextv.users[0].filters)
        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,), hide=(HIDE,))
        landed = mock_plextv.update_user_filters.side_effect
        mock_plextv.update_user_filters.side_effect = RuntimeError("plex.tv is away")

        with pytest.raises(RuntimeError):
            self._sync(mock_plextv, snapshot_store, ledger)

        assert ledger.saved[400]["filterMovies"] == {"admit": [ADMIT], "hide": [HIDE]}
        mock_plextv.update_user_filters.side_effect = landed
        order.clear()
        self._sync(mock_plextv, snapshot_store, ledger)
        assert mock_plextv.users[0].filters == without
        assert ledger.saved[400] == {}
        assert order == ["ledger", "plex", "ledger"]  # still listed while in flight; cleared once gone

    def test_a_label_in_flight_is_on_record_beside_the_ones_already_there(self, mock_plextv, snapshot_store, account):
        ledger, _ = account
        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,))
        mock_plextv.update_user_filters.side_effect = RuntimeError("plex.tv is away")

        with pytest.raises(RuntimeError):
            self._sync(mock_plextv, snapshot_store, ledger, admit=("Docs",))

        assert ledger.saved[400]["filterMovies"] == {"admit": [ADMIT, "Docs"]}

    def test_taking_the_labels_away_puts_the_filter_back_and_clears_the_record(
        self, mock_plextv, snapshot_store, account
    ):
        ledger, _ = account
        self._sync(mock_plextv, snapshot_store, ledger)
        without = dict(mock_plextv.users[0].filters)
        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,), hide=(HIDE,))

        self._sync(mock_plextv, snapshot_store, ledger)

        assert mock_plextv.users[0].filters == without
        assert ledger.saved[400] == {}

    def _wiped_but_otherwise_settled(self, mock_plextv, snapshot_store, ledger):
        """The label Shortlist wrote is gone from the filter and the setting is gone too, while the
        row excludes are all in place — so tonight there is NOTHING to write to Plex."""
        self._sync(mock_plextv, snapshot_store, ledger)
        settled_without = dict(mock_plextv.users[0].filters)
        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,))
        assert ledger.saved[400]
        mock_plextv.users[0].filters.update(settled_without)

    def test_a_record_with_nothing_left_behind_it_is_cleared_though_no_filter_write_is_needed(
        self, mock_plextv, snapshot_store, account
    ):
        """Or a `label=Hand Picked` the owner types in next month would be taken for Shortlist's, and
        deleted the day they tidy this setting away."""
        ledger, order = account
        self._wiped_but_otherwise_settled(mock_plextv, snapshot_store, ledger)
        order.clear()

        assert self._sync(mock_plextv, snapshot_store, ledger) is None

        assert order == ["ledger"] and ledger.saved[400] == {}

    def test_but_never_by_a_dry_run(self, mock_plextv, snapshot_store, account):
        ledger, order = account
        self._wiped_but_otherwise_settled(mock_plextv, snapshot_store, ledger)
        order.clear()

        self._sync(mock_plextv, snapshot_store, ledger, dry_run=True)

        assert order == [] and ledger.saved[400]

    def test_a_field_plex_cannot_read_tonight_keeps_the_record_it_had(self, mock_plextv, snapshot_store, account):
        """One field is skipped (a literal `&` in one of the owner's labels), the other written. The
        skipped field was not examined, so what was recorded for it still stands — dropping it would
        orphan the label Shortlist wrote there the day the owner fixes the filter."""
        ledger, _ = account
        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,))
        mock_plextv.users[0].filters["filterTelevision"] = "label=Rock&Roll"  # the owner's, and unreadable

        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,), hide=(HIDE,), refused={})

        assert ledger.saved[400]["filterTelevision"] == {"admit": [ADMIT]}
        assert ledger.saved[400]["filterMovies"] == {"admit": [ADMIT], "hide": [HIDE]}

    def test_the_snapshot_holds_the_filter_from_before_shortlist_not_from_before_the_labels(
        self, mock_plextv, snapshot_store, account
    ):
        ledger, _ = account
        self._sync(mock_plextv, snapshot_store, ledger)
        self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,), hide=(HIDE,))

        assert snapshot_store.get(400).filters["filterMovies"] == "contentRating=G%2CPG"

    def test_a_dry_run_reports_the_write_and_makes_none_of_either_kind(self, mock_plextv, snapshot_store, account):
        ledger, order = account

        diff = self._sync(mock_plextv, snapshot_store, ledger, admit=(ADMIT,), dry_run=True)

        assert "Hand%20Picked" in diff["filterMovies"][1]
        assert order == [] and ledger.saved == {}

    def test_with_nowhere_to_record_them_no_label_is_written(self, mock_plextv, snapshot_store, account):
        """A label nobody recorded writing is one nobody could safely take back out."""
        from shortlist.engine.models import UserType
        from shortlist.engine.privacy import sync_user_restrictions
        from tests.conftest import make_profile

        sync_user_restrictions(
            mock_plextv,
            make_profile("me", user_type=UserType.MANAGED, account_id=400),
            mock_plextv.get_user(400),
            {"me": ME, "other": OTHER},
            snapshot_store,
            own_label=ME,
            labels=TitleLabels(admit=(ADMIT,), hide=(HIDE,)),
        )

        assert mock_plextv.users[0].filters["filterMovies"] == (
            "contentRating=G%2CPG|label=Shortlist_me&label!=Shortlist_other"
        )


class TestTheLogSaysWhatChangedForTheirTitles:
    def test_in_words_per_label_not_as_a_rewritten_filter(self):
        from shortlist.engine.privacy import _summarise_ledger_change

        was = {"filterMovies": {"admit": ["Old"], "hide": ["Scary"]}, "filterTelevision": {"admit": ["Old"]}}
        now = {"filterMovies": {"admit": ["For Kids"], "hide": ["Scary"]}, "filterTelevision": {"admit": ["For Kids"]}}

        assert _summarise_ledger_change(was, now) == (
            "also shows titles labelled 'For Kids'; no longer shows titles labelled 'Old'"
        )
        assert _summarise_ledger_change(now, now) == ""
        assert _summarise_ledger_change({}, {"filterMovies": {"hide": ["Scary"]}}) == "hides titles labelled 'Scary'"


class TestWhereTheLabelsComeFrom:
    def test_prefs_and_the_stored_record_become_the_accounts_labels_and_junk_is_ignored(self):
        from shortlist.server.services.context_builder import _title_labels

        labels = _title_labels(
            {"admit_labels": ["For Kids", "", 7], "hide_labels": "not a list"},
            {"filterMovies": {"admit": ["For Kids"], "hide": None, "hide_copy": ["Scary"]}, "junk": 3},
        )

        assert labels.admit == ("For Kids",) and labels.hide == ()
        assert labels.written_in("filterMovies", "admit") == ("For Kids",)
        assert labels.written_in("filterMovies", "hide_copy") == ("Scary",)
        assert labels.written_in("filterTelevision", "hide") == ()
        assert not _title_labels({})

    def test_a_record_with_nothing_configured_still_counts_as_something_to_do(self):
        """Every label removed from the settings: the record is all that is left, and it is exactly
        what tells the next pass to take them back out. An account like that must not be dropped
        from the context as "no labels"."""
        from shortlist.server.services.context_builder import _title_labels

        assert _title_labels({}, {"filterMovies": {"admit": ["For Kids"]}})

    def test_a_record_smuggled_into_prefs_is_not_a_record(self):
        """Prefs pass unknown keys through, so an old install or a hand-edited row could carry one.
        The record decides what may be REMOVED from someone's restriction: only the column counts."""
        from shortlist.server.services.context_builder import _title_labels

        assert not _title_labels({"written_title_labels": {"filterMovies": {"admit": ["Kids"]}}})

    def test_the_record_has_a_column_of_its_own_that_a_settings_save_cannot_clobber(self, tmp_path):
        """`prefs` is rewritten WHOLE by every settings PATCH. Kept in there, a PATCH that read prefs a
        moment before a privacy pass saved the record wrote the old record back over it — and a lost
        record turns Shortlist's labels into "the owner's" for good."""
        from shortlist.server.db.adapters import DbTitleLabelLedger
        from shortlist.server.db.models import User
        from shortlist.server.db.session import make_engine, make_session_factory, run_migrations

        run_migrations(tmp_path)
        engine = make_engine(tmp_path)
        sessions = make_session_factory(engine)
        with sessions() as session:
            session.add(User(plex_account_id=400, username="kids", slug="kids", prefs={"household": "kids"}))
            session.commit()
        ledger = DbTitleLabelLedger(sessions)

        with sessions() as patching:  # a settings save that read the row BEFORE the record was saved…
            stale = patching.query(User).filter_by(plex_account_id=400).one()
            prefs_as_read = dict(stale.prefs)
            ledger.save(400, {"filterMovies": {"admit": ["For Kids"]}})
            ledger.save(999, {"filterMovies": {"admit": ["Nobody"]}})  # no such account: nothing to record
            stale.prefs = {**prefs_as_read, "admit_labels": ["For Kids"]}  # …and commits after it
            patching.commit()

        with sessions() as session:
            user = session.query(User).filter_by(plex_account_id=400).one()
            assert user.title_labels_written == {"filterMovies": {"admit": ["For Kids"]}}
            assert user.prefs == {"household": "kids", "admit_labels": ["For Kids"]}

        ledger.save(400, {})
        with sessions() as session:
            assert session.query(User).filter_by(plex_account_id=400).one().title_labels_written is None
        engine.dispose()


class TestWhatTheLogLineSays:
    def test_the_write_is_logged_in_words_about_their_titles(self, mock_plextv, snapshot_store, caplog):
        """The filter summary only knows Shortlist's row labels, so a write that changed what a child
        can see used to read "filterMovies rewritten"."""
        import logging

        from loguru import logger

        from shortlist.engine.models import UserType
        from shortlist.engine.privacy import sync_user_restrictions
        from tests.conftest import make_profile, plextv_user

        mock_plextv.users = [plextv_user(400, "me", filters={"filterMovies": "contentRating=G"})]
        ledger = _Ledger([])
        handler_id = logger.add(
            lambda message: caplog.handler.emit(
                logging.LogRecord("loguru", logging.INFO, "", 0, message.record["message"], None, None)
            )
        )
        try:
            with caplog.at_level(logging.INFO):
                sync_user_restrictions(
                    mock_plextv,
                    make_profile("me", user_type=UserType.MANAGED, account_id=400),
                    mock_plextv.get_user(400),
                    {"me": ME, "other": OTHER},
                    snapshot_store,
                    own_label=ME,
                    labels=TitleLabels(admit=(ADMIT,)),
                    ledger=ledger,
                )
        finally:
            logger.remove(handler_id)

        assert "also shows titles labelled 'Hand Picked'" in caplog.text
