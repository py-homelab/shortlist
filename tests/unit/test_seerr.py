"""Overseerr/Jellyseerr client tests.

Response bodies come from `tests/fixtures/overseerr_*.json`, which are **recorded from a live Seerr
3.4.1** — each file's `_provenance` says exactly what was captured and what was sanitised. Per the
testing rules we assert the REQUEST payloads (the body fields the client is responsible for:
mediaType, mediaId, seasons, userId), not merely that a call happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from shortlist.engine.clients import seerr as seerr_mod
from shortlist.engine.clients.seerr import SeerrClient, SeerrError
from shortlist.engine.models import SeerrTarget

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

TARGET = SeerrTarget(url="http://overseerr.test", api_key="ok")
BASE = "http://overseerr.test/api/v1"


def _media_page() -> dict:
    return json.loads((FIXTURES / "overseerr_media_page.json").read_text())


def _fixture_ids() -> dict[int, tuple[str, int]]:
    """``{status: (media_type, tmdb_id)}`` from the recorded page — one row per status.

    Derived, never hard-coded: this fixture holds REAL ids from a real server, and re-recording it
    changes every one of them. A test that pins the numbers would have to be rewritten each time,
    which is how a fixture quietly stops being re-recorded.
    """
    return {r["status"]: (r["mediaType"], r["tmdbId"]) for r in _media_page()["results"]}


def _users_page() -> dict:
    return json.loads((FIXTURES / "overseerr_users_page.json").read_text())


def _client(**kwargs) -> SeerrClient:
    """A client whose throttle never sleeps — the rate limiter is tested by its own case."""
    return SeerrClient(kwargs.pop("target", TARGET), min_write_interval=0.0, **kwargs)


class TestPing:
    def test_ping_names_the_account_the_key_belongs_to(self):
        with respx.mock:
            route = respx.get(f"{BASE}/auth/me").mock(
                return_value=httpx.Response(200, json={"id": 1, "displayName": "serverowner"})
            )
            assert "serverowner" in _client().ping()
        assert route.calls[0].request.headers["X-Api-Key"] == "ok"

    def test_ping_uses_auth_me_not_the_unauthenticated_status_endpoint(self):
        """`/status` declares `security: []`, so it answers 200 to a wrong key.

        Testing against it would call a broken connection healthy — the single reason this client
        pings `/auth/me` instead, and the reason that choice is pinned by a test.
        """
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(401, json={"message": "denied"}))
            status = respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200, json={"version": "1.33.2"}))
            with pytest.raises(SeerrError, match="rejected the API key"):
                _client().ping()
        assert not status.called

    def test_a_non_json_body_says_to_check_the_url_and_proxy(self):
        """A 200 of HTML is a reverse proxy or SSO page, not the app — say which."""
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(200, text="<html>login</html>"))
            with pytest.raises(SeerrError, match="non-JSON body"):
                _client().ping()

    def test_a_403_blames_the_permission_not_the_key(self):
        """A working key whose account lacks Manage Requests. Calling that "rejected the API key"
        sends the owner off to regenerate a key that was never the problem — and the permission it
        is nearly always about is the one "Request as" needs."""
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(403, json={"message": "no"}))
            with pytest.raises(SeerrError, match="Manage Requests"):
                _client().ping()

    def test_a_401_still_blames_the_key(self):
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(401, json={"message": "no"}))
            with pytest.raises(SeerrError, match="rejected the API key"):
                _client().ping()

    def test_the_error_never_carries_the_api_key(self):
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(side_effect=httpx.ConnectError("nope"))
            with pytest.raises(SeerrError) as excinfo:
                _client().ping()
        assert "ok" not in str(excinfo.value).replace("Overseerr", "")


class TestMediaState:
    def test_maps_overseerr_status_codes_to_the_inbox_vocabulary(self):
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            state = _client().media_state()
        ids = _fixture_ids()

        def status_of(code: int) -> str | None:
            kind, tmdb_id = ids[code]
            return state[("movie" if kind == "movie" else "show", tmdb_id)]

        assert status_of(5) == "downloaded"  # AVAILABLE
        # PENDING gets its OWN word: the inbox renders "queued" as "Searching", which would dress up
        # "a person has not approved this yet" as the machine already working on it.
        assert status_of(2) == "awaiting_approval"
        # PROCESSING and PARTIALLY_AVAILABLE are NOT "downloading". Measured on the server this
        # fixture came from: 76 rows were PROCESSING and exactly ONE was moving — the rest are
        # approved-but-unreleased films and airing series, resting there indefinitely.
        assert status_of(3) == "queued"  # PROCESSING, nothing in the download client
        assert status_of(4) == "queued"  # PARTIALLY_AVAILABLE

    def test_downloadstatus_is_what_makes_a_title_downloading(self):
        """The only honest "moving right now" signal the API offers — it carries the download
        client's own sizeLeft/timeLeft, so a non-empty one is a fact rather than an inference."""
        rows = {
            "pageInfo": {"pages": 1},
            "results": [
                {"mediaType": "movie", "tmdbId": 1, "status": 3, "downloadStatus": []},
                {"mediaType": "movie", "tmdbId": 2, "status": 3, "downloadStatus": [{"sizeLeft": 42}]},
            ],
        }
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=rows))
            state = _client().media_state()
        assert state[("movie", 1)] == "queued"
        assert state[("movie", 2)] == "downloading"

    def test_a_deleted_or_unknown_row_is_not_known_so_the_title_stays_requestable(self):
        """DELETED and UNKNOWN say nothing is on its way.

        Treating them as "present" would make a title the server once held, and no longer has,
        permanently unrequestable — a hole in the library that nothing could ever fill. Between them
        these were 1,828 of the 5,000 sampled rows, so this is the common case, not an edge.

        DELETED is 7 here, which is documented NOWHERE — not in Overseerr's spec and not in the
        server's own shipped seerr-api.yml, both of which say 6 and stop. Only the running code says
        otherwise. Which is why the mapping is an allow-list: an unrecognised code means "unknown",
        and unknown means requestable.
        """
        ids = _fixture_ids()
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            state = _client().media_state()
        for code in (1, 7):
            kind, tmdb_id = ids[code]
            assert ("movie" if kind == "movie" else "show", tmdb_id) not in state

    def test_show_ids_are_keyed_by_tmdb_not_tvdb(self):
        """The reason this target needs no TVDB crossing at all.

        The fixture's Game of Thrones row carries BOTH ids (tmdb 1399, tvdb 121361); reading the
        wrong one would silently key the whole show side against a namespace the inbox never uses.
        """
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            state = _client().media_state()
        row = next(r for r in _media_page()["results"] if r["mediaType"] == "tv" and r.get("tvdbId"))
        assert ("show", row["tmdbId"]) in state
        assert ("show", row["tvdbId"]) not in state

    def test_pages_until_a_short_page(self):
        size = SeerrClient._PAGE_SIZE
        page_one = {
            "pageInfo": {"pages": 2},
            "results": [{"mediaType": "movie", "tmdbId": i, "status": 5} for i in range(size)],
        }
        page_two = {"pageInfo": {"pages": 2}, "results": [{"mediaType": "movie", "tmdbId": 999_999, "status": 5}]}
        with respx.mock:
            route = respx.get(f"{BASE}/media").mock(
                side_effect=[httpx.Response(200, json=page_one), httpx.Response(200, json=page_two)]
            )
            state = _client().media_state()
        assert len(state) == size + 1
        assert ("movie", 999_999) in state
        # Derived from the constant, not hard-coded: the page size is a measured value that has
        # already changed once (100 -> 1000 after walking a real 26,941-row library).
        assert route.calls[1].request.url.params["skip"] == str(size)

    def test_a_page_with_no_usable_mediatype_warns_rather_than_reading_as_an_empty_library(self):
        """The one shape that would fail silently.

        `mediaType` is undocumented in the published MediaInfo schema, so a fork dropping it would
        make every row unusable — and an unusable page is byte-identical to a healthy empty library.
        The run still fails open (a redundant request, never a wrong one), but it must say so.
        """
        rows = {"pageInfo": {"pages": 1}, "results": [{"tmdbId": 273481, "status": 5}]}
        # loguru does not route through stdlib logging, so `caplog` sees nothing — sink pattern.
        lines: list[str] = []
        sink = seerr_mod.logger.add(lines.append, level="WARNING", format="{message}")
        try:
            with respx.mock:
                respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=rows))
                state = _client().media_state()
        finally:
            seerr_mod.logger.remove(sink)
        assert state == {}
        assert any("mediaType" in line for line in lines)

    def test_state_is_fetched_once_per_client(self):
        """The run's reconcile and the send's presence check must not walk the library twice."""
        with respx.mock:
            route = respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=_media_page()))
            client = _client()
            client.media_state()
            client.media_state()
        assert route.call_count == 1


