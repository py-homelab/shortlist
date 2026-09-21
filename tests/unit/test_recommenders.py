"""The recommender boundary: the built-in engine behind it, an external engine over HTTP, the
fallback between them, and what the row build does with an engine's final order."""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar
from unittest.mock import MagicMock

import httpx
import pytest
import respx

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine import ranking
from shortlist.engine.clients import http_retry
from shortlist.engine.clients.engine_http import EngineClient, EngineError, recommend_payload
from shortlist.engine.context import EngineContext
from shortlist.engine.models import EngineConfig, MediaType, Pick, RowSpec, Seed
from shortlist.engine.picker import reason_for
from shortlist.engine.recommender import RecommendRequest, RecommendResult, engine_status
from shortlist.engine.recommenders import BuiltinRecommender, FallbackRecommender, HttpRecommender
from shortlist.engine.recommenders.builtin import is_kids
from tests.conftest import MemorySnapshotStore, make_candidate, make_profile, make_watched, plextv_user


class FakeEngineClient:
    """An `EngineClient` that answers from a canned body and remembers what it was asked."""

    def __init__(
        self,
        items: list[dict] | Exception,
        *,
        name: str = "fake-engine",
        trace: dict | None = None,
        household: dict | None = None,
    ):
        self.items = items
        self.name = name
        self.trace = trace
        self.household = household
        self.payloads: list[dict] = []

    def recommend(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if isinstance(self.items, Exception):
            raise self.items
        body = {"engine": {"name": self.name, "version": "0.1"}, "ordered": True, "items": self.items}
        if self.trace is not None:
            body["trace"] = self.trace
        if self.household is not None:
            body["household"] = self.household
        return body

    def info(self) -> dict:
        return {"name": self.name, "version": "0.1", "surfaces": ["library"], "serves_cold": False, "ready": True}


def _item(
    tmdb_id: int, title: str, *, media="movie", rating=7.0, reason=None, kids=False, seed=None, genres=(), year=2020
):
    return {
        "tmdb_id": tmdb_id,
        "media_type": media,
        "title": title,
        "year": year,
        "genres": list(genres),
        "rating": rating,
        "vote_count": 1000,
        "reason": reason,
        "kids": kids,
        "seed": seed,
    }


@pytest.fixture
def ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    """The pipeline test's context: one movie library holding watched 900 and candidates 10, 20, 30."""
    plex = MagicMock()
    movie_section = MagicMock()
    movie_section.type = "movie"
    movie_section.title = "Movies"
    plex.sections.return_value = [movie_section]
    plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section}
    movie_section.collections.return_value = []
    plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020, 30: 1030}
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = []
    plex.stored_label.side_effect = lambda collection, label, *, extra=None: label.replace("shortlist", "Shortlist", 1)
    plex.fetch_items.side_effect = lambda keys: ([MagicMock(ratingKey=k, title=f"item{k}", guids=[]) for k in keys], [])
    plex.top_rated.return_value = [(30, MagicMock(ratingKey=1030, title="Top Rated"))]
    history = MagicMock()
    history.fetch.return_value = [make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)]
    mock_tmdb.suggestions.return_value = [
        ({"id": 10, "title": "Candidate Ten", "genre_ids": [], "vote_average": 8.0}, 1.0),
        ({"id": 20, "title": "Candidate Twenty", "genre_ids": [], "vote_average": 7.0}, 1.0),
    ]
    mock_tmdb.genre_names.return_value = {}
    mock_plextv.update_user_filters.side_effect = lambda account_id, fields: None
    return EngineContext(
        config=engine_config,
        plex=plex,
        plextv=mock_plextv,
        tmdb=mock_tmdb,
        history_source=history,
        curator=mock_curator,
        snapshots=MemorySnapshotStore(),
    )


def _run(ctx: EngineContext, mock_plextv, rows: list[RowSpec] | None = None):
    ctx.config.rows = rows or [RowSpec(slug="picked", name_template="Picked", size=5)]
    mock_plextv.users = [plextv_user(100, "sarah")]
    return pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]


class TestBuiltinRecommender:
    def test_is_the_default_engine_and_ranks_as_before(self, ctx, mock_plextv):
        assert isinstance(ctx.recommender, BuiltinRecommender)
        assert ctx.recommender.name == "builtin"
        assert ctx.recommender.serves_cold is False

        report = _run(ctx, mock_plextv)

        # Rating 8.0 outranks 7.0 under the built-in score — the pre-plugin order, untouched.
        assert [p.tmdb_id for p in report.picks] == [10, 20]
        assert report.picks[0].reason.startswith("Because you watched")

    def test_answers_a_scored_pool_not_a_final_order(self, ctx):
        req = RecommendRequest(
            user=make_profile("sarah"),
            seeds=[Seed(tmdb_id=900, title="Fargo", media_type=MediaType.MOVIE)],
            history=[],
            library_index={MediaType.MOVIE: {10: 1010, 20: 1020}, MediaType.SHOW: {}},
            watched_exclusions=None,
            excluded_genres=set(),
            media="movie",
            limit_per_media=10,
            sources=("tmdb_similar",),
        )
        result = BuiltinRecommender().recommend(ctx, req)
        assert result.ordered is False
        assert [c.tmdb_id for c in result.gathered] == [10, 20]
        assert [c.tmdb_id for c in result.ranked] == [10, 20]
        assert all(c.external_rank is None for c in result.ranked)

    def test_tags_children_titles_from_genres(self):
        assert is_kids(["Animation", "Family"])
        assert is_kids(["Kids", "Comedy"])
        assert not is_kids(["Animation", "Comedy"])  # adult animation
        assert not is_kids(["Family", "Drama"])


class TestHttpRecommender:
    def test_the_engines_order_is_the_rows_order(self, ctx, mock_plextv):
        # The engine ranks 20 (rating 7) above 10 (rating 8): the row must follow the engine, not the score.
        client = FakeEngineClient(
            [_item(20, "Twenty", rating=7.0, reason="Because the engine says so"), _item(10, "Ten", rating=8.0)]
        )
        ctx.recommender = HttpRecommender(client, name="fake-engine")

        report = _run(ctx, mock_plextv)

        assert [p.tmdb_id for p in report.picks] == [20, 10]
        assert report.picks[0].reason == "Because the engine says so"
        assert report.picks[1].reason == "Chosen for you by your recommendation engine"
        assert report.picks[0].sources == ["engine:fake-engine"]
        [gather] = report.trace["gathers"]
        [source] = gather["sources"]
        assert source["source"] == "engine:fake-engine"
        assert source["status"] == "ok"
        assert source["contributed"] == 2
        assert source["disposition"] == {"kept": 2}

    def test_sends_seeds_history_library_and_exclusions(self, ctx, mock_plextv):
        client = FakeEngineClient([_item(10, "Ten")])
        ctx.recommender = HttpRecommender(client)

        _run(ctx, mock_plextv)

        # The library surface for the rows, then the missing surface for their request page.
        payload, missing = client.payloads
        assert missing["surface"] == "missing" and missing["plex_account_id"] == 100
        assert payload["protocol"] == 1
        assert payload["plex_account_id"] == 100
        assert payload["surface"] == "library"
        assert payload["media"] == ["movie", "show"]  # the default row is media="both"
        assert payload["limit_per_media"] == ctx.config.candidates_pre_rank
        assert payload["library"] == {"movie": [10, 20, 30, 900], "show": []}
        assert [s["tmdb_id"] for s in payload["seeds"]] == [900]
        assert payload["history"] and all(h["tmdb_id"] == 900 for h in payload["history"])
        assert [900, "movie"] in payload["exclude"]  # watched, so the engine is told not to offer it

    def test_shortlists_rules_still_apply_to_what_comes_back(self, ctx, mock_plextv):
        # Not in the library (99), watched (900), excluded genre (Horror): all dropped, order kept.
        client = FakeEngineClient(
            [
                _item(99, "Not Here"),
                _item(900, "Fargo"),
                _item(20, "Twenty"),
                _item(30, "Scary", genres=("Horror",)),
                _item(10, "Ten"),
            ]
        )
        ctx.recommender = HttpRecommender(client, name="e")

        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5)]
        mock_plextv.users = [plextv_user(100, "sarah")]
        profile = make_profile("sarah", account_id=100, excluded_genres={"horror"})
        report = pipeline_mod.run(ctx, [profile]).users[0]

        assert [p.tmdb_id for p in report.picks] == [20, 10]
        [source] = report.trace["gathers"][0]["sources"]
        assert source["disposition"] == {
            "kept": 2,
            "not_in_your_libraries": 1,
            "already_watched": 1,
            "excluded_genre": 1,
        }

    def test_cuts_per_media_type_in_engine_order(self, ctx):
        items = [_item(i, f"T{i}") for i in range(1, 8)] + [_item(50 + i, f"S{i}", media="show") for i in range(1, 4)]
        client = FakeEngineClient(items, name="e")
        req = RecommendRequest(
            user=make_profile("sarah"),
            seeds=[],
            history=[],
            library_index={
                MediaType.MOVIE: {i: 1000 + i for i in range(1, 8)},
                MediaType.SHOW: {50 + i: 2000 + i for i in range(1, 4)},
            },
            watched_exclusions=set(),
            excluded_genres=set(),
            media="both",
            limit_per_media=3,
        )
        result = HttpRecommender(client).recommend(ctx, req)
        assert result.ordered is True
        assert [c.tmdb_id for c in result.ranked] == [1, 2, 3, 51, 52, 53]
        assert [c.external_rank for c in result.ranked] == [0, 1, 2, 3, 4, 5]
        assert len(result.in_library) == 10
        assert result.gathered == []

    def test_a_seed_named_by_the_engine_becomes_the_picks_seed(self, ctx, mock_plextv):
        client = FakeEngineClient([_item(10, "Ten", seed={"tmdb_id": 900, "title": "Fargo", "media_type": "movie"})])
        ctx.recommender = HttpRecommender(client)

        report = _run(ctx, mock_plextv, [RowSpec(slug="bw", name_template="Because you watched {top_seed}", size=5)])

        assert report.picks[0].seed_title == "Fargo"
        assert report.picks[0].seed_tmdb_id == 900

    def test_malformed_items_are_skipped_not_fatal(self, ctx, mock_plextv):
        client = FakeEngineClient([{"nonsense": True}, "not a dict", _item(10, "Ten")])
        ctx.recommender = HttpRecommender(client)
        report = _run(ctx, mock_plextv)
        assert [p.tmdb_id for p in report.picks] == [10]

    def test_the_engines_own_trace_rides_along(self, ctx, mock_plextv):
        client = FakeEngineClient([_item(10, "Ten")], trace={"built_at": 123})
        ctx.recommender = HttpRecommender(client)
        report = _run(ctx, mock_plextv)
        assert report.trace["gathers"][0]["engine"] == {"built_at": 123}


