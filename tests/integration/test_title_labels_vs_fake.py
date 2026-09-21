"""The owner's admit / hide labels, read back AS the restricted account from the fake PMS.

The unit suite proves the planned filter string with an independent evaluator. This asks the other
question: handed that exact string, does a server that applies share filters the way a real one was
recorded to (`pms_share_filter_allow_lists.json`, `pms_share_filter_tv_by_key.json`) show that account
what the owner meant? It is the same by-key read a restricted person's rows are built from
(`RowPolicy.visible`), so it is also the proof that rows follow the labels with no code of their own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.models import MediaType
from shortlist.engine.privacy import plan_account_filter
from tests.fakes.fake_plex import make_fake_plex, seed_state
from tests.integration.test_watch_replica_vs_fake import OWNER_TOKEN, TARGET_ACCOUNT, TARGET_TOKEN, _UvicornThread

PLANET_EARTH, SPACE_ODYSSEY, BLUEY, ALIEN = 301, 101, 302, 102
ADMIT, HIDE = ("For Kids",), ("Not For Kids",)


@pytest.fixture
def kids():
    state = seed_state()
    state.item(PLANET_EARTH).content_rating = "TV-G"  # outside the list, hand-picked in
    state.item(PLANET_EARTH).labels = ["For Kids"]
    state.item(BLUEY).content_rating = "TV-Y"
    state.item(SPACE_ODYSSEY).content_rating = "G"  # inside the list, hand-picked out
    state.item(SPACE_ODYSSEY).labels = ["Not For Kids"]
    state.item(ALIEN).content_rating = "R"
    account = state.users[TARGET_ACCOUNT]
    account.filters["filterMovies"] = "contentRating=G%2CPG"
    account.filters["filterTelevision"] = "contentRating=TV-Y%2CTV-Y7%2CTV-Y7-FV"
    server = _UvicornThread(make_fake_plex(state)).start()
    yield state, PlexClient(server.url, OWNER_TOKEN)
    server.stop()


def _write(state, ledger: dict, *, admit=(), hide=()) -> None:
    """One privacy pass over the account's two fields, keeping the ledger the way the sync does."""
    account = state.users[TARGET_ACCOUNT]
    for field in ("filterMovies", "filterTelevision"):
        account.filters[field], ledger[field] = plan_account_filter(
            account.filters[field],
            {"Shortlist_other"},
            own_row_label="Shortlist_kids",
            show=True,
            admit=admit,
            hide=hide,
            written=ledger.get(field),
        )


def test_before_the_labels_the_ratings_alone_decide(kids):
    state, client = kids
    _write(state, {})

    assert client.visible_to(TARGET_TOKEN, [PLANET_EARTH, SPACE_ODYSSEY, BLUEY, ALIEN]) == {SPACE_ODYSSEY, BLUEY}


def test_a_hand_picked_title_gets_in_and_a_hand_refused_one_is_kept_out(kids):
    state, client = kids
    _write(state, {}, admit=ADMIT, hide=HIDE)

    assert client.visible_to(TARGET_TOKEN, [PLANET_EARTH, SPACE_ODYSSEY, BLUEY, ALIEN]) == {PLANET_EARTH, BLUEY}


def test_taking_the_labels_away_is_back_to_the_ratings(kids):
    state, client = kids
    ledger: dict = {}
    _write(state, ledger, admit=ADMIT, hide=HIDE)
    _write(state, ledger)

    assert client.visible_to(TARGET_TOKEN, [PLANET_EARTH, SPACE_ODYSSEY, BLUEY, ALIEN]) == {SPACE_ODYSSEY, BLUEY}


