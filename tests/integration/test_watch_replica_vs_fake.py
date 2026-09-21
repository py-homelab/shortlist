"""The watching-account transfer against the in-process fake PMS — real client, real HTTP.

The unit suites mock the PMS away, so nothing there can catch a wrong query string, a missing
container header, or a response shape the parser mis-reads. This is the layer that can: real
`PlexClient`, real httpx, real (loopback) HTTP, and a fake server that behaves the way a live one was
measured to behave on 2026-08-25.

The fake is deliberately no easier than the real thing (testing rules). Two behaviours matter most:

* a scrobble **increments** `viewCount` and never sets it — which is what makes the shortfall-vs-total
  bug detectable rather than invisible;
* a scrobble on a **show** key marks every episode, exactly as a real server does. That is the bug
  this whole feature exists to stop, so the fake reproduces it instead of refusing it: write a show
  key and this test sees all ten episodes watched.
"""

from __future__ import annotations

import threading
import time

import pytest
import uvicorn

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.models import MediaType
from shortlist.engine.watch_replica import build_plan
from tests.fakes.fake_plex import FakeHistoryEntry, FakeMovie, FakePlexState, make_fake_plex, seed_state

OWNER_TOKEN = "owner-token"
TARGET_TOKEN = "server-203"  # the Home canary; `watched_account_id` maps it to account 203
TARGET_ACCOUNT = 203
SHOW_KEY = 301


class _UvicornThread:
    def __init__(self, app):
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self):
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("fake PMS did not start")

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _add_episodes(state: FakePlexState, show_key: int, count: int) -> list[int]:
    """Give one seeded show real episodes, so the leaf reads have something to return."""
    section = state.section_of(show_key)
    assert section is not None
    keys = []
    for number in range(1, count + 1):
        key = 900_000 + show_key * 100 + number
        section.items[key] = FakeMovie(
            rating_key=key,
            title=f"Episode {number:02d}",
            year=2001,
            added_at=1_700_000_000 + number,
            tmdb_id=0,
            audience_rating=7.0,
            media_type="episode",
            grandparent_rating_key=show_key,
            parent_index=1,
            index=number,
        )
        keys.append(key)
    return keys


@pytest.fixture
def pms():
    state = seed_state()
    episodes = _add_episodes(state, SHOW_KEY, 10)
    server = _UvicornThread(make_fake_plex(state)).start()
    client = PlexClient(server.url, OWNER_TOKEN)
    yield state, client, episodes
    server.stop()


def _sections(state: FakePlexState) -> list[tuple[str, MediaType]]:
    return [(str(s.key), MediaType.MOVIE if s.type == "movie" else MediaType.SHOW) for s in state.sections.values()]