class TestUsers:
    def test_names_every_account_for_the_request_as_dropdown(self):
        with respx.mock:
            respx.get(f"{BASE}/user").mock(return_value=httpx.Response(200, json=_users_page()))
            users = _client().users()
        # `permissions` comes straight from the recorded fixture; the two auto-approve flags are
        # derived from it with the bits read off a live Seerr (ADMIN=2 implies every permission,
        # which is why the owner row approves without carrying an AUTO_APPROVE bit at all).
        assert users == [
            {
                "id": 1,
                "name": "serverowner",
                "plex_id": 111111,
                "auto_approve_movies": True,
                "auto_approve_tv": True,
                "is_plex_user": True,
            },
            {
                "id": 4,
                "name": "Shortlist",
                "plex_id": None,  # a local account: nobody on the server can sign in as it
                "auto_approve_movies": False,
                "auto_approve_tv": False,
                # userType 2 — a local account made inside Overseerr, not a person on the server.
                "is_plex_user": False,
            },
            {
                "id": 7,
                "name": "MooHouse",
                "plex_id": 222222,
                "auto_approve_movies": False,
                "auto_approve_tv": False,
                "is_plex_user": True,
            },
        ]

    def test_falls_back_through_the_documented_name_fields(self):
        """`displayName` is what the live API returns but is NOT in the published User schema."""
        payload = {
            "pageInfo": {"pages": 1},
            "results": [
                {"id": 2, "username": "local-user"},
                {"id": 3, "plexUsername": "plex-user"},
                {"id": 5, "email": "only@example.test"},
                {"id": 6},
            ],
        }
        with respx.mock:
            respx.get(f"{BASE}/user").mock(return_value=httpx.Response(200, json=payload))
            users = _client().users()
        assert [u["name"] for u in users] == ["local-user", "plex-user", "only@example.test", "User 6"]
        # No `permissions` at all reads as no auto-approval — the cautious direction, since the
        # screen uses it to promise whether a title starts downloading.
        assert all(not u["auto_approve_movies"] and not u["auto_approve_tv"] for u in users)


