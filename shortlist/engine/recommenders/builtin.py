"""Shortlist's own candidate engine: TMDB similar/discover, Trakt, web search, the ranking dials.

This is the gather-and-rank step that lived inline in ``rows.py`` (``_candidate_pool``), moved
behind the :class:`~shortlist.engine.recommender.Recommender` protocol so another engine can stand in
its place. The code is the same; only its address changed. ``rows.py`` still owns everything around
it — which seeds to derive, what to exclude, how each library's row is then selected.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING

from shortlist.engine import candidates as candidates_mod
from shortlist.engine import ranking
from shortlist.engine import seasons as seasons_mod
from shortlist.engine.models import Candidate, MediaType
from shortlist.engine.recommender import RecommendRequest, RecommendResult

if TYPE_CHECKING:
    from shortlist.engine.context import EngineContext

# Genres TMDB tags children's titles with. Animation alone is not enough (adult animation is a
# genre of its own in practice), but Animation + Family together, or the TV "Kids" genre, is a
# reliable signal from the list payload every candidate already carries — no extra lookup.
_KIDS_GENRE = "Kids"
_KIDS_PAIR = frozenset({"Animation", "Family"})


def is_kids(genres: list[str]) -> bool:
    """Whether a title's TMDB genres mark it as children's / family viewing."""
    names = set(genres)
    return _KIDS_GENRE in names or names >= _KIDS_PAIR


def _run_year(run_day: int) -> int:
    """The calendar year this run belongs to — what "how old is this title" is measured against.

    Derived from ``run_day`` rather than read from the clock so ranking stays reproducible and the
    whole run agrees with itself: a run that starts at 23:59 on 31 December must not age half the
    roster's candidates against one year and half against the next.

    ``run_day`` is 0 for a direct engine call (the server always sets it). Falling back to today
    rather than to 0 keeps the setting working for CLI and library callers — `recency_factor` reads
    a 0 as "no opinion", which would have made this silently do nothing there.
    """
    return date.fromordinal(run_day).year if run_day > 0 else date.today().year


def _media_filter(items: list, media: str) -> list:
    """Keep only items of the row's media type ('both' keeps everything)."""
    if media == "both":
        return list(items)
    kind = MediaType(media)
    return [item for item in items if item.media_type is kind]


