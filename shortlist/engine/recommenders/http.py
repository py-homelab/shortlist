"""An engine running elsewhere, reached over HTTP (``clients/engine_http.py`` has the protocol).

What comes back is the engine's FINAL order. This module turns it into ``Candidate``s the row build
already understands, applies the rules that are Shortlist's whatever the engine — library
membership, the row's watched/started exclusions, genre exclusions, what the person's Plex
restrictions let them see — and cuts to ``limit_per_media`` in that order. Nothing here re-scores:
``external_rank`` is what ``ranking._sort_key`` reads for these, and ``diversify_by_seed`` steps aside.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from shortlist.engine import candidates as candidates_mod
from shortlist.engine.clients.engine_http import EngineClient, EngineError, recommend_payload
from shortlist.engine.models import Candidate, MediaType, Seed
from shortlist.engine.recommender import EXTERNAL_SOURCE_PREFIX, RecommendRequest, RecommendResult
from shortlist.engine.recommenders.builtin import _media_filter, _visible_candidates

if TYPE_CHECKING:
    from shortlist.engine.context import EngineContext


class HttpRecommender:
    def __init__(self, client: EngineClient, *, name: str = "", serves_cold: bool = False, info: dict | None = None):
        """``name`` and ``serves_cold`` come from the engine's ``/v1/info``; the server adapter reads it
        once at build time and passes them in, so a run never blocks on it. Unknown defaults are the
        conservative ones: an unnamed engine, and cold-start handled by Shortlist."""
        self._client = client
        self.name = name or "external"
        self.serves_cold = serves_cold
        #: The engine's own `/v1/info` at run start, or an `{"unreachable": reason}` when it could not be
        #: read. What `engine_status` reports; the rows never read it.
        self.info = info

    @property
    def source(self) -> str:
        return EXTERNAL_SOURCE_PREFIX + self.name

    def recommend(
        self,
        ctx: EngineContext,
        req: RecommendRequest,
        *,
        visible: Callable[[list[int]], set[int] | None] | None = None,
    ) -> RecommendResult:
        started = time.monotonic()
        body = self._client.recommend(recommend_payload(req, ctx.run_day))
        engine = body.get("engine") or {}
        if isinstance(engine, dict) and isinstance(engine.get("name"), str) and engine["name"]:
            self.name = engine["name"]
        returned = [c for c in (_candidate(item, self.source) for item in body["items"]) if c is not None]
        for i, c in enumerate(returned):  # arrival order IS the engine's ranking
            c.external_rank = i
        stats = candidates_mod.GatherStats()
        stats.trace = {
            "sources": [
                {
                    "source": self.source,
                    "status": "ok",
                    "contributed": len(returned),
                    "detail": f"{len(returned)} titles in {round((time.monotonic() - started) * 1000)} ms",
                    "queries": [],
                }
            ],
            **({"engine": body["trace"]} if isinstance(body.get("trace"), dict) else {}),
        }
        # None (no exclusion rule) means what it means to the built-in engine: keep the seeds out.
        watched = (
            req.watched_exclusions
            if req.watched_exclusions is not None
            else {(s.tmdb_id, s.media_type) for s in req.seeds}
        )
        dropped: list[tuple[Candidate, str]] = []
        if req.season is not None:
            in_season = [c for c in returned if req.season.contains(c.tmdb_id, c.media_type)]
            dropped.extend((c, "not_in_season") for c in returned if not req.season.contains(c.tmdb_id, c.media_type))
            returned = in_season
        if req.surface == "missing":
            # The request surface: what no library holds. The same watched and genre rules, and the
            # library as an EXCLUSION rather than the universe — an engine that lost track of what
            # the server holds must not offer a title that is already there.
            excluded = {g.lower() for g in req.excluded_genres}
            valid = []
            for c in returned:
                if req.library_index.get(c.media_type, {}).get(c.tmdb_id) is not None:
                    dropped.append((c, "in_your_libraries"))
                elif (c.tmdb_id, c.media_type) in watched:
                    dropped.append((c, "already_watched"))
                elif excluded and any(g.lower() in excluded for g in c.genres):
                    dropped.append((c, "excluded_genre"))
                else:
                    valid.append(c)
        else:
            valid = candidates_mod.filter_candidates(
                returned,
                req.library_index,
                watched_tmdb_ids=watched,
                excluded_genres=req.excluded_genres,
                dropped=dropped,
            )
        in_library = _media_filter(valid, req.media)
        if visible is not None and in_library and req.surface != "missing":
            in_library, hidden = _visible_candidates(ctx, in_library, visible)
            dropped.extend((c, "hidden_by_their_restrictions") for c in hidden)
        # The engine's order, cut per media type — never re-sorted. `external_rank` is stamped over the
        # SURVIVORS so it is dense; a gap would be harmless to the sort but misleading in the trace.
        ranked: list[Candidate] = []
        for kind in (MediaType.MOVIE, MediaType.SHOW):
            ranked.extend([c for c in in_library if c.media_type is kind][: req.limit_per_media])
        ranked.sort(key=lambda c: c.external_rank)  # type: ignore[arg-type, return-value]
        for i, c in enumerate(ranked):
            c.external_rank = i
        tally: dict[str, int] = {"kept": len(ranked), "lost_ranking_cutoff": len(in_library) - len(ranked)}
        for _, reason in dropped:
            tally[reason] = tally.get(reason, 0) + 1
        stats.trace["sources"][0]["disposition"] = {k: v for k, v in tally.items() if v}
        logger.debug(
            "{}: engine '{}' returned {} titles, {} in these libraries, {} kept",
            req.user.username,
            self.name,
            len(body["items"]),
            len(in_library),
            len(ranked),
        )
        household = body.get("household") if isinstance(body.get("household"), dict) else None
        return RecommendResult(
            ranked=ranked, in_library=in_library, gathered=[], ordered=True, stats=stats, household=household
        )


def _candidate(item: object, source: str) -> Candidate | None:
    """One engine item as a Candidate, or None for one too malformed to place (logged, never fatal)."""
    if not isinstance(item, dict):
        return None
    try:
        tmdb_id = int(item["tmdb_id"])
        media_type = MediaType(item["media_type"])
        title = str(item.get("title") or "")
    except (KeyError, ValueError, TypeError):
        logger.debug("engine item skipped — no usable tmdb_id/media_type: {!r}", item)
        return None
    seed = item.get("seed")
    seeds: list[Seed] = []
    if isinstance(seed, dict):
        try:
            seeds = [
                Seed(
                    tmdb_id=int(seed["tmdb_id"]),
                    title=str(seed.get("title") or ""),
                    media_type=MediaType(seed.get("media_type") or media_type.value),
                )
            ]
        except (KeyError, ValueError, TypeError):
            seeds = []
    year = item.get("year")
    return Candidate(
        tmdb_id=tmdb_id,
        title=title,
        media_type=media_type,
        year=int(year) if isinstance(year, int | float) else None,
        genres=[str(g) for g in item.get("genres") or []],
        rating=float(item.get("rating") or 0.0),
        vote_count=int(item.get("vote_count") or 0),
        poster_path=str(item.get("poster_path") or ""),
        overview=str(item.get("overview") or ""),
        language=str(item.get("language") or ""),
        seeds=seeds,
        # ONE source, the engine — `ranking.pre_rank` round-robins per source, so letting an engine's own
        # sub-sources through would let it reshuffle its own order past the cut. Its trace keeps them.
        sources={source},
        external_rank=0,  # stamped from arrival order by the caller
        reason=str(item["reason"]) if item.get("reason") else None,
        kids=bool(item.get("kids", False)),
    )


__all__ = ["EngineError", "HttpRecommender"]
