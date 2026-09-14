"""`PlexClient.visible_to` — which of these library items can this ACCOUNT see? (#115)

Read AS the account: `GET /library/metadata/{k1,k2,...}` with its own server token. Recorded on a real
PMS (`tests/fixtures/pms_share_filter_allow_lists.json`, `read_as_account`): the items its share filter
hides are simply absent from a 200, and a batch holding none it can see is a 404 — the same shape as a
batch of deleted keys, which is why a 404 here means "none visible", not a failure.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients import plex_pms
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.models import Candidate, EngineConfig, MediaType, UserProfile, UserType
from shortlist.engine.rows import RowPolicy, _visible_candidates


def _client() -> PlexClient:
    client = PlexClient.__new__(PlexClient)
    client._server = MagicMock()
    client._server.url.side_effect = lambda path, includeToken=False: f"http://pms{path}"
    client._timeout = 20
    return client


def _response(status: int, keys: list[int] = ()) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"MediaContainer": {"size": len(keys), "Metadata": [{"ratingKey": str(k)} for k in keys]}}
    if status >= 400:
        r.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return r


class TestVisibleTo:
    def test_the_keys_a_200_returns_are_the_visible_ones(self, monkeypatch):
        get = MagicMock(return_value=_response(200, [1, 3]))
        monkeypatch.setattr(plex_pms.http_retry, "get", get)

        assert _client().visible_to("user-token", [1, 2, 3]) == {1, 3}

        call = get.call_args
        assert call.args[0] == "http://pms/library/metadata/1,2,3"
        assert call.kwargs["headers"]["X-Plex-Token"] == "user-token"

    def test_a_404_means_none_of_them_are_visible(self, monkeypatch):
        monkeypatch.setattr(plex_pms.http_retry, "get", MagicMock(return_value=_response(404)))

        assert _client().visible_to("user-token", [1, 2]) == set()

    @pytest.mark.parametrize("status", [401, 500])
    def test_any_other_failure_raises_rather_than_reading_as_nothing_visible(self, monkeypatch, status):
        """An expired token answering "none visible" would empty every row of that person's."""
        monkeypatch.setattr(plex_pms.http_retry, "get", MagicMock(return_value=_response(status)))

        with pytest.raises(RuntimeError):
            _client().visible_to("user-token", [1, 2])

    def test_a_long_list_is_read_in_bounded_batches(self, monkeypatch):
        keys = list(range(1, 121))
        get = MagicMock(side_effect=lambda url, **_: _response(200, [int(k) for k in url.rsplit("/", 1)[1].split(",")]))
        monkeypatch.setattr(plex_pms.http_retry, "get", get)

        assert _client().visible_to("user-token", keys) == set(keys)
        assert get.call_count == 3
        assert all(len(c.args[0].rsplit("/", 1)[1].split(",")) <= 50 for c in get.call_args_list)

    def test_no_keys_reads_nothing(self, monkeypatch):
        get = MagicMock()
        monkeypatch.setattr(plex_pms.http_retry, "get", get)

        assert _client().visible_to("user-token", []) == set()
        get.assert_not_called()


class TestRowPolicyVisible:
    """The per-person wrapper: never read for the owner, read once per title, and a failure means
    "pick as before" — never "this person can see nothing"."""

    def _policy(self, user_type, token="user-token"):

        ctx = MagicMock()
        ctx.token_for_user = lambda _user: token
        user = UserProfile(username="kid", plex_account_id=400, user_type=user_type)
        return RowPolicy(
            ctx=ctx,
            user=user,
            cfg=EngineConfig(),
            specs=[],
            library_index={},
            report=MagicMock(),
            resolve=lambda _w: None,
        )

    def test_the_owner_is_never_read(self):
        policy = self._policy(UserType.OWNER)

        assert policy.visible([1, 2]) is None
        policy.ctx.plex.visible_to.assert_not_called()

    def test_each_title_is_read_once_across_rows(self):
        policy = self._policy(UserType.MANAGED)
        policy.ctx.plex.visible_to.return_value = {1}

        assert policy.visible([1, 2]) == {1}
        assert policy.visible([2, 1]) == {1}
        policy.ctx.plex.visible_to.assert_called_once_with("user-token", [1, 2])

    def test_a_failed_read_picks_as_before_and_is_not_retried_for_every_row(self):
        policy = self._policy(UserType.SHARED)
        policy.ctx.plex.visible_to.side_effect = RuntimeError("HTTP 401")

        assert policy.visible([1]) is None
        assert policy.visible([2]) is None
        policy.ctx.plex.visible_to.assert_called_once()

    def test_no_token_picks_as_before(self):
        policy = self._policy(UserType.MANAGED, token=None)

        assert policy.visible([1]) is None
        policy.ctx.plex.visible_to.assert_not_called()


class TestAnyCopyTheyCanSee:
    """A title held in two libraries has two ratingKeys, and the pool keeps only one of them — whichever
    library was indexed last. Checking that copy alone drops the title for everyone who is shared only
    the other library (recorded: an unshared library's items read exactly like filtered ones)."""

    def _ctx(self):
        ctx = MagicMock()
        ctx.delivery_sections = [
            MagicMock(key=1, type="movie"),
            MagicMock(key=7, type="movie"),
            MagicMock(key=2, type="show"),
        ]
        ctx.section_index = {1: {900: 11, 901: 12}, 7: {900: 71, 901: 72}, 2: {900: 21}}
        return ctx

    def test_a_title_visible_in_either_library_is_kept(self):

        both = Candidate(tmdb_id=900, title="In both", year=2000, media_type=MediaType.MOVIE, rating_key=71)
        hidden = Candidate(tmdb_id=901, title="Hidden", year=2000, media_type=MediaType.MOVIE, rating_key=72)
        asked: list[list[int]] = []

        def visible(keys):
            asked.append(sorted(keys))
            return {11}

        kept, dropped = _visible_candidates(self._ctx(), [both, hidden], visible)

        assert [c.tmdb_id for c in kept] == [900]
        assert [c.tmdb_id for c in dropped] == [901]
        assert asked == [[11, 12, 71, 72]], "every movie copy is asked about, and never the TV key of the same tmdb id"

    def test_a_check_that_could_not_be_made_keeps_everything(self):

        c = Candidate(tmdb_id=900, title="x", year=2000, media_type=MediaType.MOVIE, rating_key=71)

        assert _visible_candidates(self._ctx(), [c], lambda _keys: None) == ([c], [])


class TestTheTokenIsFetchedOnce:
    def test_a_managed_account_does_not_mint_a_token_per_read(self):
        calls = []
        ctx = MagicMock()
        ctx.token_for_user = lambda _user: calls.append(1) or "user-token"
        ctx.plex.visible_to.side_effect = lambda _token, keys: set(keys)
        user = UserProfile(username="kid", plex_account_id=400, user_type=UserType.MANAGED)
        policy = RowPolicy(
            ctx=ctx,
            user=user,
            cfg=EngineConfig(),
            specs=[],
            library_index={},
            report=MagicMock(),
            resolve=lambda _w: None,
        )

        policy.visible([1])
        policy.visible([2])

        assert len(calls) == 1