def _stamp_disposition(
    gather_stats: candidates_mod.GatherStats,
    *,
    dropped: list[tuple[Candidate, str]],
    in_library: list[Candidate],
    ranked: list[Candidate],
    recency: float = 0.0,
    year_now: int = 0,
) -> None:
    """Annotate the gather trace with each candidate's FATE, so the operator can follow every title
    from a source's returns to the row (or to the reason it fell out).

    Reads only the lists selection already produced (``dropped`` from filter_candidates, ``in_library``,
    ``ranked``) — it computes nothing new about which candidates win and mutates none of them. Two
    things are written onto ``gather_stats.trace``:

    * a per-source ``disposition`` tally: ``{kept, already_watched, not_in_your_libraries,
      excluded_genre, hidden_by_their_restrictions, lost_ranking_cutoff}`` counts, and
    * a ``fate``/``fate_reason`` on each already-recorded per-seed return, keyed by tmdb_id, and
    * the numbers that DECIDED that fate — the title's year, its rating, and the release-date weight
      applied to it. Without them the trace could say a title lost the cut but never why, so "it
      picked a 2003 film over a 2024 one" had no answer on the page built to answer it.

    A candidate that survived filtering but lost the ``candidates_pre_rank`` cut is
    ``lost_ranking_cutoff``; one that made the pre-rank is ``kept`` (whether or not it ends in the
    final row — the per-library row build, downstream of here, decides that and is traced separately
    by the delivered-picks stage).

    KNOWN GAP: this is stamped once per POOL, at the server's ``recommendations.recency``. A row that
    OVERRIDES that weight re-takes its own cut (``RowPolicy.cut_at_recency``) after this has run, so
    for that row the two disagree — a title it delivers can read ``lost_ranking_cutoff`` here. Every
    row that inherits the global (the default, and every row on a server that never overrides it) is
    stamped exactly right. Fixing it properly means stamping per row, which needs ``dropped`` carried
    past the gather; until then the delivered-picks stage remains the authority on what actually
    landed in a row.
    """
    ranked_ids = {(c.tmdb_id, c.media_type) for c in ranked}
    in_library_ids = {(c.tmdb_id, c.media_type) for c in in_library}
    # Every candidate we know anything about, dropped ones included — a title filtered out still has
    # a year worth showing next to the reason it went.
    known: dict[tuple[int, MediaType], Candidate] = {
        (c.tmdb_id, c.media_type): c for c in [*(cand for cand, _ in dropped), *in_library]
    }
    drop_reason: dict[tuple[int, MediaType], str] = {}
    for cand, reason in dropped:
        drop_reason.setdefault((cand.tmdb_id, cand.media_type), reason)

    def fate_of(tmdb_id: int, media: MediaType) -> str:
        key = (tmdb_id, media)
        if key in ranked_ids:
            return "kept"
        if key in in_library_ids:
            return "lost_ranking_cutoff"  # survived filtering but lost the pre-rank cut
        # Defensive fallback: a returned id with no matching pooled candidate. Shouldn't occur —
        # every returned title is added to the pool, so it resolves to a real fate above.
        return drop_reason.get(key, "not_returned")

    for source in gather_stats.trace.get("sources", []):
        tally: dict[str, int] = {}
        for query in source.get("queries", []):
            qmedia = MediaType.SHOW if query.get("media") == "show" else MediaType.MOVIE
            for ret in query.get("returned", []):
                tmdb_id = int(ret.get("tmdb_id") or 0)
                verdict = fate_of(tmdb_id, qmedia)
                ret["fate"] = verdict
                cand = known.get((tmdb_id, qmedia))
                if cand is not None:
                    ret["year"] = cand.year
                    ret["rating"] = round(cand.rating, 1) if cand.rating else None
                    # The age multiplier this title was actually judged with. 1.0 when the setting is
                    # off or the title has no known year, which is the honest answer in both cases —
                    # and the UI omits it rather than printing a meaningless "x1.0".
                    ret["age_weight"] = round(ranking.recency_factor(cand.year, year_now, recency), 3)
                tally[verdict] = tally.get(verdict, 0) + 1
        if tally:
            source["disposition"] = tally

    # The web-search source records its proposals under trace["web"], not as per-seed `queries`, so it
    # needs the same fate stamp separately: which AI-proposed titles made this library's shortlist vs
    # fell out. Hallucinations (no TMDB match) never reach `proposals`, so they carry no fate — the UI
    # still strikes them through from the `unresolved` list.
    for proposal in gather_stats.trace.get("web", {}).get("proposals", []):
        pmedia = MediaType.SHOW if proposal.get("media") == "show" else MediaType.MOVIE
        proposal["fate"] = fate_of(int(proposal.get("tmdb_id") or 0), pmedia)


def _visible_candidates(
    ctx: EngineContext, candidates: list[Candidate], visible: Callable[[list[int]], set[int] | None]
) -> tuple[list[Candidate], list[Candidate]]:
    """``(kept, hidden)``: candidates with at least one copy this person can see, and the rest (#115).

    EVERY copy, not the candidate's own ratingKey. The pool's key comes from the union library index,
    which keeps whichever library was indexed last — so a title in both "Movies" and "4K Movies"
    carries the 4K key, and a person shared only "Movies" would read it as hidden (recorded: an unshared
    library's items read exactly like filtered ones). The delivery loop re-checks each library's own copy.
    Nothing is dropped when the check could not be made.
    """
    kinds = {
        section.key: (MediaType.MOVIE if section.type == "movie" else MediaType.SHOW)
        for section in ctx.delivery_sections
    }

    def copies(c: Candidate) -> set[int]:
        keys = {c.rating_key} if c.rating_key is not None else set()
        for section_key, index in ctx.section_index.items():
            if kinds.get(section_key) is c.media_type and c.tmdb_id in index:
                keys.add(index[c.tmdb_id])
        return keys

    by_candidate = [(c, copies(c)) for c in candidates]
    seen = visible(sorted({k for _, keys in by_candidate for k in keys}))
    if seen is None:
        return list(candidates), []
    kept = [c for c, keys in by_candidate if keys & seen]
    hidden = [c for c, keys in by_candidate if not keys & seen]
    return kept, hidden


