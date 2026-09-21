"""`/api/me` — a person's own picks page. Scoped to the signed-in account, actions only on their own
current set, every action and impression recorded, requests filed AS them with the *seerr."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import respx
from fastapi.testclient import TestClient

from shortlist.server.auth import SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Dismissal, PickEvent, RequestLog, User, UserSuggestion

SARAH = 555000100
MIKE = 555000200
SEERR = "http://overseerr.test"


def _sign_in_as(client: TestClient, account_id: int, role: str = "person") -> None:
    cookie = session_serializer(client.app.state.session_secret).dumps(
        {"account_id": account_id, "username": "x", "role": role}
    )
    client.cookies.set(SESSION_COOKIE, cookie)


def _seed(client: TestClient, account_id: int, titles: list[tuple[int, str, str, bool]]) -> None:
    with client.app.state.sessions() as session:
        user = session.query(User).filter(User.plex_account_id == account_id).one()
        for rank, (tmdb_id, media, title, kids) in enumerate(titles, start=1):
            session.add(
                UserSuggestion(
                    user_id=user.id,
                    rank=rank,
                    tmdb_id=tmdb_id,
                    media_type=media,
                    title=title,
                    year=2020,
                    genres=["Drama"] if not kids else ["Animation", "Family"],
                    rating=7.5,
                    vote_count=1000,
                    reason="Because you watched Fargo",
                    kids=kids,
                    sources="engine:e",
                    built_at=datetime.now(UTC),
                )
            )
        session.commit()


def _set_household(client: TestClient, account_id: int, label: str | None) -> None:
    with client.app.state.sessions() as session:
        user = session.query(User).filter(User.plex_account_id == account_id).one()
        user.household = None if label is None else {"label": label, "source": "engine"}
        session.commit()


def _configure_seerr(client: TestClient) -> None:
    r = client.put("/api/settings", json={"values": {"seerr.url": SEERR, "seerr.apikey": "k"}})
    assert r.status_code == 200, r.text


def _mock_seerr(*, sarah_seerr_id: int = 7, media: list[dict] | None = None, requests: list[dict] | None = None):
    respx.get(f"{SEERR}/api/v1/user").mock(
        return_value=httpx.Response(
            200,
            json={
                "pageInfo": {"pages": 1, "results": 2},
                "results": [
                    {"id": 1, "displayName": "owner", "plexId": 555000001, "permissions": 2, "userType": 1},
                    {"id": sarah_seerr_id, "displayName": "sarah", "plexId": SARAH, "permissions": 0, "userType": 1},
                ],
            },
        )
    )
    respx.get(f"{SEERR}/api/v1/media").mock(
        return_value=httpx.Response(
            200, json={"pageInfo": {"pages": 1, "results": len(media or [])}, "results": media or []}
        )
    )
    respx.get(f"{SEERR}/api/v1/request").mock(
        return_value=httpx.Response(
            200, json={"pageInfo": {"pages": 1, "results": len(requests or [])}, "results": requests or []}
        )
    )
    respx.get(f"{SEERR}/api/v1/user/{sarah_seerr_id}/quota").mock(
        return_value=httpx.Response(200, json={"movie": {"limit": 0}, "tv": {"limit": 5, "used": 1, "remaining": 4}})
    )


class TestReadingMyPicks:
    def test_a_person_sees_their_own_list_and_nothing_of_anyone_elses(self, client: TestClient):
        _seed(
            client, SARAH, [(10, "movie", "Ten", False), (20, "show", "Twenty", False), (30, "movie", "Cartoon", True)]
        )
        _seed(client, MIKE, [(99, "movie", "Mike's", False)])
        _set_household(client, SARAH, "family")
        _sign_in_as(client, SARAH)

        me = client.get("/api/me").json()
        assert me["state"] == "ok" and me["account_id"] == SARAH and me["role"] == "person"
        assert me["household"] == "family"
        assert me["counts"] == {"items": 3, "queued": 0, "hidden_available": 0, "family": 1}
        assert me["seerr"]["configured"] is False and me["seerr"]["linked"] is False

        body = client.get("/api/me/suggestions").json()
        assert [i["tmdb_id"] for i in body["items"]] == [10, 20]  # a family household: kids in their own lane
        assert body["has_family"] is True and body["family"] == "exclude"
        assert body["genres"] == [{"name": "Drama", "count": 2}]
        assert body["items"][0]["requestable"] is False
        assert body["items"][0]["reason_not_requestable"] == "no_seerr_user"

        assert [i["tmdb_id"] for i in client.get("/api/me/suggestions?family=only").json()["items"]] == [30]
        assert len(client.get("/api/me/suggestions?family=include").json()["items"]) == 3
        assert client.get("/api/me/suggestions?family=all").status_code == 422

    def test_anyone_else_sees_children_s_titles_with_everything_else_and_no_toggle(self, client: TestClient):
        _seed(client, SARAH, [(10, "movie", "Ten", False), (30, "movie", "Cartoon", True)])
        for household in (None, "adult"):
            _set_household(client, SARAH, household)
            _sign_in_as(client, SARAH)
            body = client.get("/api/me/suggestions").json()
            assert [i["tmdb_id"] for i in body["items"]] == [10, 30], household
            assert body["family"] == "include" and body["has_family"] is False
            assert body["household"] == household

    def test_a_childs_own_account_sees_only_childrens_titles(self, client: TestClient):
        """In step with their rows (`rows._family_means`): "auto" used to mean "everything" for a kids
        account here too, so a child's request page listed the household's grown-up missing titles."""
        _seed(client, SARAH, [(10, "movie", "Ten", False), (30, "movie", "Cartoon", True)])
        _set_household(client, SARAH, "kids")
        _sign_in_as(client, SARAH)

        body = client.get("/api/me/suggestions").json()

        assert [i["tmdb_id"] for i in body["items"]] == [30]
        assert (body["family"], body["household"]) == ("only", "kids")

    def test_a_childs_lane_is_not_a_preference(self, client: TestClient):
        """`?family=include` in the address bar does not widen it, and a grown-up title cannot be acted
        on from that account even though it sits in the household's list."""
        _seed(client, SARAH, [(10, "movie", "Ten", False), (30, "movie", "Cartoon", True)])
        _set_household(client, SARAH, "kids")
        _sign_in_as(client, SARAH)

        for asked in ("include", "exclude", "auto"):
            body = client.get(f"/api/me/suggestions?family={asked}").json()
            assert [i["tmdb_id"] for i in body["items"]] == [30], asked

        refused = client.post("/api/me/act", json={"tmdb_id": 10, "media_type": "movie", "action": "never"})
        allowed = client.post("/api/me/act", json={"tmdb_id": 30, "media_type": "movie", "action": "never"})
        assert (refused.status_code, allowed.status_code) == (403, 200)

    @respx.mock
    def test_a_childs_page_lists_no_grown_up_title_in_ANY_of_its_lists(self, client: TestClient):
        """The "already requested or on the way" posters are a second list, built beside the first. A
        pooled children's profile reads its household's suggestions, so every grown-up title anyone in
        the house had requested sat there with its poster and overview — one list below the one that
        had been narrowed."""
        _configure_seerr(client)
        _mock_seerr(media=[{"id": 2, "tmdbId": 20, "mediaType": "tv", "status": 2}])
        _seed(
            client,
            SARAH,
            [(10, "movie", "Adult", False), (20, "show", "Adult On The Way", False), (30, "movie", "Cartoon", True)],
        )
        _set_household(client, SARAH, "kids")
        _sign_in_as(client, SARAH)

        body = client.get("/api/me/suggestions").json()
        summary = client.get("/api/me").json()

        assert [i["title"] for i in body["items"]] == ["Cartoon"]
        assert body["queued"] == []
        assert (summary["counts"]["items"], summary["counts"]["queued"]) == (1, 0)

    @respx.mock
    def test_everyone_else_still_sees_what_is_on_the_way(self, client: TestClient):
        _configure_seerr(client)
        _mock_seerr(media=[{"id": 2, "tmdbId": 20, "mediaType": "tv", "status": 2}])
        _seed(client, SARAH, [(20, "show", "Adult On The Way", False), (30, "movie", "Cartoon", True)])
        _set_household(client, SARAH, "family")
        _sign_in_as(client, SARAH)

        assert [i["title"] for i in client.get("/api/me/suggestions").json()["queued"]] == ["Adult On The Way"]

    def test_the_owners_setting_applies_here_before_the_next_run_catches_up(self, client: TestClient):
        """The stored label is last night's. A profile just marked as a child's must not go on being
        shown the household's grown-up titles until a run happens to persist the new one."""
        _seed(client, SARAH, [(10, "movie", "Ten", False), (30, "movie", "Cartoon", True)])
        _set_household(client, SARAH, "family")  # what the last run stored
        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(plex_account_id=SARAH).one()
            user.prefs = {**(user.prefs or {}), "household": "kids"}
            session.commit()
        _sign_in_as(client, SARAH)

        body = client.get("/api/me/suggestions").json()

        assert [i["tmdb_id"] for i in body["items"]] == [30]
        assert body["household"] == "kids"

    def test_the_owner_sees_their_own_page_too(self, client: TestClient):
        with client.app.state.sessions() as session:
            session.add(User(plex_account_id=555000001, username="owner", slug="owner", user_type="owner"))
            session.commit()
        _seed(client, 555000001, [(5, "movie", "Five", False)])
        assert client.get("/api/me").json()["role"] == "owner"
        assert [i["tmdb_id"] for i in client.get("/api/me/suggestions").json()["items"]] == [5]

    def test_no_picks_yet_is_a_state_not_an_error(self, client: TestClient):
        _sign_in_as(client, MIKE)
        body = client.get("/api/me").json()
        assert body["state"] == "no_picks" and body["counts"]["items"] == 0

    def test_anonymous_and_strangers_are_refused(self, client: TestClient):
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/me").status_code == 401
        _sign_in_as(client, 999)
        assert client.get("/api/me").status_code == 403

    @respx.mock
    def test_the_seerr_decides_what_is_requestable(self, client: TestClient):
        _configure_seerr(client)
        _seed(
            client,
            SARAH,
            [
                (10, "movie", "Ten", False),
                (20, "show", "Twenty", False),
                (30, "movie", "There", False),
                (40, "movie", "Mine", False),
            ],
        )
        _mock_seerr(
            media=[
                {"id": 1, "tmdbId": 30, "mediaType": "movie", "status": 5},  # available -> hidden
                {"id": 2, "tmdbId": 20, "mediaType": "tv", "status": 2},  # someone asked -> on the way
            ],
            requests=[{"id": 77, "status": 1, "media": {"tmdbId": 40, "mediaType": "movie", "status": 2}}],
        )
        _sign_in_as(client, SARAH)

        body = client.get("/api/me/suggestions").json()
        assert body["seerr"]["linked"] is True and body["seerr"]["user_id"] == 7
        assert body["seerr"]["quota"]["tv"] == {
            "limit": 5,
            "used": 1,
            "days": None,
            "remaining": 4,
            "restricted": False,
        }
        assert [i["tmdb_id"] for i in body["items"]] == [10]
        assert body["items"][0]["requestable"] is True
        queued = {i["tmdb_id"]: i for i in body["queued"]}
        assert queued[20]["reason_not_requestable"] == "on_the_way"
        assert queued[40]["reason_not_requestable"] == "requested" and queued[40]["seerr"]["request_id"] == 77
        assert body["hidden_available"] == 1