class TestFailingOpen:
    """What happens when the media walk cannot be done."""

    def test_the_status_endpoint_still_learns_the_walk_failed(self):
        """`media_state` must not swallow a failed walk, or a caller could not tell
        "Overseerr tracks none of these" from "Overseerr never answered"."""
        with respx.mock:
            respx.get(f"{BASE}/media").mock(side_effect=httpx.ConnectError("down"))
            client = _client()
            with pytest.raises(SeerrError):
                client.media_state()
            with pytest.raises(SeerrError):
                client.media_state()  # the memoised error, re-raised rather than re-fetched


class TestNaming:
    def test_ping_falls_back_through_the_name_fields_and_never_crashes_on_a_bare_reply(self):
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(return_value=httpx.Response(200, json={"id": 1}))
            assert _client().ping().endswith("?")

    def test_a_blank_display_name_does_not_win_over_a_real_username(self):
        """`or`-chaining already skipped "", but not "   " — which reads as a nameless account."""
        with respx.mock:
            respx.get(f"{BASE}/auth/me").mock(
                return_value=httpx.Response(200, json={"id": 1, "displayName": "   ", "username": "real"})
            )
            assert "real" in _client().ping()


class TestWhoAutoApproves:
    """Which accounts skip Overseerr's approval queue — bits read off a live Seerr 3.4.1."""

    def _users(self, *perms: int) -> list[dict]:
        rows = [{"id": i, "displayName": f"u{i}", "permissions": p} for i, p in enumerate(perms)]
        with respx.mock:
            respx.get(f"{BASE}/user").mock(
                return_value=httpx.Response(200, json={"pageInfo": {"pages": 1}, "results": rows})
            )
            return _client().users()

    def test_admin_approves_everything_without_any_auto_approve_bit(self):
        """The case that matters most: an owner's API key is an admin, and Overseerr's own permission
        check short-circuits on ADMIN — so it approves instantly while carrying none of the
        AUTO_APPROVE bits. Reading only those bits would label it "requests wait", which is the exact
        opposite of what it does."""
        [admin] = self._users(2)
        assert admin["auto_approve_movies"] and admin["auto_approve_tv"]

    def test_the_blanket_bit_covers_both_types(self):
        [u] = self._users(128 | 32)
        assert u["auto_approve_movies"] and u["auto_approve_tv"]

    def test_per_type_bits_are_read_separately(self):
        films, shows = self._users(256 | 32, 512 | 32)
        assert films["auto_approve_movies"] and not films["auto_approve_tv"]
        assert shows["auto_approve_tv"] and not shows["auto_approve_movies"]

    def test_a_plain_requester_approves_nothing(self):
        """32 = REQUEST, which is what every ordinary Plex user on a real instance carries."""
        [u] = self._users(32)
        assert not u["auto_approve_movies"] and not u["auto_approve_tv"]