class TestMissingSurface:
    def test_the_builtin_ranks_what_no_library_holds_from_the_same_gather(self, ctx, mock_plextv):
        # 10 and 20 are in the library; 99 is not. One gather serves both surfaces (suggestions once).
        ctx.tmdb.suggestions.return_value = [
            ({"id": 10, "title": "Ten", "genre_ids": [], "vote_average": 8.0}, 1.0),
            ({"id": 99, "title": "Not Here", "genre_ids": [], "vote_average": 7.0}, 1.0),
            ({"id": 98, "title": "Nor This", "genre_ids": [], "vote_average": 9.0}, 1.0),
        ]
        report = _run(ctx, mock_plextv)
        assert [p.tmdb_id for p in report.picks] == [10]
        assert [m["tmdb_id"] for m in report.missing] == [98, 99]  # by score, best first
        assert report.missing[0]["rank"] == 1
        assert report.missing[0]["reason"].startswith("Because you watched")
        assert report.missing[0]["seed_tmdb_id"] == 900
        assert ctx.tmdb.suggestions.call_count == 1

    def test_the_http_engine_is_asked_for_the_missing_surface_and_its_order_kept(self, ctx, mock_plextv):
        items = [_item(20, "Twenty"), _item(99, "Not Here", reason="Trust me"), _item(98, "Nor")]
        client = FakeEngineClient(items, name="e")
        ctx.recommender = HttpRecommender(client, name="e")
        report = _run(ctx, mock_plextv)
        assert [m["tmdb_id"] for m in report.missing] == [99, 98]  # 20 is in the library: never a request
        assert report.missing[0]["reason"] == "Trust me"
        assert report.missing[0]["sources"] == ["engine:e"]

    def test_a_failing_missing_call_leaves_the_surface_empty_not_the_run_failed(self, ctx, mock_plextv):
        calls = {"n": 0}

        class Flaky(FakeEngineClient):
            def recommend(self, payload):
                calls["n"] += 1
                if payload["surface"] == "missing":
                    raise EngineError("engine unreachable")
                return super().recommend(payload)

        ctx.recommender = HttpRecommender(Flaky([_item(20, "Twenty")], name="e"))
        report = _run(ctx, mock_plextv)
        assert report.status == "ok" and [p.tmdb_id for p in report.picks] == [20]
        assert report.missing == []

    def test_a_cold_person_has_no_missing_surface(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        report = _run(ctx, mock_plextv)
        assert report.status == "cold_start" and report.missing == []


class TestFallbackRecommender:
    def test_a_failing_engine_falls_back_to_the_builtin_and_says_so(self, ctx, mock_plextv):
        client = FakeEngineClient(EngineError("engine unreachable (ConnectError)"), name="e")
        ctx.recommender = FallbackRecommender(HttpRecommender(client, name="e"), BuiltinRecommender())

        report = _run(ctx, mock_plextv)

        assert [p.tmdb_id for p in report.picks] == [10, 20]  # the built-in ranking
        assert report.status == "ok"
        sources = report.trace["gathers"][0]["sources"]
        assert sources[0]["source"] == "engine:e"
        assert sources[0]["status"] == "failed"
        assert "fell back to builtin" in sources[0]["detail"]
        assert "unreachable" in sources[0]["detail"]

    def test_an_engine_answering_nothing_falls_back_too(self, ctx, mock_plextv):
        ctx.recommender = FallbackRecommender(HttpRecommender(FakeEngineClient([], name="e")), BuiltinRecommender())
        report = _run(ctx, mock_plextv)
        assert [p.tmdb_id for p in report.picks] == [10, 20]
        assert report.trace["gathers"][0]["sources"][0]["status"] == "failed"

    def test_a_working_engine_is_left_alone(self, ctx, mock_plextv):
        ctx.recommender = FallbackRecommender(
            HttpRecommender(FakeEngineClient([_item(20, "Twenty")], name="e")), BuiltinRecommender()
        )
        report = _run(ctx, mock_plextv)
        assert [p.tmdb_id for p in report.picks] == [20]
        assert report.trace["gathers"][0]["sources"][0]["status"] == "ok"

    def test_without_a_fallback_the_row_keeps_what_it_has(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient(EngineError("engine unreachable"), name="e"))
        report = _run(ctx, mock_plextv)
        # Every pool failed — the same failed-person outcome as every source being down.
        assert report.status == "error"
        assert "unreachable" in (report.error or "")
        assert report.picks == []


class TestEngineStatus:
    """What the run report says about the external engine — the dashboard alert is raised from it."""

    def test_the_builtin_engine_reports_nothing(self):
        assert engine_status(BuiltinRecommender()) is None

    def test_a_healthy_engine_is_no_trouble(self):
        info = {"name": "e", "version": "0.3.1", "stale": False, "age_hours": 3.0, "last_build_ok": True}
        status = engine_status(
            FallbackRecommender(HttpRecommender(FakeEngineClient([]), name="e", info=info), BuiltinRecommender())
        )
        assert status["trouble"] is None and status["fell_back"] == 0
        assert status["info"]["age_hours"] == 3.0

    def test_stale_lists_and_the_failed_build_behind_them_are_said(self):
        info = {
            "stale": True,
            "age_hours": 60.2,
            "last_build_ok": False,
            "last_build_error": "OperationalError: disk I/O",
        }
        status = engine_status(HttpRecommender(FakeEngineClient([]), name="e", info=info))
        assert "60.2 hours old" in status["trouble"] and "disk I/O" in status["trouble"]

    def test_a_failed_build_with_lists_still_fresh_is_said(self):
        info = {"stale": False, "age_hours": 26.0, "last_build_ok": False, "last_build_error": "boom"}
        status = engine_status(HttpRecommender(FakeEngineClient([]), name="e", info=info))
        assert "failed its last build (boom)" in status["trouble"]

    def test_an_unreachable_engine_is_said(self):
        status = engine_status(HttpRecommender(FakeEngineClient([]), name="e", info={"unreachable": "ConnectError"}))
        assert "could not be reached" in status["trouble"]

    def test_people_who_fell_back_are_counted_with_the_reason(self, ctx, mock_plextv):
        client = FakeEngineClient(EngineError("engine unreachable (ConnectError)"), name="e")
        ctx.recommender = FallbackRecommender(HttpRecommender(client, name="e", info={}), BuiltinRecommender())

        _run(ctx, mock_plextv)
        status = engine_status(ctx.recommender)

        assert status["fell_back"] == 1
        assert "1 person got Shortlist's own picks" in status["trouble"]
        assert "unreachable" in status["fallback_reasons"][0]

    def test_the_run_report_carries_it(self, ctx, mock_plextv):
        info = {"stale": True, "age_hours": 60.0}
        ctx.recommender = HttpRecommender(FakeEngineClient([_item(20, "Twenty")], name="e"), name="e", info=info)
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5)]
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        assert report.engine["name"] == "e" and "60.0 hours old" in report.engine["trouble"]


