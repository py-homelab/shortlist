"""The owner's admit / hide labels, read back AS the restricted account from the fake PMS.

The unit suite proves the planned filter string with an independent evaluator. This asks the other
question: handed that exact string, does a server that applies share filters the way a real one was
recorded to (`pms_share_filter_allow_lists.json`, `pms_share_filter_tv_by_key.json`) show that account
what the owner meant? It is the same by-key read a restricted person's rows are built from
(`RowPolicy.visible`), so it is also the proof that rows follow the labels with no code of their own.
"""

from __future__ import annotations

import pytest

from shortlist.engine.clients.plex_pms import PlexClient
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