class TestTheShowReadSeesWhatUnwatchedZeroHides:
    """Issue #108, end to end over real HTTP: a series `?type=2&unwatched=0` cannot see.

    That query filters on the show's own watch-state row, which marking a series or a season never
    establishes — so a finished series is absent from it while its episode counts are correct.
    `viewedLeafCount!=0` filters on the counts and returns it. Measured on two independent servers:
    533 shows against 491, with 20 shows missing from `unwatched=0` entirely.

    The fake makes the two queries DISAGREE (`FakePlexState.invisible_to_show_read`) because
    otherwise they answer from the same set, the distinction is unrepresentable, and this test would
    pass against either query — the rule that a fake must be no easier than the real server.
    """

    def test_a_show_hidden_from_unwatched_0_still_comes_back(self, pms):
        state, client, _episodes = pms
        state.history.append(FakeHistoryEntry(account_id=1, rating_key=SHOW_KEY, viewed_at=1_700_000_000))
        state.watch_episodes(1, SHOW_KEY, 4)  # four episodes in
        section = state.section_of(SHOW_KEY)

        before = client.watched_titles(str(section.key), MediaType.SHOW, OWNER_TOKEN)
        assert SHOW_KEY in {i.rating_key for i in before.items}, "precondition: normally visible"

        # Now Plex stops returning it from `unwatched=0`, exactly as it does for a series marked
        # watched rather than played. The read asks `viewedLeafCount!=0` instead, so it still lands.
        state.invisible_to_show_read.add(SHOW_KEY)
        after = client.watched_titles(str(section.key), MediaType.SHOW, OWNER_TOKEN)

        still_there = {i.rating_key: i for i in after.items}
        assert SHOW_KEY in still_there, "the show vanished with the query that hides it"
        assert still_there[SHOW_KEY].viewed_leaf_count == 4, "its watched episode count was lost"

    def test_a_show_with_nothing_watched_is_not_returned(self, pms):
        """`viewedLeafCount!=0` means what it says — and it is applied client-side too, so a server
        that ignores the filter and answers with the whole library still yields the same set."""
        state, client, _episodes = pms
        state.history.append(FakeHistoryEntry(account_id=1, rating_key=SHOW_KEY, viewed_at=1_700_000_000))
        state.watch_episodes(1, SHOW_KEY, 0)  # watched nothing of it
        section = state.section_of(SHOW_KEY)

        read = client.watched_titles(str(section.key), MediaType.SHOW, OWNER_TOKEN)

        assert SHOW_KEY not in {i.rating_key for i in read.items}

    def test_a_show_the_read_cannot_date_takes_its_newest_EPISODE_date(self, pms):
        """The other half of the same Plex behaviour, end to end over real HTTP.

        A show marked watched carries no `lastViewedAt` of its own — `viewedLeafCount` is right and
        the date is simply absent, which `_watched_item` has to read as 1970. That is not cosmetic: a
        1970 date weighs zero as a seed, so the show never seeds again, and the effectiveness report
        showed a series finished minutes ago as "finished 20697d ago" (reported on #108). The repair
        reads the library's episodes and takes the newest.

        The fake omits the attribute for shows in `undated_in_show_read`, because otherwise every
        watched show it serves is dated, the undated shape is unrepresentable, and the repair is dead
        code in every full-stack test.
        """
        state, client, episodes = pms
        state.history.append(FakeHistoryEntry(account_id=1, rating_key=SHOW_KEY, viewed_at=1_700_000_000))
        # Episodes 2 and 7, deliberately not the first or the last: the repair must take the NEWEST
        # of the watched episodes, not whichever the walk happened to see first or last.
        state.leaf(1, episodes[1])[0] = 1
        state.leaf(1, episodes[6])[0] = 1
        state.watch_episodes(1, SHOW_KEY, 2)
        section = state.section_of(SHOW_KEY)

        state.undated_in_show_read.add(SHOW_KEY)
        read = client.watched_titles(str(section.key), MediaType.SHOW, OWNER_TOKEN)

        found = {i.rating_key: i for i in read.items}[SHOW_KEY]
        assert found.watched_at.year > 1970, "the show kept the epoch — the episode date repair did not run"
        newest = max(state.section_of(SHOW_KEY).items[key].added_at for key in (episodes[1], episodes[6]))
        assert int(found.watched_at.timestamp()) == newest


class TestTheLeafReadsOverRealHttp:
    def test_a_watched_episode_comes_back_with_its_show_key(self, pms):
        state, client, episodes = pms
        state.leaf(1, episodes[0])[0] = 1  # owner watched episode 1

        found = client.read_watch_state(_sections(state), OWNER_TOKEN)

        assert found.items[episodes[0]].show_rating_key == SHOW_KEY
        assert found.items[episodes[0]].media_type == "episode"

    def test_a_part_watched_episode_is_returned_with_no_view_count(self, pms):
        """Invisible to `unwatched=0`, which is precisely what the old transfer dropped."""
        state, client, episodes = pms
        state.leaf(1, episodes[3])[1] = 1_200_000

        found = client.read_watch_state(_sections(state), OWNER_TOKEN)

        assert found.items[episodes[3]].view_count == 0
        assert found.items[episodes[3]].view_offset_ms == 1_200_000

    def test_a_rewatched_movie_reports_its_real_count(self, pms):
        state, client, _ = pms
        state.leaf(1, 101)[0] = 3

        found = client.read_watch_state(_sections(state), OWNER_TOKEN)

        assert found.items[101].view_count == 3

    def test_an_untouched_account_reads_as_empty(self, pms):
        state, client, _ = pms

        assert client.read_watch_state(_sections(state), TARGET_TOKEN).items == {}