class TestWhichTitlesCarryALabel:
    """`PlexClient.items_labelled` — the owner's read behind "a labelled title IS a children's title
    for this account's rows". The fake answers the way a real PMS was recorded to
    (`pms_label_listing.json`): no inline labels on a listing, a missing label is a 200 with no rows,
    and a label on a show matches the show and none of its episodes."""

    def _section(self, client, kind):
        return client.sections_by_type()[kind]

    def test_a_movie_and_a_show_by_label_each_in_its_own_library(self, kids):
        _, client = kids

        assert client.items_labelled(self._section(client, MediaType.MOVIE), "Not For Kids") == {SPACE_ODYSSEY}
        assert client.items_labelled(self._section(client, MediaType.SHOW), "For Kids") == {PLANET_EARTH}

    def test_the_name_is_matched_whatever_its_case(self, kids):
        _, client = kids

        assert client.items_labelled(self._section(client, MediaType.SHOW), "for kids") == {PLANET_EARTH}

    def test_a_label_the_server_has_but_this_library_does_not_is_empty_not_missing(self, kids):
        """The label choices are the SERVER's, identical in every section (recorded: the same 104 under
        Movies and TV). So "For Kids", carried only by a show, is offered under Movies too."""
        _, client = kids

        assert client.items_labelled(self._section(client, MediaType.MOVIE), "For Kids") == frozenset()

    def test_a_label_plex_does_not_have_is_none_so_a_typo_can_be_told_from_nothing_labelled(self, kids):
        _, client = kids

        assert client.items_labelled(self._section(client, MediaType.MOVIE), "For Kidz") is None

    def test_a_label_named_like_another_labels_key_is_still_found_by_its_name(self, kids):
        """Why the listing is asked for by KEY: `label=2024` by name would be read as a key."""
        state, client = kids
        state.item(ALIEN).labels = ["For Kids"]
        numeric = str(state.label_keys()["for kids"])  # a label whose NAME is "For Kids"'s key
        state.item(SPACE_ODYSSEY).labels = [numeric]

        assert client.items_labelled(self._section(client, MediaType.MOVIE), numeric) == {SPACE_ODYSSEY}

    def test_the_label_choices_are_read_once_per_library_however_many_labels_are_named(self, kids):
        _, client = kids
        section = self._section(client, MediaType.MOVIE)
        asked = []
        real = client._server.query
        client._server.query = lambda path, *a, **kw: asked.append(path) or real(path, *a, **kw)

        client.items_labelled(section, "Not For Kids")
        client.items_labelled(section, "For Kids")

        assert sum(path.endswith("/label") for path in asked) == 1

    def test_every_account_naming_a_label_shares_one_read(self, kids):
        _, client = kids
        section = self._section(client, MediaType.SHOW)
        client.items_labelled(section, "For Kids")
        asked = []
        real = client._server.query
        client._server.query = lambda path, *a, **kw: asked.append(path) or real(path, *a, **kw)

        client.items_labelled(section, "For Kids")

        assert asked == []


class TestTheRecordedListing:
    """What `items_labelled` and the fake above are built on, read off the recording."""

    RECORDED = json.loads((Path(__file__).parents[1] / "fixtures" / "pms_label_listing.json").read_text())

    def test_a_listing_carries_no_inline_labels_even_when_asked_to(self):
        rows = self.RECORDED["reads"]["7_includeLabels_probe_one_item"]["metadata"]

        assert rows and not any(row["label_inline"] for row in rows)

    def test_a_missing_label_is_a_200_with_no_rows_by_name_and_by_key(self):
        for read in ("6a_missing_label_by_name", "6b_missing_label_by_key"):
            assert (self.RECORDED["reads"][read]["status"], self.RECORDED["reads"][read]["metadata_count"]) == (200, 0)

    def test_a_show_label_matches_the_show_and_none_of_its_episodes(self):
        reads = self.RECORDED["reads"]

        assert reads["4a_tv_KidsAllow_by_key"]["metadata_count"] == 6
        assert reads["4c_tv_episodes_KidsAllow_by_key_size_only"]["metadata_count"] == 0

    def test_the_label_choices_are_the_servers_not_the_librarys(self):
        assert self.RECORDED["label_keys"]["movie"] == self.RECORDED["label_keys"]["show"]
        choices = self.RECORDED["label_choices"]
        assert choices["movie_no_type"]["directory_count"] == choices["show_no_type"]["directory_count"] == 104