class TestColdStartWithAnEngine:
    def test_the_builtin_never_sees_a_thin_history(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]  # 1 < min_history 3
        report = _run(ctx, mock_plextv)
        assert report.status == "cold_start"
        assert [p.title for p in report.picks] == ["Top Rated"]
        assert not ctx.tmdb.suggestions.called

    def test_an_engine_that_serves_cold_is_asked_first(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        client = FakeEngineClient([_item(20, "Twenty")], name="e")
        ctx.recommender = HttpRecommender(client, name="e", serves_cold=True)

        report = _run(ctx, mock_plextv)

        assert report.status == "ok"
        assert [p.tmdb_id for p in report.picks] == [20]
        assert [p["surface"] for p in client.payloads] == ["library", "missing"]

    def test_an_engine_that_serves_cold_but_answers_nothing_yields_a_cold_start(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        ctx.recommender = HttpRecommender(FakeEngineClient([], name="e"), name="e", serves_cold=True)

        report = _run(ctx, mock_plextv)

        assert report.status == "cold_start"
        assert [p.title for p in report.picks] == ["Top Rated"]

    def test_a_row_that_skips_cold_start_still_skips_on_the_late_path(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        ctx.recommender = HttpRecommender(FakeEngineClient([], name="e"), name="e", serves_cold=True)

        report = _run(ctx, mock_plextv, [RowSpec(slug="picked", name_template="Picked", size=5, cold_start="skip")])

        assert report.status == "cold_start"
        assert report.picks == []
        assert "Not enough watch history" in (report.reason or "")

    def test_skip_means_no_row_for_a_thin_history_even_when_the_engine_would_serve_one(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        client = FakeEngineClient([_item(20, "Twenty"), _item(777, "Not Here Yet")], name="e")
        ctx.recommender = HttpRecommender(client, name="e", serves_cold=True)

        report = _run(ctx, mock_plextv, [RowSpec(slug="picked", name_template="Picked", size=5, cold_start="skip")])

        assert report.status == "cold_start"
        assert report.picks == []
        # No row was asked for — but their request page still gets the engine's suggestions (the
        # titles no library holds).
        assert [p["surface"] for p in client.payloads] == ["missing"]
        assert [m["tmdb_id"] for m in report.missing] == [777]

    def test_the_server_wide_skip_applies_to_every_row(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        ctx.config.cold_start = "skip"
        ctx.recommender = HttpRecommender(FakeEngineClient([_item(20, "Twenty")], name="e"), name="e", serves_cold=True)
        report = _run(
            ctx,
            mock_plextv,
            [RowSpec(slug="a", name_template="A", size=5), RowSpec(slug="b", name_template="B", size=5)],
        )
        assert report.status == "cold_start" and report.picks == []

    def test_a_row_left_on_popular_still_goes_to_the_engine(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        ctx.recommender = HttpRecommender(FakeEngineClient([_item(20, "Twenty")], name="e"), name="e", serves_cold=True)
        report = _run(
            ctx,
            mock_plextv,
            [
                RowSpec(slug="skipped", name_template="Skipped", size=5, cold_start="skip"),
                RowSpec(slug="kept", name_template="Kept", size=5, cold_start="popular"),
            ],
        )
        assert _rows(report) == {"kept": [20]}

    def test_an_engine_that_does_not_serve_cold_is_not_asked(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        client = FakeEngineClient([_item(20, "Twenty")], name="e")
        ctx.recommender = HttpRecommender(client, name="e", serves_cold=False)
        report = _run(ctx, mock_plextv)
        assert report.status == "cold_start"
        assert client.payloads == []


class TestRowsDrawWithoutReplacement:
    """An engine answers one ordered list per pool; a person's rows sharing it must not repeat it."""

    def test_each_engine_row_takes_the_next_titles_not_the_same_head(self, ctx, mock_plextv):
        client = FakeEngineClient([_item(10, "Ten"), _item(20, "Twenty"), _item(30, "Thirty")], name="e")
        ctx.recommender = HttpRecommender(client, name="e")

        report = _run(
            ctx,
            mock_plextv,
            [
                RowSpec(slug="first", name_template="First", size=1),
                RowSpec(slug="second", name_template="Second", size=1),
                RowSpec(slug="third", name_template="Third", size=5),
            ],
        )

        by_row = {}
        for p in report.picks:
            by_row.setdefault(p.collection_slug, []).append(p.tmdb_id)
        assert by_row == {"first": [10], "second": [20], "third": [30]}

    def test_the_builtin_engine_keeps_its_upstream_behaviour(self, ctx, mock_plextv):
        ctx.recommender = BuiltinRecommender()

        report = _run(
            ctx,
            mock_plextv,
            [
                RowSpec(slug="first", name_template="First", size=5),
                RowSpec(slug="second", name_template="Second", size=5),
            ],
        )

        by_row = {}
        for p in report.picks:
            by_row.setdefault(p.collection_slug, []).append(p.tmdb_id)
        assert by_row["first"] == by_row["second"] == [10, 20]


def _rows(report) -> dict[str, list[int]]:
    by_row: dict[str, list[int]] = {}
    for p in report.picks:
        by_row.setdefault(p.collection_slug, []).append(p.tmdb_id)
    return by_row


class TestRowSettingsUnderAnEngine:
    """Every row setting in the admin keeps its meaning when an external engine ranks."""

    def test_a_row_that_narrows_its_seeds_asks_the_engine_to_focus(self, ctx, mock_plextv):
        client = FakeEngineClient([_item(10, "Ten"), _item(20, "Twenty")], name="e")
        ctx.recommender = HttpRecommender(client, name="e")
        _run(
            ctx,
            mock_plextv,
            [
                RowSpec(slug="picked", name_template="Picked", size=1),
                RowSpec(slug="byw", name_template="Because you watched {top_seed}", size=1, max_seeds=1),
            ],
        )
        library = [p for p in client.payloads if p["surface"] == "library"]
        assert sorted(p["seed_focus"] for p in library) == [False, True]
        focused = next(p for p in library if p["seed_focus"])
        assert len(focused["seeds"]) == 1

    def test_a_rows_own_release_date_weight_reorders_the_engines_answer(self, ctx, mock_plextv):
        items = [_item(10, "Old", year=1980), _item(20, "New", year=2026)]
        ctx.recommender = HttpRecommender(FakeEngineClient(items, name="e"), name="e")
        newer = _run(ctx, mock_plextv, [RowSpec(slug="newer", name_template="Newer", size=2, recency=1.0)])
        assert [p.tmdb_id for p in newer.picks] == [20, 10]

    def test_without_a_row_weight_the_engines_order_stands(self, ctx, mock_plextv):
        items = [_item(10, "Old", year=1980), _item(20, "New", year=2026)]
        ctx.recommender = HttpRecommender(FakeEngineClient(items, name="e"), name="e")
        plain = _run(ctx, mock_plextv, [RowSpec(slug="plain", name_template="Plain", size=2)])
        assert [p.tmdb_id for p in plain.picks] == [10, 20]

    def test_reweigh_external_multiplies_position_by_the_release_date_factor(self):
        old = make_candidate(10, "Old", year=1980)
        new = make_candidate(20, "New", year=2026)
        old.external_rank, new.external_rank = 0, 1
        assert [c.tmdb_id for c in ranking.reweigh_external([old, new], 1.0, 2026)] == [20, 10]
        assert [c.tmdb_id for c in ranking.reweigh_external([old, new], 0.0, 2026)] == [10, 20]

    def test_a_row_naming_its_own_sources_is_built_by_shortlists_engine(self, ctx, mock_plextv):
        client = FakeEngineClient([_item(30, "Thirty")], name="e")
        ctx.recommender = FallbackRecommender(HttpRecommender(client, name="e"), BuiltinRecommender())
        report = _run(
            ctx,
            mock_plextv,
            [
                RowSpec(slug="engine", name_template="Engine", size=5),
                RowSpec(slug="own", name_template="Own", size=5, candidate_sources=list(ctx.config.candidate_sources)),
            ],
        )
        rows = _rows(report)
        assert rows["engine"] == [30]
        assert rows["own"] == [10, 20]  # the built-in ranking, although its sources equal the default
        assert len([p for p in client.payloads if p["surface"] == "library"]) == 1

    def test_a_rewatch_rows_taste_comes_from_the_watches_behind_the_engines_titles(self):
        from shortlist.engine import rows as rows_mod

        c = make_candidate(10, "Ten")
        c.seeds = [Seed(tmdb_id=900, title="Fargo", media_type=MediaType.MOVIE)]
        pools = MagicMock(gathered=[], ranked=[c])
        assert rows_mod._taste(pools) == {(900, MediaType.MOVIE)}
        pools = MagicMock(gathered=[make_candidate(5, "Five")], ranked=[c])
        assert rows_mod._taste(pools) == {(5, MediaType.MOVIE)}


class TestCarriedPicksAndTheFamilyRule:
    """A carried pick holds no children's flag of its own and the engine's classification can change
    under it. Found live: King of the Hill (TV-14) stayed in a family row after the engine stopped
    calling it a children's title. Re-checked against tonight's answer — but only a positive one."""

    def _prior(self, ctx, *tmdb_ids: int, row: str = "fam") -> None:
        # The fixture's section is a MagicMock; carry-forward is keyed on the section key as a STRING.
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", row, "1"): [
                Pick(
                    tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE
                )
                for i, t in enumerate(tmdb_ids)
            ]
        }

    def _engine(self, ctx, *items: dict) -> None:
        ctx.recommender = HttpRecommender(FakeEngineClient(list(items), name="e"), name="e")

    @staticmethod
    def _kid(tmdb_id: int, title: str) -> dict:
        return _item(tmdb_id, title, kids=True, genres=("Animation", "Family"))

    @staticmethod
    def _grown_up(tmdb_id: int, title: str) -> dict:
        return _item(tmdb_id, title, kids=False, genres=("Drama",))

    def _row(self, **kw) -> RowSpec:
        kw.setdefault("refresh_days", 0)  # frozen by default: carry-forward is what these test
        kw.setdefault("size", 5)
        return RowSpec(slug=kw.pop("slug", "fam"), name_template="Family", **kw)

    def test_a_family_row_drops_a_pick_the_engine_no_longer_calls_a_childrens_title(self, ctx, mock_plextv):
        self._prior(ctx, 10, 20)  # 20 was a children's title when it was delivered
        self._engine(ctx, self._kid(10, "Ten"), self._grown_up(20, "Twenty"))

        report = _run(ctx, mock_plextv, [self._row(family="only")])

        assert [p.tmdb_id for p in report.picks] == [10]
        selection = report.trace["selection"][0]
        assert selection["family_dropped"] == 1  # the run trace can say why the row lost it

    def test_absence_from_tonights_pool_is_not_an_answer(self, ctx, mock_plextv):
        """The pool is truncated and narrowed by the person's other rows, so a children's title is
        missing from it most nights. Dropping on absence would rebuild a frozen row every night."""
        self._prior(ctx, 10, 20)
        self._engine(ctx, self._kid(10, "Ten"))  # 20 simply is not in tonight's answer

        report = _run(ctx, mock_plextv, [self._row(family="only")])

        assert {p.tmdb_id for p in report.picks} == {10, 20}
        assert "family_dropped" not in report.trace["selection"][0]

    def test_an_unclassified_title_is_not_a_contradiction_either(self, ctx, mock_plextv):
        """An engine that omits `kids`, or a genre lookup that failed, leaves a candidate unclassified.
        "Not marked as a children's title" is not the same answer as "is not one"."""
        self._prior(ctx, 10, 20)
        self._engine(ctx, self._kid(10, "Ten"), _item(20, "Twenty"))  # no genres, no kids flag

        report = _run(ctx, mock_plextv, [self._row(family="only")])

        assert {p.tmdb_id for p in report.picks} == {10, 20}

    def test_a_rebuild_night_also_lets_go_of_a_pick_tonights_answer_does_not_place_in_the_row(self, ctx, mock_plextv):
        """The engine answers a bounded list, so a long-stale pick can fall out of it entirely — no
        contradiction, no confirmation. On a night the row is rewritten anyway, it goes."""
        self._prior(ctx, 10, 20)
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(ctx, mock_plextv, [self._row(family="only", refresh_days=1)])  # rebuilds nightly

        assert [p.tmdb_id for p in report.picks] == [10]
        # Its own trace key: "tonight's answer refuses it" and "tonight's answer does not mention it"
        # are different facts, and the run page should not report the second as the first.
        assert report.trace["selection"][0]["unconfirmed_dropped"] == 1
        assert "family_dropped" not in report.trace["selection"][0]

    def test_an_exclude_row_drops_a_carried_pick_now_classed_as_a_childrens_title(self, ctx, mock_plextv):
        self._prior(ctx, 10, 20)
        self._engine(ctx, self._grown_up(10, "Ten"), self._kid(20, "Twenty"))
        report = _run(ctx, mock_plextv, [self._row(family="exclude")])
        assert [p.tmdb_id for p in report.picks] == [10]

    def test_auto_re_checks_only_for_a_family_household(self, ctx, mock_plextv):
        for household, expected in ((FAMILY, [10]), (ADULT, [10, 20])):  # module constants, defined below
            self._prior(ctx, 10, 20)
            ctx.recommender = HttpRecommender(
                FakeEngineClient([self._grown_up(10, "Ten"), self._kid(20, "Twenty")], name="e", household=household),
                name="e",
            )
            report = _run(ctx, mock_plextv, [self._row(family="auto")])
            assert sorted(p.tmdb_id for p in report.picks) == expected, household

    def test_with_no_household_an_auto_row_keeps_what_it_carried(self, ctx, mock_plextv):
        """Deliberate, and the same answer `_family_admits` gives a fresh candidate: nothing could
        label this person, so "children's titles belong elsewhere" has nobody to apply to."""
        self._prior(ctx, 10, 20)
        self._engine(ctx, self._grown_up(10, "Ten"), self._kid(20, "Twenty"))
        report = _run(ctx, mock_plextv, [self._row(family="auto")])
        assert sorted(p.tmdb_id for p in report.picks) == [10, 20]

    def _classify_finished(self, ctx, kids: bool) -> None:
        """What the finished title (tmdb 900, the fixture's watch) looks up as on TMDB."""
        ctx.tmdb.genre_names.return_value = {16: "Animation", 10751: "Family", 18: "Drama"}
        ctx.tmdb.genre_ids_for.side_effect = lambda tmdb_id, kind: [16, 10751] if kids else [18]

    def test_a_rewatch_row_leads_with_a_finished_title_the_rule_admits(self, ctx, mock_plextv):
        """A rewatch row leads with what they finished, which never went through the pool's filter — so
        a "children's titles only" row served whatever they last rewatched. It must still LEAD with the
        finished titles the rule does admit, or the fix is just an empty row."""
        self._classify_finished(ctx, kids=True)
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(ctx, mock_plextv, [self._row(family="only", rewatch=True, watched_pct=1.0)])

        assert 900 in [p.tmdb_id for p in report.picks]  # the finished children's title, kept
        assert report.trace["selection"][0]["rewatches"] == 1

    def test_a_rewatch_row_leaves_out_a_finished_title_the_rule_refuses(self, ctx, mock_plextv):
        self._classify_finished(ctx, kids=False)  # Fargo is a drama
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(ctx, mock_plextv, [self._row(family="only", rewatch=True, watched_pct=1.0)])

        assert [p.tmdb_id for p in report.picks] == [10]
        assert report.trace["selection"][0]["rewatches"] == 0  # counted after the rule, not before

    def test_a_carried_rewatch_pick_the_rule_refuses_is_dropped_too(self, ctx, mock_plextv):
        """A rewatch row's pool holds no finished title, so the pool can never contradict one. The
        titles tonight's rule refused are carried back for exactly this."""
        self._prior(ctx, 900, 10)  # 900 is the finished title, delivered when it counted as children's
        self._classify_finished(ctx, kids=False)
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(ctx, mock_plextv, [self._row(family="only", rewatch=True, watched_pct=1.0)])

        assert [p.tmdb_id for p in report.picks] == [10]
        assert report.trace["selection"][0]["family_dropped"] == 1

    def test_a_carried_pick_is_asked_about_directly_not_left_to_the_lead_scan(self, ctx, mock_plextv):
        """The lead scan stops once the row has its leads, and it takes the person's OLDEST finished
        titles first. A stale carried pick among their recent watches is never reached by it — so
        whether the row let go of one used to depend on where it happened to sit in that order."""
        ctx.plex.build_library_index.return_value = {900: 999, 10: 1010, 30: 1030, 40: 1040, 50: 1050}
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),  # most recent, so the scan reaches it last
            make_watched("Cartoon A", days_ago=40, rating_key=1030, tmdb_id=30),
            make_watched("Cartoon B", days_ago=41, rating_key=1040, tmdb_id=40),
            make_watched("Cartoon C", days_ago=42, rating_key=1050, tmdb_id=50),
        ]
        # Fargo is a drama; the cartoons are children's titles and fill the scan's budget before it.
        ctx.tmdb.genre_names.return_value = {16: "Animation", 10751: "Family", 18: "Drama"}
        ctx.tmdb.genre_ids_for.side_effect = lambda tmdb_id, kind: [18] if tmdb_id == 900 else [16, 10751]
        self._prior(ctx, 900)
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(ctx, mock_plextv, [self._row(family="only", rewatch=True, watched_pct=1.0, size=1)])

        assert 900 not in [p.tmdb_id for p in report.picks], [p.title for p in report.picks]
        assert report.trace["selection"][0]["family_dropped"] == 1

    def test_a_genre_map_that_came_back_empty_is_not_an_answer(self, ctx, mock_plextv):
        """TMDB caches a 404 genre map as {} for a week. Reading that as "not a children's title"
        dropped a real one out of the row that exists to hold it."""
        ctx.tmdb.genre_names.return_value = {}  # nothing resolves
        ctx.tmdb.genre_ids_for.side_effect = lambda tmdb_id, kind: [16, 10751]
        self._prior(ctx, 900, 10)
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(ctx, mock_plextv, [self._row(family="only", rewatch=True, watched_pct=1.0)])

        assert 900 in [p.tmdb_id for p in report.picks]  # left alone, not refused on unreadable data
        assert "family_dropped" not in report.trace["selection"][0]

    def test_a_cold_only_row_is_left_short_rather_than_padded_with_unvetted_titles(self, ctx, mock_plextv):
        """A cold start has no history to classify from, and the server's top-rated titles carry no
        classification at all — so they must not fill a children's-titles-ONLY row."""
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]  # thin: cold start
        ctx.recommender = BuiltinRecommender()

        report = _run(ctx, mock_plextv, [self._row(family="only", cold_start="popular")])

        assert [p.title for p in report.picks] == []

    def test_a_cold_exclude_row_still_fills(self, ctx, mock_plextv):
        """`exclude` admits an unclassified title, exactly as it does for a fresh candidate — so the
        ordinary adult setting must not turn every cold-start person's row empty."""
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        ctx.recommender = BuiltinRecommender()

        report = _run(ctx, mock_plextv, [self._row(family="exclude", cold_start="popular")])

        assert [p.title for p in report.picks] == ["Top Rated"]

    def test_a_held_row_says_in_the_trace_that_it_is_knowingly_stale(self, ctx, mock_plextv):
        """The hold abandons the section; without a trace entry the run page shows the row as simply
        absent, when in fact it is keeping yesterday's picks on purpose."""
        ctx.tmdb.genre_names.side_effect = RuntimeError("tmdb is down")
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(ctx, mock_plextv, [self._row(family="only", rewatch=True, watched_pct=1.0)])

        assert report.picks == []
        assert report.trace["selection"][0]["decision"] == "held_unbuilt"

    def test_an_unreadable_genre_lookup_holds_the_row_rather_than_emptying_it(self, ctx, mock_plextv):
        """The hold exists so a TMDB hiccup cannot decide who sees a children's title. Before this,
        the rebuild-night drop ran first and the row disappeared with nothing in the trace."""
        self._prior(ctx, 10, 20)
        ctx.tmdb.genre_names.side_effect = RuntimeError("tmdb is down")
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(ctx, mock_plextv, [self._row(family="only", rewatch=True, watched_pct=1.0, refresh_days=1)])

        assert {p.tmdb_id for p in report.picks} == {10, 20}  # last night's row, held
        assert report.trace["selection"][0]["decision"] == "carried_forward"

    def test_one_rows_lookup_failure_does_not_freeze_this_persons_other_rewatch_rows(self, ctx, mock_plextv):
        """The children's-title lookup has its own flag. Sharing the excluded-genre one meant a TMDB
        hiccup inside a family row held every rewatch row the person had, including the ones that asked
        no children's-title question — and someone with no excluded genres could not reach that hold
        at all before."""
        ctx.tmdb.genre_names.side_effect = RuntimeError("tmdb is down")
        self._engine(ctx, self._kid(10, "Ten"))

        report = _run(
            ctx,
            mock_plextv,
            [
                self._row(slug="fam", family="only", rewatch=True, watched_pct=1.0),
                RowSpec(slug="rew", name_template="Rewatch", size=5, rewatch=True, watched_pct=1.0),
            ],
        )

        assert _rows(report).get("rew"), "the unconstrained rewatch row asked nothing and must still build"

    def test_a_title_an_earlier_row_took_is_not_missing_from_tonights_answer(self, ctx, mock_plextv):
        """Draw-without-replacement narrows what a row may SHOW, not what the answer contained. Reading
        the narrowed pool made an earlier row's picks look unconfirmed and shrank the family row."""
        self._prior(ctx, 10, 20)
        self._engine(ctx, self._kid(10, "Ten"), self._kid(20, "Twenty"), self._kid(30, "Thirty"))

        report = _run(
            ctx,
            mock_plextv,
            [
                RowSpec(slug="first", name_template="First", size=2),
                self._row(slug="fam", family="only", size=2, refresh_days=1),
            ],
        )

        rows = _rows(report)
        assert rows["first"] == [10, 20]
        assert len(rows["fam"]) == 2, rows  # a full row, not one shrunk by what the first row took
        assert "unconfirmed_dropped" not in report.trace["selection"][1]

    def test_a_settings_change_does_not_hide_what_the_rule_took(self, ctx, mock_plextv):
        """Found live: a row whose recipe changed logged four titles dropped by the children's-title
        rule while its trace reported none, because the settings-change branch cleared the counters.
        Both are true at once — the rule ran first, and `carried: 0` says the rebuild took the rest."""
        self._prior(ctx, 10, 20)
        ctx.previous_recipes = {("sarah", "fam", "1"): "a different recipe than tonight's"}
        self._engine(ctx, self._kid(10, "Ten"), self._grown_up(20, "Twenty"))

        report = _run(ctx, mock_plextv, [self._row(family="only")])

        selection = report.trace["selection"][0]
        assert selection["decision"] == "settings_changed"
        assert selection["family_dropped"] == 1
        assert selection["carried"] == 0

    def test_a_settings_change_does_not_hide_an_unconfirmed_drop_either(self, ctx, mock_plextv):
        """The removed line cleared BOTH counters, so the other one needs the same row: a rebuild
        night, a recipe change, and a carried pick tonight's answer does not mention."""
        self._prior(ctx, 10, 20)
        ctx.previous_recipes = {("sarah", "fam", "1"): "a different recipe than tonight's"}
        self._engine(ctx, self._kid(10, "Ten"))  # 20 is simply absent tonight

        report = _run(ctx, mock_plextv, [self._row(family="only", refresh_days=1)])

        selection = report.trace["selection"][0]
        assert selection["decision"] == "settings_changed"
        assert selection["unconfirmed_dropped"] == 1
        assert selection["carried"] == 0

    def test_an_unconstrained_row_keeps_what_it_carried(self, ctx, mock_plextv):
        """The common case: no family rule in play, so nothing is re-checked and nothing churns."""
        self._prior(ctx, 10, 20, row="picked")
        self._engine(ctx, _item(10, "Ten"))
        report = _run(ctx, mock_plextv, [RowSpec(slug="picked", name_template="Picked", size=5, refresh_days=0)])
        assert {p.tmdb_id for p in report.picks} == {10, 20}


class TestFamilyRows:
    def test_exclude_keeps_children_titles_out_and_only_keeps_nothing_else(self, ctx, mock_plextv):
        client = FakeEngineClient([_item(10, "Ten", kids=True), _item(20, "Twenty"), _item(30, "Thirty", kids=True)])
        ctx.recommender = HttpRecommender(client)

        report = _run(
            ctx,
            mock_plextv,
            [
                RowSpec(slug="grown", name_template="Grown-ups", size=5, family="exclude"),
                RowSpec(slug="fam", name_template="Family", size=5, family="only"),
            ],
        )

        by_row = {}
        for p in report.picks:
            by_row.setdefault(p.collection_slug, []).append(p.tmdb_id)
        assert by_row == {"grown": [20], "fam": [10, 30]}

    def test_the_builtin_tags_kids_from_genres_too(self, ctx, mock_plextv):
        ctx.tmdb.genre_names.return_value = {16: "Animation", 10751: "Family", 18: "Drama"}
        ctx.tmdb.suggestions.return_value = [
            ({"id": 10, "title": "Cartoon", "genre_ids": [16, 10751], "vote_average": 8.0}, 1.0),
            ({"id": 20, "title": "Drama", "genre_ids": [18], "vote_average": 7.0}, 1.0),
        ]
        report = _run(ctx, mock_plextv, [RowSpec(slug="grown", name_template="Grown-ups", size=5, family="exclude")])
        assert [p.tmdb_id for p in report.picks] == [20]

    def test_recipe_changes_only_when_set_away_from_the_default(self, ctx, mock_plextv):
        from shortlist.engine import rows as rows_mod

        policy = MagicMock()
        policy.user.blocked_seeds = set()
        policy.effective_sources.return_value = ("tmdb_similar",)
        policy.effective_recency.return_value = 0.0
        policy.effective_watched_pct.return_value = 0.0
        policy.cfg = ctx.config
        plain = rows_mod.row_recipe(policy, RowSpec(slug="r", name_template="", size=5))
        include = rows_mod.row_recipe(policy, RowSpec(slug="r", name_template="", size=5, family="include"))
        only = rows_mod.row_recipe(policy, RowSpec(slug="r", name_template="", size=5, family="only"))
        assert plain == include
        assert "family=only" in only and "family" not in plain


class TestExternalOrderInRanking:
    def test_sort_key_orders_externals_by_rank_only(self):
        a = make_candidate(1, "A", rating=9.0, external_rank=3)
        b = make_candidate(2, "B", rating=1.0, external_rank=0)
        assert sorted([a, b], key=lambda c: ranking._sort_key(c)) == [b, a]

    def test_pre_rank_and_diversify_keep_an_external_order(self):
        seed = Seed(tmdb_id=1, title="S", media_type=MediaType.MOVIE)
        pool = [
            make_candidate(i, f"C{i}", rating=float(i), seeds=[seed], external_rank=i, sources={"engine:e"})
            for i in range(6)
        ]
        assert [c.tmdb_id for c in ranking.pre_rank(pool, 4)] == [0, 1, 2, 3]
        assert [c.tmdb_id for c in ranking.diversify_by_seed(pool, 3)] == [0, 1, 2]

    def test_picker_prefers_the_engines_reason(self):
        c = make_candidate(1, "A", reason="Because it knows", external_rank=0)
        assert reason_for(c) == "Because it knows"
        assert (
            reason_for(make_candidate(1, "A", seeds=[], external_rank=0))
            == "Chosen for you by your recommendation engine"
        )
        assert reason_for(make_candidate(1, "A", seeds=[])) == "Matched to your taste"


class TestEngineClient:
    URL = "http://engine.test:8090"

    @respx.mock
    def test_recommend_posts_the_payload_with_the_bearer_token(self):
        route = respx.post(f"{self.URL}/v1/recommend").mock(
            return_value=httpx.Response(200, json={"engine": {"name": "x"}, "items": [_item(10, "Ten")]})
        )
        body = EngineClient(self.URL, token="s3cret").recommend({"protocol": 1, "plex_account_id": 1})
        assert body["items"][0]["tmdb_id"] == 10
        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer s3cret"
        assert httpx.Request("POST", self.URL, json={"protocol": 1, "plex_account_id": 1}).content == request.content

    @respx.mock
    def test_a_rejected_token_is_reported_without_the_token(self):
        respx.post(f"{self.URL}/v1/recommend").mock(return_value=httpx.Response(401))
        with pytest.raises(EngineError) as e:
            EngineClient(self.URL, token="s3cret").recommend({})
        assert "s3cret" not in str(e.value)
        assert "token" in str(e.value)

    @respx.mock
    def test_an_unreachable_engine_is_an_engine_error(self, monkeypatch):
        monkeypatch.setattr(http_retry, "_backoff", lambda attempt, base, cap: 0.0)
        respx.post(f"{self.URL}/v1/recommend").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(EngineError, match="unreachable"):
            EngineClient(self.URL).recommend({})

    @respx.mock
    def test_a_malformed_answer_is_an_engine_error(self):
        respx.post(f"{self.URL}/v1/recommend").mock(return_value=httpx.Response(200, json={"nope": 1}))
        with pytest.raises(EngineError, match="items"):
            EngineClient(self.URL).recommend({})

    @respx.mock
    def test_info(self):
        respx.get(f"{self.URL}/v1/info").mock(return_value=httpx.Response(200, json={"name": "x", "serves_cold": True}))
        assert EngineClient(self.URL).info()["serves_cold"] is True
        respx.get(f"{self.URL}/v1/info").mock(return_value=httpx.Response(200, json={"hello": 1}))
        with pytest.raises(EngineError, match="name"):
            EngineClient(self.URL).info()

    def test_payload_serialises_dates_and_the_none_exclusion_sentinel(self):
        watched = make_watched("Fargo", days_ago=2, tmdb_id=900)
        from datetime import UTC, datetime

        from shortlist.engine.models import WatchedItem

        undated = WatchedItem(
            title="Old", media_type=MediaType.MOVIE, watched_at=datetime.fromtimestamp(0, UTC), tmdb_id=901
        )
        req = RecommendRequest(
            user=make_profile("sarah", history=[watched, undated, make_watched("No id")]),
            seeds=[Seed(tmdb_id=900, title="Fargo", media_type=MediaType.MOVIE, weight=0.5)],
            history=[watched, undated],
            library_index={MediaType.MOVIE: {10: 1010}, MediaType.SHOW: {}},
            watched_exclusions=None,
            excluded_genres={"Horror"},
            media="both",
        )
        payload = recommend_payload(req, run_day=0)
        assert payload["exclude"] == [[900, "movie"]]  # None -> the seeds
        assert [h["tmdb_id"] for h in payload["history"]] == [900, 901]
        assert payload["history"][0]["watched_at"] is not None
        assert payload["history"][1]["watched_at"] is None
        assert payload["seeds"] == [{"tmdb_id": 900, "media_type": "movie", "title": "Fargo", "weight": 0.5}]
        assert payload["library"] == {"movie": [10], "show": []}
        assert payload["excluded_genres"] == ["Horror"]
        assert payload["media"] == ["movie", "show"]


def test_result_defaults():
    r = RecommendResult(ranked=[])
    assert r.in_library == [] and r.gathered == [] and r.ordered is False and r.stats.trace == {}


FAMILY = {"label": "family", "kids_titles": 66, "window_titles": 187, "window_days": 365}
ADULT = {"label": "adult", "kids_titles": 1, "window_titles": 94, "window_days": 365}


class TestHouseholds:
    """The family split follows each person's own viewing: an "auto" row drops children's titles for a
    family household only, and a family-only row exists only for family households."""

    ROWS: ClassVar[list[RowSpec]] = [
        RowSpec(slug="picked", name_template="Picked", size=5, family="auto"),
        RowSpec(slug="fam", name_template="Family", size=5, family="only"),
    ]

    def _items(self):
        return [_item(10, "Cartoon", kids=True), _item(20, "Drama"), _item(30, "Anime", kids=True)]

    def _by_row(self, report):
        out = {}
        for p in report.picks:
            out.setdefault(p.collection_slug, []).append(p.tmdb_id)
        return out

    def test_a_family_household_gets_a_grown_up_row_and_a_family_row(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient(self._items(), household=FAMILY))
        report = _run(ctx, mock_plextv, self.ROWS)
        assert self._by_row(report) == {"picked": [20], "fam": [10, 30]}
        assert report.household == {
            "label": "family",
            "source": "engine",
            "kids_titles": 66,
            "window_titles": 187,
            "window_days": 365,
            "engine_label": "family",
        }

    def test_an_adult_keeps_children_s_titles_and_has_no_family_row(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient(self._items(), household=ADULT))
        report = _run(ctx, mock_plextv, self.ROWS)
        assert self._by_row(report) == {"picked": [10, 20, 30]}
        assert report.rows_considered["fam"] == "not_a_family_household"

    def test_shortlist_s_thresholds_decide_not_the_engine_s_label(self, ctx, mock_plextv):
        # The engine says adult; at a 1% family threshold Shortlist says family.
        ctx.config.family_min_share = 0.01
        ctx.config.family_min_kids_titles = 1
        ctx.recommender = HttpRecommender(FakeEngineClient(self._items(), household=ADULT))
        report = _run(ctx, mock_plextv, self.ROWS)
        assert report.household["label"] == "family" and report.household["engine_label"] == "adult"
        assert self._by_row(report) == {"picked": [20], "fam": [10, 30]}

    def test_the_owner_s_override_wins(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient(self._items(), household=ADULT))
        ctx.config.rows = self.ROWS
        mock_plextv.users = [plextv_user(100, "sarah")]
        profile = make_profile("sarah", account_id=100, household_override="family")
        report = pipeline_mod.run(ctx, [profile]).users[0]
        assert report.household["label"] == "family" and report.household["source"] == "override"
        assert self._by_row(report) == {"picked": [20], "fam": [10, 30]}

    def test_with_nothing_to_go_on_nobody_is_labelled_and_nothing_is_filtered(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient(self._items()))  # no household reported
        report = _run(ctx, mock_plextv, self.ROWS)
        # Nothing was ESTABLISHED tonight, so nothing is reported — the run is persisted over what the
        # last good run found, and "unknown" written there would erase it.
        assert report.household is None
        # Nothing filtered: the personal row keeps the children's titles too, and the family row, which
        # draws after it from the same engine list, finds them already on this person's Home.
        assert self._by_row(report) == {"picked": [10, 20, 30]}
        assert report.rows_considered["fam"] == "due"


class TestAKidsAccount:
    """A child's own profile. "auto" rows hold ONLY children's titles — they used to admit everything,
    which handed a child five rows of the household's adult taste for Plex's parental filter to hide —
    and the two family-SPLIT settings come down, each for a reason of its own."""

    KIDS: ClassVar[dict] = {"label": "kids", "kids_titles": 90, "window_titles": 100, "window_days": 365}
    ROWS: ClassVar[list[RowSpec]] = [
        RowSpec(slug="picked", name_template="Picked", size=5, family="auto"),
        RowSpec(slug="fam", name_template="Family", size=5, family="only"),
        RowSpec(slug="grownups", name_template="Grown-ups", size=5, family="exclude"),
        RowSpec(slug="all", name_template="Everything", size=5, family="include"),
    ]

    def _items(self):
        return [_item(10, "Cartoon", kids=True), _item(20, "Drama"), _item(30, "Anime", kids=True)]

    def _by_row(self, report):
        out = {}
        for p in report.picks:
            out.setdefault(p.collection_slug, []).append(p.tmdb_id)
        return out

    def _pinned(self, ctx, mock_plextv, rows, household=None):
        """The real case: counts say one thing (a pooled household's say "family" for every profile in
        it), and the owner's per-person pin says this profile is the children's."""
        ctx.recommender = HttpRecommender(FakeEngineClient(self._items(), household=household or FAMILY))
        ctx.config.rows = rows
        mock_plextv.users = [plextv_user(100, "kids-tv")]
        profile = make_profile("kids-tv", account_id=100, household_override="kids")
        return pipeline_mod.run(ctx, [profile]).users[0]

    def test_an_auto_row_holds_only_childrens_titles(self, ctx, mock_plextv):
        report = self._pinned(ctx, mock_plextv, self.ROWS[:1])

        assert self._by_row(report) == {"picked": [10, 30]}
        assert report.household["label"] == "kids" and report.household["source"] == "override"

    def test_the_same_holds_when_the_engines_counts_say_kids_with_no_pin(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient(self._items(), household=self.KIDS))

        report = _run(ctx, mock_plextv, self.ROWS[:1])

        assert self._by_row(report) == {"picked": [10, 30]}
        assert report.household["source"] == "engine"

    def test_the_family_shelf_comes_down_and_says_why(self, ctx, mock_plextv):
        """Deliberate, not inherited from "not a family household": its own rows ARE the children's
        shelf, so a family row is a sixth row of the same titles from the same answer."""
        report = self._pinned(ctx, mock_plextv, self.ROWS[:2])

        assert "fam" not in self._by_row(report)
        assert report.rows_considered["fam"] == "kids_rows_are_already_childrens_titles"

    def test_the_grown_ups_half_of_the_split_comes_down_too(self, ctx, mock_plextv):
        """An `exclude` row on a kids account could only ever hold titles that are NOT for children."""
        report = self._pinned(ctx, mock_plextv, [self.ROWS[0], self.ROWS[2]])

        assert "grownups" not in self._by_row(report)
        assert report.rows_considered["grownups"] == "no_grown_ups_row_on_a_kids_account"

    def test_a_row_set_to_everything_is_left_exactly_as_the_owner_set_it(self, ctx, mock_plextv):
        """`include` is an explicit per-row "everything". Not second-guessed, only documented."""
        report = self._pinned(ctx, mock_plextv, [self.ROWS[3]])

        assert self._by_row(report) == {"all": [10, 20, 30]}

    def test_an_adult_still_loses_only_the_family_shelf(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient(self._items(), household=ADULT))

        report = _run(ctx, mock_plextv, self.ROWS[1:3])

        assert report.rows_considered["fam"] == "not_a_family_household"
        assert self._by_row(report) == {"grownups": [20]}

    def test_a_pick_carried_from_before_the_pin_is_dropped_once_the_engine_says_what_it_is(self, ctx, mock_plextv):
        """The day a profile is pinned "kids", its frozen rows still hold whatever they held. `auto`
        now constrains for this person, so carried picks are re-checked like any family row's."""
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", "picked", "1"): [
                Pick(
                    tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE
                )
                for i, t in enumerate((10, 20))
            ]
        }
        frozen = RowSpec(slug="picked", name_template="Picked", size=5, family="auto", refresh_days=0)
        # CLASSIFIED, both of them. A title with no genres and no flag is "the engine did not say",
        # which is deliberately never a reason to drop a carried pick.
        answer = [
            _item(10, "Cartoon", kids=True, genres=("Animation", "Family")),
            _item(20, "Drama", genres=("Drama",)),
        ]
        ctx.recommender = HttpRecommender(FakeEngineClient(answer, household=FAMILY))
        ctx.config.rows = [frozen]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pinned = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="kids")]).users[0]

        assert [p.tmdb_id for p in pinned.picks] == [10]
        assert pinned.trace["selection"][0]["family_dropped"] == 1

    def test_the_same_carried_picks_are_left_alone_for_an_adult(self, ctx, mock_plextv):
        """The control for the test above: same row, same carried picks, same answer — no pin."""
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", "picked", "1"): [
                Pick(
                    tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE
                )
                for i, t in enumerate((10, 20))
            ]
        }
        answer = [
            _item(10, "Cartoon", kids=True, genres=("Animation", "Family")),
            _item(20, "Drama", genres=("Drama",)),
        ]
        ctx.recommender = HttpRecommender(FakeEngineClient(answer, household=ADULT))
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, family="auto", refresh_days=0)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]

        assert {p.tmdb_id for p in report.picks} == {10, 20}
        assert "family_dropped" not in report.trace["selection"][0]

    def test_a_cold_start_on_a_pinned_kids_profile_is_left_short_not_filled_with_top_rated(self, ctx, mock_plextv):
        """The pin needs no engine, so it is known on a cold start too — and the server's top-rated
        films, which carry no classification at all, are the last thing a new child's profile should
        be handed because it happens to be new."""
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]  # thin: cold start
        ctx.recommender = BuiltinRecommender()
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, family="auto", cold_start="popular")]
        mock_plextv.users = [plextv_user(100, "kids-tv")]

        pinned = pipeline_mod.run(ctx, [make_profile("kids-tv", account_id=100, household_override="kids")]).users[0]
        unpinned = pipeline_mod.run(ctx, [make_profile("kids-tv", account_id=100)]).users[0]

        assert [p.title for p in pinned.picks] == []
        assert pinned.household["label"] == "kids"
        assert [p.title for p in unpinned.picks] == ["Top Rated"]  # nobody else's cold start changed

    def test_the_trace_says_what_auto_came_to(self, ctx, mock_plextv):
        report = self._pinned(ctx, mock_plextv, self.ROWS[:1])

        entry = next(e for e in report.trace["selection"] if e["row"] == "picked")
        assert (entry["family"], entry["family_means"]) == ("auto", "only")