class TestReplicatingOverRealHttp:
    def _replicate(self, state, client, episodes):
        """Plan and apply, exactly as the service does — read both, build, write in order."""
        sections = _sections(state)
        source = client.read_watch_state(sections, OWNER_TOKEN)
        target = client.read_watch_state(sections, TARGET_TOKEN)
        for op in build_plan(source, target):
            client.apply_watch_op(op, TARGET_TOKEN)
        return source

    def test_a_part_watched_show_stays_part_watched(self, pms):
        """The One Piece case, end to end over HTTP. Three of ten episodes watched must land as
        three — the old code wrote the SHOW key here and the fake would report all ten."""
        state, client, episodes = pms
        for key in episodes[:3]:
            state.leaf(1, key)[0] = 1

        self._replicate(state, client, episodes)

        watched = [k for k in episodes if state.leaf_view(TARGET_ACCOUNT, k)[0] > 0]
        assert watched == episodes[:3]

    def test_rewatch_counts_land_exactly(self, pms):
        state, client, episodes = pms
        state.leaf(1, 101)[0] = 3

        self._replicate(state, client, episodes)

        assert state.leaf_view(TARGET_ACCOUNT, 101)[0] == 3

    def test_a_part_watched_film_keeps_its_position(self, pms):
        state, client, episodes = pms
        state.leaf(1, 102)[1] = 490_509

        self._replicate(state, client, episodes)

        assert state.leaf_view(TARGET_ACCOUNT, 102) == (0, 490_509)

    def test_a_film_both_watched_and_in_progress_keeps_both(self, pms):
        state, client, episodes = pms
        state.leaf(1, 103)[:] = [1, 490_509]

        self._replicate(state, client, episodes)

        assert state.leaf_view(TARGET_ACCOUNT, 103) == (1, 490_509)

    def test_it_removes_what_the_owner_has_not_watched(self, pms):
        state, client, episodes = pms
        state.leaf(1, 101)[0] = 1
        state.leaf(TARGET_ACCOUNT, 104)[0] = 2  # the target watched something the owner did not

        self._replicate(state, client, episodes)

        assert state.leaf_view(TARGET_ACCOUNT, 104)[0] == 0
        assert state.leaf_view(TARGET_ACCOUNT, 101)[0] == 1

    def test_it_repairs_an_account_a_show_key_write_spoiled(self, pms):
        """What the OLD transfer did, then what the new one does about it. The fake reproduces the
        show-key behaviour faithfully, so this starts from the real damage rather than a mock of it."""
        state, client, episodes = pms
        for key in episodes[:2]:
            state.leaf(1, key)[0] = 1
        state.scrobble(TARGET_ACCOUNT, SHOW_KEY)  # the old bug: one write, all ten episodes
        assert sum(1 for k in episodes if state.leaf_view(TARGET_ACCOUNT, k)[0]) == 10

        self._replicate(state, client, episodes)

        assert [k for k in episodes if state.leaf_view(TARGET_ACCOUNT, k)[0]] == episodes[:2]

    def test_running_it_twice_changes_nothing_the_second_time(self, pms):
        """The fixed point, over the wire. A count that climbed — the shortfall-vs-total bug — shows
        up here as a second pass that keeps writing."""
        state, client, episodes = pms
        state.leaf(1, 101)[0] = 3
        state.leaf(1, 102)[1] = 90_000
        for key in episodes[:2]:
            state.leaf(1, key)[0] = 1
        self._replicate(state, client, episodes)

        sections = _sections(state)
        source = client.read_watch_state(sections, OWNER_TOKEN)
        again = build_plan(source, client.read_watch_state(sections, TARGET_TOKEN))

        assert again == []
        assert state.leaf_view(TARGET_ACCOUNT, 101)[0] == 3  # not 6

    def test_the_owners_own_account_is_never_written_to(self, pms):
        """The fear that stops people using this feature at all. Worth an assertion of its own."""
        state, client, episodes = pms
        state.leaf(1, 101)[0] = 1
        before = {k: list(v) for k, v in state.leaf_state.get(1, {}).items()}

        self._replicate(state, client, episodes)

        assert {k: list(v) for k, v in state.leaf_state.get(1, {}).items()} == before

    def test_no_history_row_is_written_by_any_of_it(self, pms):
        """Probed live: 31 scrobbles left the history log empty. The transfer copies the SOURCE's play
        log deliberately; if scrobbles also wrote one, every figure in the report would double-count."""
        state, client, episodes = pms
        before = len(state.history)
        state.leaf(1, 101)[0] = 2
        for key in episodes[:3]:
            state.leaf(1, key)[0] = 1

        self._replicate(state, client, episodes)

        assert len(state.history) == before

    def test_an_unreachable_key_is_skipped_rather_than_raising(self, pms):
        """A target with narrower library sharing 404s. One of those must not abandon the run."""
        _state, client, _ = pms
        from shortlist.engine.watch_replica import OpKind, WriteOp

        op = WriteOp(kind=OpKind.MARK, rating_key=999_999_999, media_type="movie", view_count=1, scrobbles=1)

        assert client.apply_watch_op(op, TARGET_TOKEN) is False