class TestActing:
    def _events(self, client: TestClient) -> list[tuple]:
        with client.app.state.sessions() as session:
            return [
                (e.event, e.tmdb_id, e.surface, e.position, e.source)
                for e in session.query(PickEvent).order_by(PickEvent.id)
            ]

    def test_never_later_skip_undo_and_what_they_leave_behind(self, client: TestClient):
        _seed(
            client, SARAH, [(10, "movie", "Ten", False), (20, "show", "Twenty", False), (30, "movie", "Thirty", False)]
        )
        _sign_in_as(client, SARAH)

        r = client.post(
            "/api/me/act",
            json={"action": "never", "tmdb_id": 10, "media_type": "movie", "surface": "deck", "position": 0},
        )
        assert r.status_code == 200 and r.json()["dismissed"] == {"kind": "never", "until": None}
        r = client.post("/api/me/act", json={"action": "later", "tmdb_id": 20, "media_type": "show", "surface": "grid"})
        assert r.json()["dismissed"]["kind"] == "later" and r.json()["dismissed"]["until"] is not None
        r = client.post(
            "/api/me/act",
            json={"action": "skip", "tmdb_id": 30, "media_type": "movie", "surface": "deck", "position": 2},
        )
        assert r.json()["ok"] is True and r.json()["skipped"] is True

        # never and later are gone from the deck; a skip leaves the title in place.
        assert [i["tmdb_id"] for i in client.get("/api/me/suggestions").json()["items"]] == [30]
        dismissed = client.get("/api/me/dismissed").json()
        assert [d["tmdb_id"] for d in dismissed["never"]] == [10] and [d["tmdb_id"] for d in dismissed["later"]] == [20]

        # Undo the newest (the later), then the never by name; a skip's undo records only the event.
        r = client.post("/api/me/act", json={"action": "undo", "last": True})
        assert r.json()["restored"]["tmdb_id"] == 20
        r = client.post("/api/me/act", json={"action": "undo", "tmdb_id": 10, "media_type": "movie"})
        assert r.json()["restored"]["kind"] == "never"
        r = client.post("/api/me/act", json={"action": "undo", "tmdb_id": 30, "media_type": "movie", "undone": "skip"})
        assert r.json()["ok"] is True and r.json()["restored"] is None
        assert [i["tmdb_id"] for i in client.get("/api/me/suggestions").json()["items"]] == [10, 20, 30]

        assert self._events(client) == [
            ("never", 10, "deck", 0, "engine:e"),
            ("later", 20, "grid", None, "engine:e"),
            ("skip", 30, "deck", 2, "engine:e"),
            ("undo", 20, None, None, ""),
            ("undo", 10, None, None, ""),
            ("undo", 30, None, None, ""),
        ]
        with client.app.state.sessions() as session:
            assert session.query(Dismissal).count() == 0

    def test_a_title_outside_their_current_set_cannot_be_acted_on(self, client: TestClient):
        _seed(client, SARAH, [(10, "movie", "Ten", False)])
        _seed(client, MIKE, [(99, "movie", "Mike's", False)])
        _sign_in_as(client, SARAH)
        r = client.post("/api/me/act", json={"action": "never", "tmdb_id": 99, "media_type": "movie"})
        assert r.status_code == 403
        assert self._events(client) == []

    def test_a_lapsed_later_comes_back_on_its_own(self, client: TestClient):
        _seed(client, SARAH, [(10, "movie", "Ten", False)])
        _sign_in_as(client, SARAH)
        client.post("/api/me/act", json={"action": "later", "tmdb_id": 10, "media_type": "movie"})
        with client.app.state.sessions() as session:
            row = session.query(Dismissal).one()
            row.until = datetime.now(UTC) - timedelta(days=1)
            session.commit()
        assert [i["tmdb_id"] for i in client.get("/api/me/suggestions").json()["items"]] == [10]

    def test_bad_bodies_are_422_and_unknown_fields_refused(self, client: TestClient):
        _sign_in_as(client, SARAH)
        assert (
            client.post("/api/me/act", json={"action": "eat", "tmdb_id": 1, "media_type": "movie"}).status_code == 422
        )
        assert (
            client.post("/api/me/act", json={"action": "skip", "tmdb_id": 1, "media_type": "movie", "x": 1}).status_code
            == 422
        )
        assert (
            client.post("/api/me/act", json={"action": "skip"}).status_code == 403
        )  # nothing named -> not in their set

    @respx.mock
    def test_requesting_files_as_the_person_with_the_first_season_and_records_it(self, client: TestClient):
        _configure_seerr(client)
        _seed(client, SARAH, [(10, "movie", "Ten", False), (20, "show", "Twenty", False)])
        _mock_seerr()
        respx.get(f"{SEERR}/api/v1/tv/20").mock(
            return_value=httpx.Response(
                200, json={"seasons": [{"seasonNumber": 0}, {"seasonNumber": 2}, {"seasonNumber": 1}]}
            )
        )
        posted = respx.post(f"{SEERR}/api/v1/request").mock(
            return_value=httpx.Response(201, json={"id": 501, "status": 1})
        )
        _sign_in_as(client, SARAH)

        r = client.post(
            "/api/me/act",
            json={"action": "request", "tmdb_id": 20, "media_type": "show", "surface": "deck", "position": 1},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "code": None, "message": None, "status": "pending", "request_id": 501}
        request = posted.calls[0].request
        assert request.headers["X-API-User"] == "7"  # filed AS sarah, never as the key's account
        assert (
            httpx.Request("POST", SEERR, json={"mediaType": "tv", "mediaId": 20, "seasons": [1]}).content
            == request.content
        )
        with client.app.state.sessions() as session:
            log = session.query(RequestLog).one()
            assert (log.tmdb_id, log.seerr_user_id, log.seerr_request_id, log.seerr_status) == (20, 7, 501, "pending")
        assert self._events(client)[-1] == ("request", 20, "deck", 1, "engine:e")

    @respx.mock
    def test_the_seerrs_refusals_are_passed_on_in_plain_words(self, client: TestClient):
        _configure_seerr(client)
        _seed(client, SARAH, [(10, "movie", "Ten", False)])
        _mock_seerr()
        respx.post(f"{SEERR}/api/v1/request").mock(
            return_value=httpx.Response(403, json={"message": "Movie Quota exceeded"})
        )
        _sign_in_as(client, SARAH)
        r = client.post("/api/me/act", json={"action": "request", "tmdb_id": 10, "media_type": "movie"})
        assert r.status_code == 200 and r.json()["ok"] is False and r.json()["code"] == "quota"
        with client.app.state.sessions() as session:
            assert session.query(RequestLog).count() == 0

    def test_without_a_seerr_account_a_request_is_refused_before_anything_is_sent(self, client: TestClient):
        _seed(client, SARAH, [(10, "movie", "Ten", False)])
        _sign_in_as(client, SARAH)
        r = client.post("/api/me/act", json={"action": "request", "tmdb_id": 10, "media_type": "movie"})
        assert r.json()["ok"] is False and r.json()["code"] == "no_seerr_user"