class TestAKidsRowWithNothingToShow:
    """Delivery leaves a library a row has no picks for ALONE — right when the alternative is deleting
    a good row over one thin night, wrong here: until the day the account was labelled kids its "auto"
    rows held everything, so the copy being left alone is a row of grown-up titles on a child's
    profile. And with several such rows drawing children's titles from one answer without
    replacement, a later row coming up empty is the ordinary case."""

    ANSWER: ClassVar[list] = [
        _item(10, "Cartoon", kids=True, genres=("Animation", "Family")),
        _item(20, "Drama", genres=("Drama",)),
        _item(40, "Thriller", genres=("Thriller",)),
        _item(50, "Horror", genres=("Horror",)),
    ]
    ROWS: ClassVar[list[RowSpec]] = [
        RowSpec(slug="a", name_template="A", size=5, family="auto"),
        RowSpec(slug="b", name_template="B", size=5, family="auto"),
    ]

    def _run(self, ctx, mock_plextv, monkeypatch, *, pin="kids", household=FAMILY, rows=None):
        import shortlist.engine.rows as rows_mod

        taken_down = []

        def _spy(plex, profile, config, spec, **kw):
            taken_down.append((spec.slug, [s.key for s in kw["sections"]], kw["dry_run"]))
            return [str(s.key) for s in kw["sections"]]

        monkeypatch.setattr(rows_mod, "remove_row", _spy)
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", "b", "1"): [
                Pick(
                    tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE
                )
                for i, t in enumerate((40, 50))
            ]
        }
        ctx.recommender = HttpRecommender(FakeEngineClient(list(self.ANSWER), household=household))
        ctx.config.rows = rows or self.ROWS
        mock_plextv.users = [plextv_user(100, "sarah")]
        profile = make_profile("sarah", account_id=100, **({"household_override": pin} if pin else {}))
        return pipeline_mod.run(ctx, [profile]).users[0], taken_down

    def test_the_old_copy_of_an_emptied_row_is_taken_down_in_that_library(self, ctx, mock_plextv, monkeypatch):
        report, taken_down = self._run(ctx, mock_plextv, monkeypatch)

        assert {p.collection_slug: p.tmdb_id for p in report.picks} == {"a": 10}  # the one children's title
        assert taken_down == [("b", ["1"], False)]

    def test_the_run_page_is_told_rather_than_shown_nothing(self, ctx, mock_plextv, monkeypatch):
        report, _ = self._run(ctx, mock_plextv, monkeypatch)

        entry = next(e for e in report.trace["selection"] if e["row"] == "b")
        assert (entry["decision"], entry["delivered"], entry["family_means"], entry["removed"]) == (
            "emptied",
            0,
            "only",
            True,
        )

    def test_a_row_already_built_as_a_childrens_row_is_left_alone_on_a_thin_night(self, ctx, mock_plextv, monkeypatch):
        """What is up is then NOT the grown-up row this exists to remove (the recipe records what
        "auto" came to), so one night of an engine answering with little must not delete a good row."""
        from shortlist.engine.household import Household
        from shortlist.engine.rows import RowPolicy, _rating_key_resolver, row_recipe

        policy = RowPolicy(
            ctx=ctx,
            user=make_profile("sarah", account_id=100, household_override="kids"),
            cfg=ctx.config,
            specs=self.ROWS,
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )
        policy.household = Household(label="kids", source="override")
        ctx.previous_recipes = {("sarah", "b", "1"): row_recipe(policy, self.ROWS[1])}

        _, taken_down = self._run(ctx, mock_plextv, monkeypatch)

        assert taken_down == []

    def test_nobody_elses_empty_row_is_touched(self, ctx, mock_plextv, monkeypatch):
        """The control: same rows, same carried picks, an adult. "Left alone" still stands for them."""
        _, taken_down = self._run(ctx, mock_plextv, monkeypatch, pin=None, household=ADULT)

        assert taken_down == []

    def test_an_explicit_family_row_that_comes_up_empty_is_still_left_alone(self, ctx, mock_plextv, monkeypatch):
        """Only "auto" changed meaning. An `only` row never held anything but children's titles, so its
        old copy is not a row of grown-up titles and the usual rule is the right one."""
        rows = [self.ROWS[0], RowSpec(slug="b", name_template="B", size=5, family="only")]

        _, taken_down = self._run(ctx, mock_plextv, monkeypatch, pin="family", rows=rows)

        assert ("b", ["1"], False) not in taken_down

    def test_a_cold_row_with_nothing_vetted_takes_its_old_copy_down_too(self, ctx, mock_plextv, monkeypatch):
        """A new profile that already got a cold row of the server's top-rated films, THEN was pinned."""
        import shortlist.engine.rows as rows_mod

        taken_down = []
        monkeypatch.setattr(
            rows_mod,
            "remove_row",
            lambda plex, profile, config, spec, **kw: taken_down.append(spec.slug) or ["1"],
        )
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]  # thin: cold start
        ctx.recommender = BuiltinRecommender()
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, family="auto", cold_start="popular")]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="kids")]).users[0]

        assert taken_down == ["picked"]
        assert [e["decision"] for e in report.trace["selection"]] == ["emptied"]