@pytest.fixture
def db(tmp_path):
    from shortlist.server.db.models import User
    from shortlist.server.db.session import make_engine, make_session_factory, run_migrations

    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    sessions = make_session_factory(engine)
    with sessions() as session:
        # The fake files the owner's plays under its LOCAL PMS account id, as a real server does.
        session.add(User(id=1, plex_account_id=1, username="steve", slug="steve", user_type="owner"))
        session.add(User(id=2, plex_account_id=TARGET_ACCOUNT, username="kids", slug="kids", user_type="managed"))
        session.commit()
    yield sessions
    engine.dispose()


class TestACopyNarrowedByRatingOverRealHttp:
    """The children's-profile copy: the real SERVICE, the real client, and a PMS that really restricts
    the target.

    The unit suite hands the service a set of hidden keys. Here the hiding is the fake PMS applying the
    profile's `contentRating` share filters to a batch read made AS the target — the recorded behaviour
    (`pms_share_filter_allow_lists.json`) — and the ratings arrive as attributes on the leaf rows, the
    way the real parser has to find them. Nothing about the narrowing is re-implemented here: an
    earlier version of this class computed it by hand and so never ran `_narrow` at all.
    """

    KIDS = ("G", "TV-Y")
    OTHER_SHOW = 302

    def _household(self, state, episodes):
        """Four films (two G, one R, one unrated), a TV-Y show the profile may see, and a second show
        whose EPISODES say TV-Y while the show itself is TV-14 — which is what Plex restricts on."""
        state.item(101).content_rating = "G"
        state.item(102).content_rating = "R"
        state.item(103).content_rating = "G"
        state.item(104).content_rating = ""  # Plex holds no rating for it
        state.item(SHOW_KEY).content_rating = "TV-Y"
        state.item(self.OTHER_SHOW).content_rating = "TV-14"
        hidden_episodes = _add_episodes(state, self.OTHER_SHOW, 2)
        for key in (*episodes, *hidden_episodes):
            state.item(key).content_rating = "TV-Y"
        for key in (101, 102, 103, 104, *episodes[:2], *hidden_episodes):
            state.leaf(1, key)[0] = 1
        kids = state.users[TARGET_ACCOUNT]
        kids.filters["filterMovies"] = "contentRating=G"
        kids.filters["filterTelevision"] = "contentRating=TV-Y"
        # The owner's play log, where the R-rated play sits right beside the G-rated ones. (The seed
        # also gives the owner eight unrated films, 109-116, which is why "" counts nine below.)
        state.history.extend(FakeHistoryEntry(1, key, 1_600_000_000 + key) for key in (101, 102, 103))
        return hidden_episodes

    def _copy(self, db, client, **kw):
        from shortlist.server.services.watching_account import transfer_watch_history

        with db() as session:
            report = transfer_watch_history(
                session,
                sessions=db,
                from_user_id=1,
                to_user_id=2,
                plex=client,
                source_token=OWNER_TOKEN,
                target_token=TARGET_TOKEN,
                ratings=list(self.KIDS),
                **kw,
            )
            session.commit()
        return report

    def _landed(self, state, keys):
        return {k for k in keys if state.leaf_view(TARGET_ACCOUNT, k)[0]}

    def test_only_the_childrens_titles_the_profile_can_see_land(self, pms, db):
        state, client, episodes = pms
        hidden = self._household(state, episodes)

        report = self._copy(db, client)

        assert self._landed(state, (101, 102, 103, 104, *episodes, *hidden)) == {101, 103, *episodes[:2]}
        assert (report.kept, report.unreachable, report.failed, report.verify_mismatched) == (4, 0, 0, 0)

    def test_episodes_under_a_show_the_profile_cannot_see_stay_behind(self, pms, db):
        """Rated TV-Y episode by episode, inside the list — and the SHOW is TV-14, which is what the
        profile's restriction hides. Asked about the show, Plex says no, and both are left out; asked
        about nothing, they would be written to an account that can never see them."""
        state, client, episodes = pms
        hidden = self._household(state, episodes)

        report = self._copy(db, client)

        assert self._landed(state, hidden) == set()
        assert report.hidden_from_target == 2
        assert report.ratings_seen == {"": 9, "TV-Y": 4, "G": 2, "R": 1}
        assert report.ratings_kept == {"G": 2, "TV-Y": 2}

    def test_shortlists_own_history_for_the_profile_holds_no_adult_play(self, pms, db):
        """Read from the PMS's real history endpoint, where the R-rated play sits right beside the
        G-rated one under the same account."""
        from shortlist.server.db.models import WatchEvent

        state, client, episodes = pms
        self._household(state, episodes)

        self._copy(db, client)

        with db() as session:
            copied = {e.rating_key for e in session.query(WatchEvent).filter_by(plex_account_id=TARGET_ACCOUNT)}
        assert copied == {101, 103, *episodes[:2]}

    def test_a_preview_writes_nothing_and_shows_the_same_split(self, pms, db):
        state, client, episodes = pms
        hidden = self._household(state, episodes)

        report = self._copy(db, client, dry_run=True)

        assert self._landed(state, (101, 102, 103, 104, *episodes, *hidden)) == set()
        assert (report.kept, report.left_out, report.hidden_from_target) == (4, 12, 2)
        assert report.kept_preview == sorted([state.item(101).title, state.item(103).title, state.item(SHOW_KEY).title])