class TestImpressions:
    def test_shown_is_recorded_once_per_title_surface_and_day_for_their_own_set_only(self, client: TestClient):
        _seed(client, SARAH, [(10, "movie", "Ten", False), (20, "show", "Twenty", False)])
        _sign_in_as(client, SARAH)
        items = [
            {"tmdb_id": 10, "media_type": "movie", "surface": "deck", "position": 0},
            {"tmdb_id": 20, "media_type": "show", "surface": "grid", "position": 3},
            {"tmdb_id": 99, "media_type": "movie", "surface": "deck", "position": 1},  # not theirs: dropped
        ]
        assert client.post("/api/me/seen", json={"items": items}).json() == {"ok": True, "logged": 2}
        assert client.post("/api/me/seen", json={"items": items}).json() == {"ok": True, "logged": 0}
        with client.app.state.sessions() as session:
            rows = session.query(PickEvent).filter(PickEvent.event == "shown").all()
            assert sorted((r.tmdb_id, r.surface, r.position) for r in rows) == [(10, "deck", 0), (20, "grid", 3)]
            assert all(r.shown_day == datetime.now(UTC).strftime("%Y-%m-%d") for r in rows)

    def test_a_malformed_batch_is_422(self, client: TestClient):
        _sign_in_as(client, SARAH)
        assert (
            client.post(
                "/api/me/seen", json={"items": [{"tmdb_id": 1, "media_type": "movie", "surface": "tv"}]}
            ).status_code
            == 422
        )
        assert client.post("/api/me/seen", json={"items": "no"}).status_code == 422