class TestTakingAnEmptiedKidsRowDownForReal:
    """The REAL `remove_row`, against the Plex mock. The tests above stub it out, and a stub that
    answers the same whatever it is handed let every argument be wrong: a delete during a dry run,
    every library instead of this one, and a ledger that went on calling a deleted collection live."""

    ANSWER: ClassVar[list] = [
        _item(10, "Cartoon", kids=True, genres=("Animation", "Family")),
        _item(20, "Drama", genres=("Drama",)),
    ]

    @staticmethod
    def _collection(title: str, key: int):
        from shortlist.engine.delivery import row_marker

        found = MagicMock()
        found.title = title + row_marker(100)
        found.ratingKey = key
        return found

    def _run(self, ctx, mock_plextv, rows, collections, *, dry_run=False, ledger=None):
        ctx.plex.sections.return_value[0].key = "1"
        ctx.config.dry_run = dry_run
        ctx.config.rows = rows
        ctx.plex.find_owned_collections.return_value = collections
        ctx.delivered_keys = ledger or {}
        ctx.previous_picks = {
            ("sarah", "b", "1"): [
                Pick(tmdb_id=20, rating_key=1020, title="Drama", rank=1, reason="kept", media_type=MediaType.MOVIE,
                     seed_title="Fargo", seed_tmdb_id=900)
            ]
        }  # fmt: skip
        ctx.previous_recipes = {("sarah", "b", "1"): "built before the pin"}
        ctx.recommender = HttpRecommender(FakeEngineClient(list(self.ANSWER), household=FAMILY))
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="kids")]).users[0]
        deleted = [call.args[0].ratingKey for call in ctx.plex.delete_owned_collection.call_args_list]
        emptied = [e for e in report.trace["selection"] if e["decision"] == "emptied"]
        return report, deleted, emptied

    ROWS: ClassVar[list[RowSpec]] = [
        RowSpec(slug="a", name_template="A", size=5, family="auto"),
        RowSpec(slug="b", name_template="B", size=5, family="auto"),
    ]

    def test_the_emptied_rows_own_collection_goes_and_no_other(self, ctx, mock_plextv):
        _, deleted, emptied = self._run(
            ctx, mock_plextv, self.ROWS, [self._collection("A", 1), self._collection("B", 2)]
        )

        assert deleted == [2]
        assert [(e["row"], e["removed"]) for e in emptied] == [("b", True)]

    def test_a_dry_run_deletes_nothing_and_still_says_a_copy_would_come_down(self, ctx, mock_plextv):
        report, deleted, emptied = self._run(
            ctx, mock_plextv, self.ROWS, [self._collection("A", 1), self._collection("B", 2)], dry_run=True
        )

        assert deleted == []
        assert "B" in report.diff.deleted
        assert emptied[0]["removed"] is True

    def test_with_no_old_copy_there_is_nothing_to_say_was_removed(self, ctx, mock_plextv):
        _, deleted, emptied = self._run(ctx, mock_plextv, self.ROWS, [self._collection("A", 1)])

        assert deleted == []
        assert emptied[0]["removed"] is False

    def test_the_rows_copy_in_another_library_is_not_touched(self, ctx, mock_plextv, monkeypatch):
        """THIS library came up empty. The same row's copy in another library is a good children's row
        (its recipe says so), and taking "the row" down everywhere would delete it too."""
        import shortlist.engine.rows as rows_mod

        elsewhere = MagicMock()
        elsewhere.key, elsewhere.type, elsewhere.title = "2", "movie", "4K Movies"
        ctx.plex.sections.return_value = [ctx.plex.sections.return_value[0], elsewhere]
        good_copy = self._collection("B", 22)
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [good_copy] if str(section.key) == "2" else []
        )
        scanned = []
        real = rows_mod.remove_row

        def watching(*a, **kw):
            scanned.append([str(section.key) for section in kw["sections"]])
            return real(*a, **kw)

        monkeypatch.setattr(rows_mod, "remove_row", watching)

        self._run(ctx, mock_plextv, self.ROWS, [])

        assert scanned and all(len(sections) == 1 for sections in scanned)

    def test_the_ledger_stops_calling_the_deleted_collection_live(self, ctx, mock_plextv):
        """An emptied section persists no picks, so this path repeats nightly — and a stale ledger key
        is re-presented each time, where a sibling row that has since reused that key would be deleted
        in its place. The old picks would also stay creditable as "on Plex"."""
        rows = [self.ROWS[0], RowSpec(slug="b", name_template="Because you watched {top_seed}", size=5, family="auto")]
        collections = [self._collection("A", 1), self._collection("Because you watched Fargo", 2)]

        report, deleted, _ = self._run(ctx, mock_plextv, rows, collections, ledger={("sarah", "b", "1"): 2})

        assert deleted == [2]
        assert report.removed_deliveries, "the ledger still calls the deleted collection live"

    def test_a_row_named_after_its_picks_with_no_ledger_key_is_never_guessed_at(self, ctx, mock_plextv):
        """Such a row renders to nothing with no picks, so there is no title to match on — and matching
        on anything else would delete somebody's live row. Left alone, and the trace does not claim
        otherwise."""
        rows = [self.ROWS[0], RowSpec(slug="b", name_template="Because you watched {top_seed}", size=5, family="auto")]
        collections = [self._collection("A", 1), self._collection("Because you watched Fargo", 2)]

        _, deleted, emptied = self._run(ctx, mock_plextv, rows, collections)

        assert deleted == []
        assert emptied[0]["removed"] is False