class TestTheFakeAnswersAByKeyTvReadTheWayARealServerDid:
    """`tests/fixtures/pms_share_filter_tv_by_key.json`, replayed against the fake (rule 11: a fixture
    nothing reads is documentation).

    The narrowed copy and every restricted person's rows both ask "which of these can this account
    see?" with one by-key read. For SHOWS and EPISODES under a `filterTelevision` rating allow list,
    that was an assumption until it was measured on 2026-09-20 — the fake extrapolated it from the
    movie recording. Each assertion below is one recorded fact, read off the fixture rather than
    restated, so the fake cannot be made easier than the server without this failing.
    """

    @staticmethod
    def _recorded() -> dict:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "fixtures" / "pms_share_filter_tv_by_key.json"
        return json.loads(path.read_text())

    def _world(self, state, episodes):
        """The recorded six keys, built in the fake with the recorded ratings and the recorded filters."""
        recorded = self._recorded()
        ratings = {name: spec["contentRating"] for name, spec in recorded["keys"].items()}
        other_show = 302
        out_episodes = _add_episodes(state, other_show, 1)
        keys = {
            "show_in": SHOW_KEY,
            "show_out": other_show,
            "episode_in": episodes[0],
            "episode_out": out_episodes[0],
            "movie_in": 101,
            "movie_out": 102,
        }
        for name, key in keys.items():
            state.item(key).content_rating = ratings[name]
        account = state.users[TARGET_ACCOUNT]
        account.filters["filterMovies"] = recorded["account"]["filterMovies"]
        account.filters["filterTelevision"] = recorded["account"]["filterTelevision"]
        return recorded, keys

    def test_a_batch_leaves_out_the_hidden_show_and_the_episode_under_it(self, pms):
        state, client, episodes = pms
        recorded, keys = self._world(state, episodes)

        seen = client.visible_to(TARGET_TOKEN, list(keys.values()))

        assert {name for name, key in keys.items() if key in seen} == set(
            recorded["read_as_account"]["all_six"]["present"]
        )

    def test_the_admin_sees_all_six(self, pms):
        state, client, episodes = pms
        recorded, keys = self._world(state, episodes)

        assert len(client.visible_to(OWNER_TOKEN, list(keys.values()))) == recorded["read_as_admin"]["all_six"]["size"]

    def test_a_batch_of_only_hidden_keys_is_a_404_which_reads_as_none_visible(self, pms):
        import httpx

        state, client, episodes = pms
        recorded, keys = self._world(state, episodes)
        hidden = [keys[name] for name in recorded["read_as_account"]["all_six"]["absent"]]

        raw = httpx.get(
            f"{client._server._baseurl}/library/metadata/{','.join(map(str, hidden))}",
            headers={"X-Plex-Token": TARGET_TOKEN, "Accept": "application/json"},
        )

        assert raw.status_code == recorded["read_as_account"]["batch_of_only_hidden"]["status"]
        assert client.visible_to(TARGET_TOKEN, hidden) == set()

    def test_an_episode_rated_inside_the_list_is_still_hidden_under_a_show_that_is_not(self, pms):
        """The tree is closed at the show (its /children and /allLeaves were 404), so an episode cannot
        be visible under a show that is not — whatever rating the episode itself carries."""
        state, client, episodes = pms
        _, keys = self._world(state, episodes)
        state.item(keys["episode_out"]).content_rating = "TV-Y"

        assert client.visible_to(TARGET_TOKEN, [keys["episode_out"], keys["episode_in"]]) == {keys["episode_in"]}

    def test_order_does_not_matter(self, pms):
        state, client, episodes = pms
        _, keys = self._world(state, episodes)

        assert client.visible_to(TARGET_TOKEN, [keys["show_out"], keys["episode_out"], keys["show_in"]]) == {
            keys["show_in"]
        }

    def test_asking_about_the_show_gives_the_answer_asking_about_the_episode_would(self, pms):
        """Conclusion 4 — what lets the narrowed copy ask one question per show."""
        state, client, episodes = pms
        _, keys = self._world(state, episodes)

        by_show = client.visible_to(TARGET_TOKEN, [keys["show_in"], keys["show_out"]])
        by_episode = client.visible_to(TARGET_TOKEN, [keys["episode_in"], keys["episode_out"]])

        assert (keys["show_in"] in by_show, keys["show_out"] in by_show) == (
            keys["episode_in"] in by_episode,
            keys["episode_out"] in by_episode,
        )
