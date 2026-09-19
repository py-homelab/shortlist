"""The recommender boundary: the built-in engine behind it, an external engine over HTTP, the
fallback between them, and what the row build does with an engine's final order."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
import respx

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine import ranking
from shortlist.engine.clients import http_retry
from shortlist.engine.clients.engine_http import EngineClient, EngineError, recommend_payload
from shortlist.engine.context import EngineContext
from shortlist.engine.models import EngineConfig, MediaType, RowSpec, Seed
from shortlist.engine.picker import reason_for
from shortlist.engine.recommender import RecommendRequest, RecommendResult
from shortlist.engine.recommenders import BuiltinRecommender, FallbackRecommender, HttpRecommender
from shortlist.engine.recommenders.builtin import is_kids
from tests.conftest import MemorySnapshotStore, make_candidate, make_profile, make_watched, plextv_user


class FakeEngineClient:
    """An `EngineClient` that answers from a canned body and remembers what it was asked."""

    def __init__(self, items: list[dict] | Exception, *, name: str = "fake-engine", trace: dict | None = None):
        self.items = items
        self.name = name
        self.trace = trace
        self.payloads: list[dict] = []

    def recommend(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if isinstance(self.items, Exception):
            raise self.items
        body = {"engine": {"name": self.name, "version": "0.1"}, "ordered": True, "items": self.items}
        if self.trace is not None:
            body["trace"] = self.trace
        return body

    def info(self) -> dict:
        return {"name": self.name, "version": "0.1", "surfaces": ["library"], "serves_cold": False, "ready": True}


def _item(tmdb_id: int, title: str, *, media="movie", rating=7.0, reason=None, kids=False, seed=None, genres=()):
    return {
        "tmdb_id": tmdb_id,
        "media_type": media,
        "title": title,
        "year": 2020,
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

        [payload] = client.payloads
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
        assert len(client.payloads) == 1

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

    def test_an_engine_that_does_not_serve_cold_is_not_asked(self, ctx, mock_plextv):
        ctx.history_source.fetch.return_value = [make_watched("Fargo", rating_key=999)]
        client = FakeEngineClient([_item(20, "Twenty")], name="e")
        ctx.recommender = HttpRecommender(client, name="e", serves_cold=False)
        report = _run(ctx, mock_plextv)
        assert report.status == "cold_start"
        assert client.payloads == []


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
                RowSpec(slug="all", name_template="Everyone", size=5),
            ],
        )

        by_row = {}
        for p in report.picks:
            by_row.setdefault(p.collection_slug, []).append(p.tmdb_id)
        assert by_row == {"grown": [20], "fam": [10, 30], "all": [10, 20, 30]}

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