class TestPinningAProfileKidsRebuildsItsRows:
    def test_a_frozen_row_does_not_keep_an_adult_pick_the_answer_happens_not_to_mention(self, ctx, mock_plextv):
        """Carried picks are only dropped on a POSITIVE contradiction, so an adult title tonight's
        answer omits rode on — for ever, on a frozen row — under a run page saying "holds only
        children's titles". What "auto" came to is part of the recipe now, so the pin is a settings
        change and the row rebuilds that night, cadence and hold notwithstanding."""
        ctx.plex.sections.return_value[0].key = "1"
        frozen = RowSpec(slug="picked", name_template="Picked", size=5, family="auto", refresh_days=0)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.config.rows = [frozen]
        ctx.recommender = HttpRecommender(
            FakeEngineClient(
                [_item(10, "Cartoon", kids=True, genres=("Animation", "Family")), _item(30, "Anime", kids=True)],
                household=FAMILY,
            )
        )
        # Last night, before the pin: an ordinary row holding a drama the answer no longer mentions,
        # stored with the recipe an unpinned person's row has.
        from shortlist.engine.rows import RowPolicy, _rating_key_resolver, row_recipe

        real = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]
        unpinned = RowPolicy(
            ctx=ctx,
            user=make_profile("sarah", account_id=100),
            cfg=ctx.config,
            specs=ctx.config.rows,
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )
        ctx.previous_picks = {
            ("sarah", "picked", "1"): [
                *real.picks,
                Pick(tmdb_id=20, rating_key=1020, title="Drama", rank=9, reason="kept", media_type=MediaType.MOVIE),
            ]
        }
        ctx.previous_recipes = {("sarah", "picked", "1"): row_recipe(unpinned, frozen)}

        after = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="kids")]).users[0]

        assert 20 not in [p.tmdb_id for p in after.picks]
        assert after.trace["selection"][0]["decision"] == "settings_changed"

    def test_the_recipe_of_everyone_else_is_what_it_was(self, ctx, mock_plextv):
        """Conditional on purpose: an unconditional part would rebuild every row on the server the
        night this shipped."""
        from shortlist.engine.household import Household
        from shortlist.engine.rows import row_recipe

        spec = RowSpec(slug="r", name_template="R", size=5, family="auto")
        policy = MagicMock()
        policy.user.blocked_seeds = []
        policy.effective_sources.return_value = ["tmdb"]

        def recipe(label):
            policy.household = Household(label=label, source="override") if label else None
            return row_recipe(policy, spec)

        assert recipe("family") == recipe("adult") == recipe(None)
        assert recipe("kids") != recipe(None) and "means=only" in recipe("kids")