class TestWhatTheReviewCaught:
    """Cases from the full-feature architecture review, each pinning a proved defect."""

    def _users(self, *rows: dict) -> list[dict]:
        with respx.mock:
            respx.get(f"{BASE}/user").mock(
                return_value=httpx.Response(
                    200, json={"pageInfo": {"pages": 1, "results": len(rows)}, "results": list(rows)}
                )
            )
            return _client().users()

    def test_manage_requests_auto_approves_even_without_an_auto_approve_bit(self):
        """Read from Overseerr's approval RULE, not its permission enum. `MediaRequest.js` on a live
        3.4.1: `hasPermission([AUTO_APPROVE, AUTO_APPROVE_MOVIE, MANAGE_REQUESTS], {or})` decides
        APPROVED vs PENDING. Omitting it told the owner such an account would hold their titles for
        review while it in fact sent them straight to Radarr."""
        [u] = self._users({"id": 1, "displayName": "mod", "permissions": 16 | 32})
        assert u["auto_approve_movies"] and u["auto_approve_tv"]

    def test_an_account_of_unknown_type_counts_as_a_person_and_is_hidden(self):
        """The picker offers only NON-people. Defaulting an unreadable `userType` to "not a person"
        would put every real account on the server back in the dropdown, and take the paragraph
        explaining their absence away with them."""
        rows = self._users(
            {"id": 1, "displayName": "no type at all", "permissions": 32},
            {"id": 2, "displayName": "odd type", "permissions": 32, "userType": 9},
            {"id": 3, "displayName": "local", "permissions": 32, "userType": 2},
        )
        assert [u["is_plex_user"] for u in rows] == [True, True, False]

    def test_a_server_that_caps_take_is_reported_rather_than_believed(self):
        """A capped page looks exactly like the end of the list — every row usable, nothing missing
        as far as anything downstream can tell — while `pageInfo` in the same payload says
        otherwise. 100 rows of 26,941, silently, was the failure."""
        page = {
            "pageInfo": {"pages": 27, "pageSize": 1000, "results": 3},
            "results": [{"mediaType": "movie", "tmdbId": 1, "status": 5}],
        }
        empty = {"pageInfo": {"pages": 27, "pageSize": 1000, "results": 3}, "results": []}
        lines: list[str] = []
        sink = seerr_mod.logger.add(lines.append, level="WARNING", format="{message}")
        try:
            with respx.mock:
                # One capped page, then nothing — while pageInfo in the same payload says 3.
                respx.get(f"{BASE}/media").mock(
                    side_effect=[httpx.Response(200, json=page), httpx.Response(200, json=empty)]
                )
                state = _client().media_state()
        finally:
            seerr_mod.logger.remove(sink)
        assert len(state) == 1
        assert any("of the 3 rows" in line for line in lines)

    def test_a_page_shorter_than_the_page_size_is_not_the_end_when_pageinfo_disagrees(self):
        """The bug in one line: the walk returned on a short batch BEFORE consulting pageInfo."""
        first = {
            "pageInfo": {"pages": 2, "results": 2},
            "results": [{"mediaType": "movie", "tmdbId": 1, "status": 5}],
        }
        second = {
            "pageInfo": {"pages": 2, "results": 2},
            "results": [{"mediaType": "movie", "tmdbId": 2, "status": 5}],
        }
        with respx.mock:
            respx.get(f"{BASE}/media").mock(
                side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
            )
            state = _client().media_state()
        assert state == {("movie", 1): "downloaded", ("movie", 2): "downloaded"}