def _candidate_pool(
    ctx: EngineContext,
    seeds: list,
    library_index: dict[MediaType, dict[int, int]],
    *,
    excluded_genres: set[str],
    profile=None,
    sources: list[str] | None = None,
    media: str = "both",
    watched_exclusions: set[tuple[int, MediaType]] | None = None,
    recent_count: int | None = None,
    recency: float = 0.0,
    visible: Callable[[list[int]], set[int] | None] | None = None,
    season: seasons_mod.SeasonTitles | None = None,
) -> tuple[tuple[list[Candidate], list[Candidate], list[Candidate]], candidates_mod.GatherStats]:
    """Gather TMDB candidates for ``seeds`` and intersect them with the library.

    Returns ``((pool, in_library, ranked), gather_stats)`` — the 3-tuple of candidate lists, plus the
    AI token/Exa spend the gather incurred (for per-run cost accounting):

    * ``pool`` — every pooled candidate (used for request-demand bookkeeping before narrowing).
    * ``in_library`` — the ones the delivery libraries actually hold and this user may still see.
    * ``ranked`` — the pre-ranked candidates the curator chooses from.

    ``media`` narrows the pool BEFORE the pre-rank truncation. Filtering after it meant a
    movie-heavy watcher's shows-only row could lose every show to the 40-candidate cut and deliver
    nothing — a dead row on a green run. Identity is (tmdb_id, media_type), never the bare id — movie
    1399 and TV 1399 are different titles.

    (No staleness partition anymore: rows now carry their prior picks forward on non-refresh nights,
    so there's nothing to "hold back" — see ``_reusable_prior`` / ``_is_refresh_night``.)
    """
    # The titles this person has already watched (per the row's policy), not just the ~30 seeds — a
    # recommendation you've finished is the exact thing the row shouldn't surface. Falls back to the
    # seed set when the row has no exclusion rule (see `RowPolicy.pool_exclusions` for the None sentinel).
    watched_ids = watched_exclusions if watched_exclusions is not None else {(s.tmdb_id, s.media_type) for s in seeds}
    # Blocked titles are dropped at SEED DERIVATION only (`derive_seeds(..., blocked=...)`, called by
    # this row's caller before `seeds` reaches here) — nothing downstream re-checks `blocked_seeds`,
    # so a blocked title may still surface if a different seed's similar-titles search suggests it.
    # Matches the UI's "Don't seed" wording, which promises exactly that and no more.
    gather_stats = candidates_mod.GatherStats()
    pool = candidates_mod.gather_candidates(
        ctx.tmdb,
        seeds,
        sources=sources if sources is not None else ctx.config.candidate_sources,
        curator=ctx.curator,
        profile=profile,
        trakt=ctx.trakt,
        search=ctx.search,
        web_search_mode=ctx.config.web_search_provider,
        web_search_cache=ctx.web_search_cache,
        recent_count=recent_count if recent_count is not None else ctx.config.recent_count,
        stats=gather_stats,
        # Only the kinds of title this row holds: a films row offered the season's shows would count them as a
        # working source, and the media filter below would then empty the pool without the gather failing.
        season_items=(
            {kind: items for kind, items in season.in_library.items() if media in ("both", kind.value)}
            if season is not None
            else None
        ),
    )
    # `dropped` collects (candidate, reason) as filter_candidates works — observation only, it does
    # not change which candidates are kept.
    dropped: list[tuple[Candidate, str]] = []
    if season is not None:
        # A seasonal row holds its season and nothing else. Filtered against EVERY title in the season,
        # not only the ones on the server, and before the demand bookkeeping reads `pool` — so a missing
        # Christmas film similar to their watches can still be requested, and nothing else can be.
        in_season = [c for c in pool if season.contains(c.tmdb_id, c.media_type)]
        dropped.extend((c, "not_in_season") for c in pool if not season.contains(c.tmdb_id, c.media_type))
        pool = in_season
    valid = candidates_mod.filter_candidates(
        pool,
        library_index,
        watched_tmdb_ids=watched_ids,
        excluded_genres=excluded_genres,
        dropped=dropped,
    )
    in_library = _media_filter(valid, media)
    # What this PERSON can see, BEFORE the pre-rank cut (#115). A pick their Plex restrictions hide is
    # invisible in their row, so an allow-list account was handed rows Plex emptied. Checked here, ahead
    # of the cut, so the cut fills from titles they can actually watch. None = could not be checked.
    if visible is not None and in_library:
        in_library, hidden = _visible_candidates(ctx, in_library, visible)
        dropped.extend((c, "hidden_by_their_restrictions") for c in hidden)
    # Measure genre avoidance BEFORE the cut, so the dial can rescue or demote a title across the
    # truncation boundary rather than only reordering whatever already survived — the same reason
    # `recency` participates in the cut. A no-op unless the owner turned the dial up, and the
    # library tally is empty in that case, so nothing is computed either.
    candidates_mod.stamp_genre_penalties(ctx.tmdb, in_library, seeds, ctx.library_genre_counts)
    # Seed-side only, so it costs a handful of calls whatever the pool size — safe to run before
    # the cut, where it can still rescue a sequel that would otherwise fall below the cap.
    if ctx.config.franchise > 0:
        candidates_mod.mark_franchise_members(in_library, ctx.tmdb)
    # Pre-rank EACH media type to its own cap, not the mixed pool to one cap — otherwise a 'both'
    # row whose pool skews one way (a mostly-TV watcher) truncates the other type away before the
    # per-media curate ever sees it, and that library's collection comes up empty.
    kinds = [MediaType.MOVIE, MediaType.SHOW] if media == "both" else [MediaType(media)]
    cap = ctx.config.candidates_pre_rank
    # `recency` is the weight the CALLER resolved, not `ctx.config.recency`: the server's value, so every
    # row that inherits it shares one cached cut, and a row that overrides it re-cuts (`cut_at_recency`).
    ranked = ranking.cut_for_recency(
        in_library,
        kinds,
        cap,
        recency,
        _run_year(ctx.run_day),
        ctx.config.genre_avoidance,
        ctx.config.franchise,
        ctx.config.cast,
    )
    # AFTER the cut, unlike the other two: cast overlap needs both sides' cast lists, so it is
    # the one signal whose cost scales with the pool. Bounded here to `candidates_pre_rank`
    # rather than the raw gather. It re-orders `ranked` and never changes its membership.
    if ctx.config.cast > 0:
        candidates_mod.enrich_cast_affinity(ranked, ctx.tmdb, seeds)
        ranked = ranking.cut_for_recency(
            ranked,
            kinds,
            cap,
            recency,
            _run_year(ctx.run_day),
            ctx.config.genre_avoidance,
            ctx.config.franchise,
            ctx.config.cast,
        )
    # Stamp each traced return with its fate (kept as a candidate, or dropped and why), derived
    # entirely from the lists selection already produced above — so the trace can follow every title
    # in and out without altering a single delivered pick.
    _stamp_disposition(
        gather_stats,
        dropped=dropped,
        in_library=in_library,
        ranked=ranked,
        recency=recency,
        year_now=_run_year(ctx.run_day),
    )
    return (pool, in_library, ranked), gather_stats


class BuiltinRecommender:
    """The engine Shortlist ships with. Stateless: every dial arrives on the request or the context."""

    name = "builtin"
    # No seeds means no gather, so a person under `min_history` is cold-started before any pool is built.
    serves_cold = False

    def recommend(
        self,
        ctx: EngineContext,
        req: RecommendRequest,
        *,
        visible: Callable[[list[int]], set[int] | None] | None = None,
    ) -> RecommendResult:
        (pool, in_library, ranked), stats = _candidate_pool(
            ctx,
            req.seeds,
            req.library_index,
            excluded_genres=req.excluded_genres,
            profile=req.user,
            sources=list(req.sources) if req.sources else None,
            media=req.media,
            watched_exclusions=req.watched_exclusions,
            recent_count=req.recent_count,
            recency=req.recency,
            visible=visible,
            season=req.season,
        )
        for c in in_library:
            c.kids = is_kids(c.genres)
        return RecommendResult(ranked=ranked, in_library=in_library, gathered=pool, ordered=False, stats=stats)