class TestAColdStartWhereEveryRowIsOneThisHouseholdDoesNotGet:
    """`_cold_start` sizes itself from the rows it is handed. The household used to be settled only for
    a warm person, so it was never handed none — until a pin could take every due row down first, and
    the person failed with "max() iterable argument is empty" on every run scoped to that row."""

    @pytest.mark.parametrize(
        ("pin", "family"),
        [("kids", "only"), ("kids", "exclude"), ("adult", "only")],
    )
    def test_it_is_skipped_with_a_reason_not_failed(self, ctx, mock_plextv, pin, family):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]  # thin: cold start
        ctx.recommender = BuiltinRecommender()
        ctx.config.rows = [RowSpec(slug="r", name_template="R", size=5, family=family, cold_start="popular")]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override=pin)]).users[0]

        assert (report.status, report.error) == ("skipped", None)
        assert f"not built for this account's household ({pin})" in report.reason
        assert report.picks == []

    def test_an_engine_that_takes_thin_people_on_and_answers_nothing_gives_the_same_reason(self, ctx, mock_plextv):
        """The late road to a cold start. It did not crash, but it blamed the rows' cold-start setting:
        "The 0 rows due in this run are set to build nothing until then"."""
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        ctx.recommender = HttpRecommender(FakeEngineClient([]), serves_cold=True)
        ctx.config.rows = [
            RowSpec(slug="fam", name_template="F", size=5, family="only", cold_start="popular"),
            RowSpec(slug="grown", name_template="G", size=5, family="exclude", cold_start="popular"),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="kids")]).users[0]

        assert report.status == "skipped"
        assert report.reason.startswith("All 2 rows due in this run are not built for this account's household (kids)")

    def test_the_rows_they_DO_get_are_still_built(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        ctx.recommender = BuiltinRecommender()
        ctx.config.rows = [
            RowSpec(slug="fam", name_template="F", size=5, family="only", cold_start="popular"),
            RowSpec(slug="all", name_template="A", size=5, family="include", cold_start="popular"),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="adult")]).users[0]

        assert report.status != "error"
        assert {p.collection_slug for p in report.picks} == {"all"}

    def test_a_cold_start_with_no_pin_does_not_report_an_unknown_household(self, ctx, mock_plextv):
        """The run is persisted over what the last good run found. "Unknown" written there flips the
        person's request page out of its family lane and silences the pooled-profile warning — on
        exactly the night the engine could not be reached."""
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        ctx.recommender = BuiltinRecommender()
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.config.rows = [RowSpec(slug="picked", name_template="P", size=5, cold_start="popular")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]

        assert report.household is None


class TestARewatchRowsTopUpDoesNotRepeatASibling:
    """Found on a child's new profile (dry run, 2026-09-21): "TV Shows you've already seen" held two
    real rewatches and three shows never watched — the same three that WERE "TV Shows Picked for You".

    A rewatch row leads with finished titles and tops up what is left from the pool its sibling rows
    draw from. It was exempt from draw-without-replacement as "built from history", which is only true
    of the lead: on a thin history the pads were the head of the pool, exactly what the row before it
    had just taken. Invisible on a full history, where there is no top-up at all."""

    ROWS: ClassVar[list[RowSpec]] = [
        RowSpec(slug="picked", name_template="Picked", size=2),
        RowSpec(slug="again", name_template="Already seen", size=3, rewatch=True, watched_pct=1.0),
    ]

    def _by_row(self, report):
        out = {}
        for pick in report.picks:
            out.setdefault(pick.collection_slug, []).append(pick.tmdb_id)
        return out

    def _engine(self, ctx):
        ctx.recommender = HttpRecommender(
            FakeEngineClient([_item(10, "Ten"), _item(20, "Twenty"), _item(30, "Thirty")])
        )

    def test_the_top_up_takes_what_the_earlier_row_left(self, ctx, mock_plextv):
        self._engine(ctx)

        rows = self._by_row(_run(ctx, mock_plextv, self.ROWS))

        assert rows["picked"] == [10, 20]
        assert rows["again"] == [900, 30]  # the finished title leads; the pad is the one title left
        assert not set(rows["picked"]) & set(rows["again"])

    def test_a_short_row_beats_a_padded_one(self, ctx, mock_plextv):
        """Nothing left in the pool: deliver the rewatches and stop. The row's NAME says "already seen"."""
        self._engine(ctx)
        rows = [RowSpec(slug="picked", name_template="Picked", size=3), self.ROWS[1]]

        assert self._by_row(_run(ctx, mock_plextv, rows))["again"] == [900]

    def test_pads_carried_from_before_the_fix_go_too_and_the_rewatch_stays(self, ctx, mock_plextv):
        """The pool cannot reach a pick the row is CARRYING, and a live run before this shipped would
        have persisted exactly these pads. A finished title is the row's own and is never dropped for
        appearing elsewhere."""
        self._engine(ctx)
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", "again", "1"): [
                Pick(
                    tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE
                )
                for i, t in enumerate((900, 10, 20))
            ]
        }
        frozen = [self.ROWS[0], RowSpec(slug="again", name_template="Already seen", size=3, rewatch=True,
                                        watched_pct=1.0, refresh_days=0)]  # fmt: skip

        report = _run(ctx, mock_plextv, frozen)
        rows = self._by_row(report)

        assert rows["picked"] == [10, 20]
        assert 900 in rows["again"]
        assert not {10, 20} & set(rows["again"])
        # The one reason a frozen watch-it-again row changes, so the run page has to be able to say it.
        entry = next(e for e in report.trace["selection"] if e["row"] == "again")
        assert entry["elsewhere_dropped"] == 2

    def test_a_finished_title_is_the_rows_own_even_when_another_row_holds_it(self, ctx, mock_plextv):
        """Two watch-it-again rows both lead with what they finished; that was never drawn from the
        pool and is not a pad. Treating it as a repeat would empty the second row's lead every night."""
        self._engine(ctx)
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", "again2", "1"): [
                Pick(tmdb_id=900, rating_key=1900, title="Fargo", rank=1, reason="kept", media_type=MediaType.MOVIE)
            ]
        }
        rows = [
            RowSpec(slug="again", name_template="Already seen", size=2, rewatch=True, watched_pct=1.0),
            RowSpec(slug="again2", name_template="Again, again", size=2, rewatch=True, watched_pct=1.0, refresh_days=0),
        ]

        report = _run(ctx, mock_plextv, rows)
        by_row = self._by_row(report)

        assert by_row["again"][0] == 900
        assert 900 in by_row["again2"]
        # And it was CARRIED, not dropped as a repeat and picked straight back up: the titles come out
        # the same either way, which is how that churn would hide.
        entry = next(e for e in report.trace["selection"] if e["row"] == "again2")
        assert (entry["decision"], entry["carried"]) == ("carried_forward", 1)

    def test_a_carried_pad_nobody_else_holds_is_left_alone(self, ctx, mock_plextv):
        """Only a REPEAT goes. A pad the row has carried that no sibling holds is an ordinary pick, and
        dropping it would rebuild a frozen row every night."""
        self._engine(ctx)
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", "again", "1"): [
                Pick(
                    tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE
                )
                for i, t in enumerate((900, 30))
            ]
        }
        frozen = [self.ROWS[0], RowSpec(slug="again", name_template="Already seen", size=3, rewatch=True,
                                        watched_pct=1.0, refresh_days=0)]  # fmt: skip

        report = _run(ctx, mock_plextv, frozen)

        assert self._by_row(report)["again"] == [900, 30]
        assert next(e for e in report.trace["selection"] if e["row"] == "again")["decision"] == "carried_forward"

    def test_a_row_that_is_nothing_but_repeats_with_nothing_to_replace_them_is_carried(self, ctx, mock_plextv):
        """Dropping them would build nothing, and a library a row builds nothing for is left alone — so
        the same pads would stay on Plex, come back as "carried" tomorrow and be dropped again, every
        night, with no trace entry at all. Carried until there is something to put in their place."""
        self._engine(ctx)
        ctx.plex.sections.return_value[0].key = "1"
        ctx.history_source.fetch.return_value = []  # nothing finished, so no lead either
        ctx.config.min_history = 0
        ctx.previous_picks = {
            ("sarah", "again", "1"): [
                Pick(
                    tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE
                )
                for i, t in enumerate((10, 20))
            ]
        }
        rows = [RowSpec(slug="picked", name_template="Picked", size=3), replace(self.ROWS[1], refresh_days=0)]

        report = _run(ctx, mock_plextv, rows)

        entry = next(e for e in report.trace["selection"] if e["row"] == "again")
        assert entry["decision"] == "carried_forward" and "elsewhere_dropped" not in entry
        assert self._by_row(report)["again"] == [10, 20]

    def test_a_finished_title_outside_tonights_scan_is_still_not_a_pad(self, ctx, mock_plextv):
        """The scan of their history is a window (a few times the row's size, in an order that moves
        with taste). "Not in tonight's window" is not "not finished": asked of the whole finished set,
        or a second watch-it-again row drops a rewatch it has carried for months and re-stamps itself."""
        self._engine(ctx)
        ctx.plex.sections.return_value[0].key = "1"
        finished = list(range(900, 908))
        ctx.plex.build_library_index.return_value = {
            **{t: 1000 + t for t in (10, 20, 30)},
            **{t: 9000 + t for t in finished},
        }
        ctx.history_source.fetch.return_value = [
            make_watched(f"Film {t}", days_ago=100 + i, rating_key=9000 + t, tmdb_id=t) for i, t in enumerate(finished)
        ]
        ctx.previous_picks = {
            ("sarah", "again", "1"): [
                Pick(
                    tmdb_id=t,
                    rating_key=9000 + t,
                    title=f"Film {t}",
                    rank=i + 1,
                    reason="kept",
                    media_type=MediaType.MOVIE,
                )
                for i, t in enumerate((900, 907))
            ],
            # 900 is their most RECENT finish, and the scan reads oldest-first: with a window of three
            # (size 1) it never reaches 900, which is what made it look like a pad.
            ("sarah", "again2", "1"): [
                Pick(tmdb_id=900, rating_key=9900, title="Film 900", rank=1, reason="kept", media_type=MediaType.MOVIE)
            ],
        }
        rows = [
            RowSpec(slug="again", name_template="Already seen", size=2, rewatch=True, watched_pct=1.0, refresh_days=0),
            RowSpec(slug="again2", name_template="Again, again", size=1, rewatch=True, watched_pct=1.0, refresh_days=0),
        ]

        report = _run(ctx, mock_plextv, rows)

        entry = next(e for e in report.trace["selection"] if e["row"] == "again2")
        assert (entry["decision"], entry["carried"]) == ("carried_forward", 1)
        assert "elsewhere_dropped" not in entry
        assert self._by_row(report)["again2"] == [900]

    def test_a_frozen_row_does_not_trade_a_title_back_and_forth_with_a_sibling_that_refreshes(self, ctx, mock_plextv):
        """A sibling that refreshes rotates a title out; the freed title landed here the same night and
        swapped back on the next refresh — so a row set NEVER to rebuild rewrote itself on its sibling's
        cadence, for ever. Six nights on a static answer: it may correct itself once, then never moves."""
        ids = [10, 20, 30, 40, 50, 60, 70]
        ctx.plex.build_library_index.return_value = {900: 999, **{t: 1000 + t for t in ids}}
        ctx.plex.sections.return_value[0].key = "1"
        rows = [
            RowSpec(slug="picked", name_template="Picked", size=3, refresh_days=1, idle_hold_days=0),
            replace(self.ROWS[1], refresh_days=0),
        ]
        seen = []
        for _night in range(6):
            ctx.recommender = HttpRecommender(FakeEngineClient([_item(t, f"T{t}") for t in ids]))
            report = _run(ctx, mock_plextv, rows)
            by_row: dict[str, list[Pick]] = {}
            for pick in report.picks:
                by_row.setdefault(pick.collection_slug, []).append(pick)
            seen.append(sorted(p.tmdb_id for p in by_row["again"]))
            ctx.previous_picks = {
                ("sarah", slug, "1"): sorted(picks, key=lambda p: p.rank) for slug, picks in by_row.items()
            }
            ctx.previous_recipes = {
                key: picks[0].recipe for key, picks in ctx.previous_picks.items() if picks and picks[0].recipe
            }

        # One correction is legitimate — the night the sibling's refresh takes a title this row is
        # carrying, that pad goes. After it, a static answer must leave the frozen row alone for good.
        assert all(night == seen[1] for night in seen[1:]), seen

    def test_shortlists_own_engine_is_left_as_upstream_has_it(self, ctx, mock_plextv):
        """Drawing without replacement is for an engine that answers one ordered list per pool. The
        built-in engine ranks per row, and its behaviour is upstream's to change."""
        ctx.recommender = BuiltinRecommender()

        before = self._by_row(_run(ctx, mock_plextv, self.ROWS))

        assert before["again"][0] == 900
        assert set(before["picked"]) & set(before["again"])  # it repeats, as it always did


