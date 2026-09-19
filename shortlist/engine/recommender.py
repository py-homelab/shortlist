"""The recommender boundary: what a candidate ENGINE is asked, and what it answers.

Shortlist's per-person run has always had one expensive, replaceable step in the middle — "given
this person's seeds, which titles should their rows draw from, in what order?" — sitting between
two things that are not replaceable: reading their history and library from Plex (before), and
delivering rows privately (after). This module names that step. ``rows.py`` builds one
:class:`RecommendRequest` per distinct candidate pool and hands it to ``ctx.recommender``; the
built-in engine (``recommenders/builtin.py``: TMDB similar/discover, Trakt, web search, the ranking
dials) is the default, and an out-of-process engine (``recommenders/http.py``) is a drop-in.

Everything either side of the call stays Shortlist's: watched/started exclusions, the genre and
visibility rules, the per-library selection, the delivery ledger and the share-filter privacy. An
engine ranks; it never touches Plex.

Engine code never imports the server, so nothing here knows about settings or the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from shortlist.engine.candidates import GatherStats
from shortlist.engine.models import Candidate, MediaType, Seed, UserProfile, WatchedItem

if TYPE_CHECKING:
    from collections.abc import Callable

    from shortlist.engine.context import EngineContext
    from shortlist.engine.seasons import SeasonTitles

#: What the request is for. Rows only ever ask for ``library`` — titles the delivery libraries hold.
#: ``missing`` (titles they do not hold) is reserved for the per-person request surface.
SURFACES = ("library", "missing")

#: ``Candidate.sources`` entry for a title an external engine produced, so provenance and the trace can
#: name it. The engine's own name follows the colon.
EXTERNAL_SOURCE_PREFIX = "engine:"


@dataclass(frozen=True)
class RecommendRequest:
    """One candidate pool's worth of input — everything ``rows.RowPolicy.pools_for`` resolved for it.

    Built once per distinct ``pool_key`` (sources, media, libraries, exclusion rule, seeds, season), so
    rows that resolve alike share one answer. The fields are exactly the arguments the built-in gather
    always took; naming them is what lets another engine take them instead.
    """

    user: UserProfile
    #: This row's seeds, derived by Shortlist from the person's history (recency-weighted, balanced
    #: across media, blocked and disliked titles already removed). An engine may seed from these or
    #: from ``history`` directly.
    seeds: list[Seed]
    #: Their watched titles with every tmdb_id resolved (``user.history`` holds Plex ratingKeys for
    #: most of them; ``RowPolicy.resolve`` maps those). Titles no id could be found for are left out.
    history: list[WatchedItem]
    #: tmdb_id -> ratingKey for the libraries this pool may recommend from — the candidate universe.
    #: Already narrowed to the row's own libraries when it is pinned to some.
    library_index: dict[MediaType, dict[int, int]]
    #: Titles the pool must not contain (watched / started, per the row's rule), or None when the row
    #: has no exclusion rule — which the built-in engine reads as "exclude the seeds". See
    #: ``RowPolicy.pool_exclusions`` for why the empty set and None are different answers.
    watched_exclusions: set[tuple[int, MediaType]] | None
    excluded_genres: set[str]
    #: "movie", "show" or "both" — what kinds of title this pool holds.
    media: str = "both"
    surface: str = "library"
    #: How many candidates to keep PER MEDIA TYPE after ranking (``EngineConfig.candidates_pre_rank``).
    #: The row build selects from this many; an engine returning more is truncated, never re-ranked.
    limit_per_media: int = 80
    #: The built-in engine's own dials. Ignored by an engine that ranks its own way.
    sources: tuple[str, ...] = ()
    recent_count: int = 0
    recency: float = 0.0
    season: SeasonTitles | None = None


@dataclass
class RecommendResult:
    """What an engine answered for one pool.

    ``gathered`` is every title the engine considered before the library narrowed it — the
    request-demand bookkeeping and a rewatch row's taste read it. An engine that ranks only what the
    library holds leaves it empty, and those readers see nothing rather than something wrong.
    ``in_library`` is the pool after the library, exclusion, genre and visibility filters and before
    the ``limit_per_media`` cut; ``ranked`` is after it. ``ordered`` says whether ``ranked`` is a FINAL
    order (the engine decided, and ``picker.diversify_by_seed`` must leave it alone) or a scored pool
    Shortlist still spreads across seeds per library, as the built-in engine has always had it.
    """

    ranked: list[Candidate]
    in_library: list[Candidate] = field(default_factory=list)
    gathered: list[Candidate] = field(default_factory=list)
    ordered: bool = False
    stats: GatherStats = field(default_factory=GatherStats)
    #: Who watches under this account, as the engine sees it: ``{"label", "kids_titles",
    #: "window_titles", "window_days"}``, or None when it cannot say (the built-in engine never can).
    #: Shortlist re-derives the label from the counts with its own thresholds (`household.py`).
    household: dict | None = None


class Recommender(Protocol):
    """A candidate engine. One instance serves the whole run; ``recommend`` is called per pool."""

    #: Shown in the trace and settings ("builtin", or the external engine's own name).
    name: str
    #: Can this engine answer for a person with fewer than ``min_history`` watches? The built-in one
    #: cannot (no seeds, no gather), so such a person takes the cold-start path before any pool is
    #: built. An engine that can — one with its own model of the household, say — is asked like anyone
    #: else, and only falls back to cold start if it answers with nothing.
    serves_cold: bool

    # Optional: `begin_run()` is called by `pipeline.run` before the first pool of a run, for an
    # engine that memoises anything — the run is the unit it may remember across.

    def recommend(
        self,
        ctx: EngineContext,
        req: RecommendRequest,
        *,
        visible: Callable[[list[int]], set[int] | None] | None = None,
    ) -> RecommendResult:
        """Rank one pool. ``visible`` answers "which of these ratingKeys can this person see?"
        (``RowPolicy.visible``; None from it = could not be checked, keep everything). It travels
        beside the request, not inside it: a live Plex read is not data, and an out-of-process engine
        cannot call it — the http recommender applies it to what comes back instead. Raise on a
        failure that leaves nothing to build from; the row then keeps what it has."""
        ...


def engine_status(recommender: object) -> dict | None:
    """The external engine's health for a run report, or None for the built-in engine.

    ``trouble`` is the one field a reader needs: a sentence when the engine's lists are stale, its last
    build failed, it could not be reached, or anyone fell back to the built-in engine tonight; None
    when all is well.
    """
    primary = getattr(recommender, "primary", recommender)
    info = getattr(primary, "info", None)
    if info is None and not hasattr(primary, "info"):
        return None
    info = info or {}
    fell_back = dict(getattr(recommender, "fell_back", {}) or {})
    reasons = sorted(set(fell_back.values()))
    trouble = None
    if info.get("unreachable"):
        trouble = f"could not be reached at the start of the run ({info['unreachable']})"
    elif info.get("stale"):
        age = info.get("age_hours")
        trouble = f"is serving lists {age} hours old" if age is not None else "has no usable lists"
        if info.get("last_build_error"):
            trouble += f" — its last build failed: {info['last_build_error']}"
    elif info.get("last_build_ok") is False:
        trouble = f"failed its last build ({info.get('last_build_error') or 'no reason given'}); its lists are older"
    if fell_back:
        extra = f"{len(fell_back)} {'person' if len(fell_back) == 1 else 'people'} got Shortlist's own picks instead"
        trouble = f"{trouble}; {extra}" if trouble else f"answered with nothing usable for some people — {extra}"
    return {
        "name": getattr(primary, "name", "external"),
        "info": {
            k: info.get(k)
            for k in (
                "version",
                "ready",
                "stale",
                "age_hours",
                "built_at",
                "last_build_at",
                "last_build_ok",
                "last_build_error",
                "unreachable",
            )
            if k in info
        },
        "fell_back": len(fell_back),
        "fallback_reasons": reasons[:3],
        "trouble": trouble,
    }


def is_external(candidate: Candidate) -> bool:
    """Whether an external engine placed this candidate — i.e. its order is final."""
    return candidate.external_rank is not None