class TestANightTheEngineSaidNothing:
    """Fallback is the default: an unreachable engine means the built-in one ranks, and reports no
    household. "Nothing reported tonight" is not evidence that a child's account stopped being one —
    read that way, its frozen rows were rebuilt as "everything" (the recipe lost `means=only`) and the
    stored label was overwritten with "unknown"."""

    KIDS_LAST_NIGHT: ClassVar[dict] = {"label": "kids", "source": "engine", "kids_titles": 90, "window_titles": 100}

    def _run(self, ctx, mock_plextv, *, last, engine="recommendarr"):
        silent = HttpRecommender(FakeEngineClient([_item(10, "Cartoon", kids=True), _item(20, "Drama")]), name=engine)
        ctx.recommender = silent
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, family="auto")]
        mock_plextv.users = [plextv_user(100, "sarah")]
        profile = make_profile("sarah", account_id=100)
        profile.last_household = last
        return pipeline_mod.run(ctx, [profile]).users[0]

    def test_a_kids_account_by_counts_is_still_one(self, ctx, mock_plextv):
        report = self._run(ctx, mock_plextv, last=self.KIDS_LAST_NIGHT)

        assert [p.tmdb_id for p in report.picks] == [10]
        assert report.trace["selection"][0]["family_means"] == "only"

    def test_and_last_nights_label_is_not_restamped_as_tonights(self, ctx, mock_plextv):
        """Nothing was established, so nothing is reported and the stored household stands as it was."""
        assert self._run(ctx, mock_plextv, last=self.KIDS_LAST_NIGHT).household is None

    def test_someone_with_no_earlier_label_is_unknown_as_before(self, ctx, mock_plextv):
        report = self._run(ctx, mock_plextv, last=None)

        assert [p.tmdb_id for p in report.picks] == [10, 20]

    def test_a_label_left_over_from_an_engine_since_removed_sorts_nobody(self):
        """With the built-in engine configured nobody is sorted, tonight or ever."""
        from shortlist.engine.household import resolve_household

        profile = make_profile("s")
        profile.last_household = self.KIDS_LAST_NIGHT

        assert resolve_household(profile, None, EngineConfig(), engine_reports=False).label is None
        assert resolve_household(profile, None, EngineConfig(), engine_reports=True).source == "last_run"

    def test_a_setting_the_owner_has_since_removed_is_not_stood_in_for(self):
        """Stored as `source: "override"`. It is not evidence of anything once removed — and a stand-in
        is never written back, so for someone the engine never reports on it would sort them for ever."""
        from shortlist.engine.household import resolve_household

        profile = make_profile("s")
        profile.last_household = {"label": "kids", "source": "override"}

        assert resolve_household(profile, None, EngineConfig(), engine_reports=True).label is None

    def test_tonights_answer_beats_last_nights(self):
        from shortlist.engine.household import resolve_household

        profile = make_profile("s")
        profile.last_household = self.KIDS_LAST_NIGHT

        assert resolve_household(profile, ADULT, EngineConfig(), engine_reports=True).label == "adult"


class TestARowWithNoRecipeOnRecord:
    def test_a_cold_built_row_is_not_carried_onto_a_childs_profile(self, ctx, mock_plextv):
        """A row built on a cold start records no recipe, and "no recipe" is read as "unchanged"
        everywhere — so the pin did not rebuild it: the server's top-rated films were carried forward
        onto the child's profile, for ever if frozen, under "holds only children's titles"."""
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", "picked", "1"): [
                Pick(tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="Popular on this server",
                     media_type=MediaType.MOVIE)
                for i, t in enumerate((20, 30))
            ]
        }  # fmt: skip
        ctx.previous_recipes = {}
        ctx.recommender = HttpRecommender(
            FakeEngineClient([_item(10, "Cartoon", kids=True, genres=("Animation", "Family"))], household=FAMILY)
        )
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, family="auto", refresh_days=0)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pinned = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="kids")]).users[0]
        unpinned = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="adult")]).users[0]

        assert [p.tmdb_id for p in pinned.picks] == [10]
        assert {20, 30} <= {p.tmdb_id for p in unpinned.picks}  # and nobody else's carry-forward changed


class TestAHeldRowDoesNotRecordItselfAsAChildrensRow:
    def test_a_tmdb_outage_on_the_night_of_the_pin_only_delays_the_rebuild(self, ctx, mock_plextv):
        """The genre hold carries the row as it is and stamps a recipe on what it carries. With none on
        record it stamped TONIGHT's — `means=only` — onto the server's top-rated films, so the next
        night nothing looked changed and they stayed on the child's frozen row for good."""
        ctx.plex.sections.return_value[0].key = "1"
        ctx.previous_picks = {
            ("sarah", "rew", "1"): [
                Pick(tmdb_id=t, rating_key=1000 + t, title=f"Adult{t}", rank=i + 1, reason="Popular on this server",
                     media_type=MediaType.MOVIE)
                for i, t in enumerate((20, 30))
            ]
        }  # fmt: skip
        ctx.previous_recipes = {}
        ctx.tmdb.genre_names.side_effect = RuntimeError("tmdb is down")
        ctx.recommender = HttpRecommender(
            FakeEngineClient([_item(10, "Cartoon", kids=True, genres=("Animation", "Family"))], name="e"), name="e"
        )
        ctx.config.rows = [
            RowSpec(
                slug="rew", name_template="Again", size=5, family="auto", rewatch=True, watched_pct=1.0, refresh_days=0
            )
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]
        kid = make_profile("sarah", account_id=100, household_override="kids")

        held = pipeline_mod.run(ctx, [kid]).users[0]
        assert not any("means=only" in (p.recipe or "") for p in held.picks)

        # The next night TMDB answers, and the row is rebuilt as it should have been.
        ctx.previous_picks = {("sarah", "rew", "1"): list(held.picks)}
        ctx.previous_recipes = {
            key: picks[0].recipe for key, picks in ctx.previous_picks.items() if picks and picks[0].recipe
        }
        ctx.tmdb.genre_names.side_effect = None
        ctx.tmdb.genre_names.return_value = {16: "Animation", 10751: "Family", 18: "Drama"}
        ctx.tmdb.genre_ids_for.side_effect = lambda tmdb_id, kind: [18]

        rebuilt = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="kids")]).users[0]

        assert not {20, 30} & {p.tmdb_id for p in rebuilt.picks}


class TestWhatAutoMeans:
    """`_family_means` — the one place "auto" is resolved, so the filter, the carried-pick re-check
    and the cold start cannot come to different answers about the same row."""

    @pytest.mark.parametrize(
        ("label", "means", "constrains"),
        [("family", "exclude", True), ("kids", "only", True), ("adult", "include", False), (None, "include", False)],
    )
    def test_by_household(self, label, means, constrains):
        from shortlist.engine.household import Household
        from shortlist.engine.rows import _family_constrains, _family_means

        spec = RowSpec(slug="r", name_template="R", size=5, family="auto")
        household = Household(label=label, source="override" if label else "none")

        assert _family_means(spec, household) == means
        assert _family_constrains(spec, household) is constrains

    def test_nobody_settled_yet_is_everything(self):
        from shortlist.engine.rows import _family_means

        assert _family_means(RowSpec(slug="r", name_template="R", size=5, family="auto"), None) == "include"

    @pytest.mark.parametrize("setting", ["include", "exclude", "only"])
    def test_an_explicit_setting_never_depends_on_who_is_watching(self, setting):
        from shortlist.engine.household import Household
        from shortlist.engine.rows import _family_means

        spec = RowSpec(slug="r", name_template="R", size=5, family=setting)

        assert {
            _family_means(spec, Household(label=label, source="none")) for label in ("kids", "family", "adult", None)
        } == {setting}


class TestAPooledHousehold:
    """The engine ranks a household's Plex Home profiles as one person and says so with `group`."""

    POOLED: ClassVar[dict] = {**FAMILY, "group": 895220}

    def test_the_group_rides_along_whatever_decided_the_label(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient([_item(10, "Cartoon", kids=True)], household=self.POOLED))
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, family="auto")]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pinned = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100, household_override="kids")]).users[0]
        auto = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]

        assert (pinned.household["label"], pinned.household["source"], pinned.household["group"]) == (
            "kids",
            "override",
            895220,
        )
        assert (auto.household["label"], auto.household["group"]) == ("family", 895220)

    def test_nobody_who_is_not_pooled_carries_the_key(self, ctx, mock_plextv):
        ctx.recommender = HttpRecommender(FakeEngineClient([_item(10, "Cartoon")], household=FAMILY))

        assert "group" not in _run(ctx, mock_plextv).household

    def test_the_group_survives_counts_the_engine_got_wrong(self):
        """An identity fact, not a viewing one — and an engine with a counts bug labels nobody, which
        is exactly when "this profile is pooled and unpinned" most needs saying."""
        from shortlist.engine.household import resolve_household

        household = resolve_household(make_profile("s"), {"kids_titles": "lots", "group": 895220}, EngineConfig())

        assert (household.label, household.source, household.group) == (None, "none", 895220)
        assert household.as_dict()["group"] == 895220

    @pytest.mark.parametrize("junk", ["895220", True, None, 1.5, {"id": 1}])
    def test_a_group_that_is_not_an_account_id_is_ignored(self, junk):
        from shortlist.engine.household import resolve_household

        household = resolve_household(make_profile("s"), {**FAMILY, "group": junk}, EngineConfig())

        assert household.group is None and household.label == "family"


class TestClassify:
    def test_the_default_thresholds(self):
        from shortlist.engine.household import classify

        cfg = EngineConfig()
        assert classify(5, 5, cfg) == "adult"  # too few to judge
        assert classify(10, 9, cfg) == "kids"
        assert classify(187, 66, cfg) == "family"
        assert classify(26, 4, cfg) == "family"
        assert classify(27, 4, cfg) == "adult"  # 14.8%
        assert classify(305, 23, cfg) == "adult"
        assert classify(20, 3, cfg) == "adult"  # 15% but only 3 titles

    def test_a_malformed_report_is_ignored(self):
        from shortlist.engine.household import resolve_household

        hh = resolve_household(make_profile("s"), {"label": "family", "kids_titles": "lots"}, EngineConfig())
        assert hh.label is None and hh.source == "none"
