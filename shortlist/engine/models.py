"""Engine dataclasses: inputs, intermediate stages, and run reports."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class MediaType(StrEnum):
    MOVIE = "movie"
    SHOW = "show"


class UserType(StrEnum):
    OWNER = "owner"
    SHARED = "shared"
    MANAGED = "managed"


# Every Plex label/collection Shortlist owns starts with this. Was also an `EngineConfig` field,
# but nothing ever assigned it a non-default value, so `UserProfile.label` had already hardcoded
# the literal separately — one knob nobody turned, and one hardcode that could drift from it. This
# constant is now the single place either could change.
LABEL_PREFIX = "shortlist"


def is_human_rating(value: float | None) -> bool:
    """Whether a Plex ``userRating`` was plausibly set by a PERSON rather than by a tool.

    Plex's own rating controls write whole numbers only — five stars in half-star steps on a 0..10
    scale, and thumbs, which land on the same grid. Nothing a user can press produces 6.2.

    Tools do. Kometa's rating sync writes IMDb/TMDB scores straight into ``userRating``, and those
    carry a decimal: measured on the maintainer's server, 1,455 of the owner's 1,630 watched titles
    were rated this way and 90.7% of the values were fractional, against 0 of 36 among the 49 real
    viewers. Treating those as opinions would have silently stopped dozens of the owner's own seeds
    on IMDb's say-so. Coexisting with Kometa is a standing rule here (plex-safety 4), and this is
    that rule applied to a field rather than to a collection.

    It cannot catch a tool's value that lands on a whole number by chance (~9% of them did), which is
    why `history.ratings_are_trustworthy` judges the account as well.

    Args:
        value: A 0..10 ``userRating``, or None for a title nobody rated.

    Returns:
        False for None — "not rated" is not a rating, let alone a human one.
    """
    return value is not None and float(value).is_integer()


def slugify(name: str) -> str:
    """Normalize a username into the slug used in labels: ``shortlist_<slug>``."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or "user"


def dedupe_slug(base: str, is_taken: Callable[[str], bool]) -> str:
    """Return ``base``, or ``base_2``, ``base_3``, … — the first that ``is_taken`` reports free.

    Slugs are what row labels are built from and must be unique per owner: two Plex display names
    can slugify alike (Plex names are free text), so the second claimant gets a numeric suffix
    rather than colliding onto the first's label — and their private row.
    """
    slug = base
    n = 2
    while is_taken(slug):
        slug = f"{base}_{n}"
        n += 1
    return slug


@dataclass(frozen=True)
class WatchedItem:
    """One watched title from the user's library, as Plex records it FOR THEM.

    The share-token source reads this straight from the PMS as the user (``unwatched=0``), so one item
    is one distinct TITLE they've watched — carrying Plex's own per-user counts — not one play event.
    ``watch_count`` is therefore the frequency signal (see below) rather than something a caller derives
    by counting duplicate rows.
    """

    title: str
    media_type: MediaType
    watched_at: datetime
    tmdb_id: int | None = None
    year: int | None = None
    rating_key: int | None = None
    completion: float = 1.0  # 0..1 fraction watched
    # How much this title was watched, the frequency half of a seed's weight. For a MOVIE it's the
    # play count (``viewCount``); for a SHOW it's episodes watched (``viewedLeafCount``) — so a show
    # binged 50 episodes deep weighs like 50 movie plays, matching the old per-play behaviour without
    # the source emitting 50 rows. Defaults to 1 so a single-play source reads as one watch.
    watch_count: int = 1
    # A show's per-user watched fraction, straight from Plex (``viewedLeafCount``/``leafCount``) —
    # marks included. The finished-show check reads these directly instead of reconstructing the
    # fraction from play counts. None for movies and for sources that don't report episode totals.
    viewed_leaf_count: int | None = None
    leaf_count: int | None = None
    # For a show watch, the specific episode behind it (the show name is `title`). Display only, never
    # used for seeding. Always None from ShareTokenWatchSource, which reads watched STATE at the show
    # level (viewedLeafCount), not per-episode play events — the recent-watches panel renders these
    # only when present, so it degrades to the show name. A seam for any future per-episode source.
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    # What THIS person rated the title in Plex, 0..10 (5 stars x 2), or None if they haven't rated it
    # — which is 99.7% of watches on a real server, so None is the case to optimise for. Per-account:
    # the share-token read returns the rating belonging to the token it was read with, never the
    # owner's. Only ever a whole number; `is_human_rating` explains why a fractional one is discarded.
    user_rating: float | None = None

    @property
    def is_human_rating(self) -> bool:
        """Whether `user_rating` was plausibly set by a PERSON rather than a tool — see the module
        function of the same name."""
        return is_human_rating(self.user_rating)

    @property
    def is_finished(self) -> bool:
        """Did they finish this, as opposed to merely starting it?

        A MOVIE is finished whenever it is here at all: this type only ever holds titles Plex has
        already flagged watched, and for a movie that flag means played.

        A SERIES needs every episode. That threshold is OURS and has to be — Plex publishes no
        show-level watched flag, only ``viewedLeafCount``/``leafCount`` (recorded:
        ``tests/fixtures/pms_watched_shows.xml.txt``, where a show 2 episodes into 176 comes back as
        "watched"). Of the thresholds available this is the strictest and the least arguable, and it
        is already the wording the user page shows per title ("3 of 12 episodes" / "finished").

        Deliberately NOT the engine's already-seen bar (`rows._watched_titles`, effectively 3
        episodes or 15%): that one answers "engaged enough not to recommend this again?", which is a
        different question with a legitimately looser answer.

        A series whose episode total is unknown reads as UNFINISHED — the opposite of the
        already-seen rule, which counts it as watched rather than risk re-recommending. Here the
        cautious direction is the other way: "we cannot show they finished it" must not become a
        claim that they did.
        """
        if self.media_type is MediaType.MOVIE:
            return True
        if not self.leaf_count:
            return False
        return (self.viewed_leaf_count or 0) >= self.leaf_count


@dataclass(frozen=True)
class Seed:
    """A history title used to seed candidate discovery."""

    tmdb_id: int
    title: str
    media_type: MediaType
    weight: float = 1.0  # recency/frequency weight
    # The two ingredients behind `weight` (weight = watch_count x recency decay), kept so the trace
    # can explain a seed's influence in plain terms ("watched 4x, last 3 days ago") instead of a bare
    # number. Display only — ranking reads `weight`, never these.
    watch_count: int = 1
    recency_days: int = 0  # days between this title's most-recent watch and the newest watch overall


@dataclass(frozen=True)
class Attribution:
    """One signal's strongest evidence for a candidate — which watched title, and why.

    Separate from `Candidate.top_seed`, which answers "which seed weighed most" and drives the row
    NAME and `Pick.seed_title`. This answers "what can we honestly tell the person", which is a
    different question once more than one signal fires: a sequel found through a shared franchise and
    a title found through a shared lead are both "because you watched Dune", and saying only that
    throws away the part that would actually explain it.

    Deliberately not persisted. `Pick.reason` is already a plain string and already survives a
    carried-forward row, so the richer sentence is rendered into it and this stays live for one run —
    exactly as `top_seed` is Candidate-only and only its flattened `seed_title`/`seed_tmdb_id` reach
    the database.
    """

    signal: str  # "similarity" | "franchise" | "cast"
    seed_title: str
    seed_tmdb_id: int
    detail: str = ""  # the shared actor's name, or the franchise's


@dataclass
class Candidate:
    """A TMDB-suggested title, later intersected with the library."""

    tmdb_id: int
    title: str
    media_type: MediaType
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    rating: float = 0.0  # TMDB vote_average, 0..10
    vote_count: int = 0  # TMDB vote_count — a 9.0 from 12 votes is noise
    # TMDB's own poster path ("/abc.jpg"), free in every list response. Only carried through to the
    # person's request surface, which shows the artwork — a delivered pick uses Plex's copy of the title.
    poster_path: str = ""
    # TMDB's synopsis, free in the same list response as the poster and carried the same way: only
    # the request surface reads it, so a person can judge an unfamiliar title without leaving the page.
    overview: str = ""
    # TMDB's `original_language` (ISO 639-1, lowercase: "en", "ja", "ko"), free in every TMDB list
    # response and shown on the request surface. Empty means UNKNOWN, not English: `merge()` builds
    # candidates from a non-TMDB source's own fields and never sees a TMDB payload, so Trakt titles
    # arrive without one.
    language: str = ""
    seeds: list[Seed] = field(default_factory=list)  # every seed that suggested it
    rating_key: int | None = None  # set once matched to the library
    # Which candidate source(s) produced it. Ranking needs this: seedless sources (tmdb_discover,
    # llm_web) would otherwise be crowded out wholesale by the seeded ones — see ranking.pre_rank,
    # which gives each source a fair share of the pool it draws the row from.
    sources: set[str] = field(default_factory=set)
    # How strongly the source that produced it vouched for it, 0..1. TMDB sets this from which
    # endpoint suggested the title and how near the top of that list it sat — the similarity signal
    # that used to be discarded. Sources with no ranking of their own (discover, Trakt, the web
    # source) keep the neutral 1.0: they are deliberate picks, not the tail of a list, and
    # penalising them for lacking a signal they never had is what `pre_rank`'s round-robin exists to
    # prevent. A title several seeds suggested keeps the strongest claim any of them made.
    affinity: float = 1.0
    # This candidate's measured genre-avoidance signal: the mean of its genres' shrunk log ratios,
    # negative half only (see `candidates.candidate_genre_penalty`). 0.0 = no opinion, which is what
    # every candidate carries until the owner turns `recommendations.genre_avoidance` up. A log2
    # adjustment, not a multiplier — `ranking.negative_multiplier` combines it with any future
    # negative signal BEFORE flooring, so dampeners can never compound into a floor nobody chose.
    genre_penalty: float = 0.0
    # Shares a TMDB collection with one of its own seeds — the sequel/prequel signal. Movie-only:
    # TMDB has no `belongs_to_collection` for TV, so this is inert for shows by construction.
    in_seed_franchise: bool = False
    # 0..1, how much top-billed cast this shares with its seeds, with prolific actors already
    # discounted (see `candidates.cast_idf`). 0.0 = no shared cast, or the signal is switched off.
    cast_overlap: float = 0.0
    # Every signal that fired for this candidate, for the "why you're seeing this" line. Empty until
    # a signal beyond the plain seed match actually has something to add.
    attributions: list[Attribution] = field(default_factory=list)
    # Set by an EXTERNAL engine (`recommenders/http.py`): its position in that engine's final order,
    # 0 first. When set, `ranking._sort_key` orders by it and nothing else, and `diversify_by_seed`
    # leaves the order alone — the engine decided, and re-scoring its answer with the built-in formula
    # would hand back a row the engine never ranked. None for every built-in candidate.
    external_rank: int | None = None
    # A "why you're seeing this" line the engine wrote itself; `picker.reason_for` prefers it to its own
    # template when set. None = build it from the seed and genres as always.
    reason: str | None = None
    # Children's / family title, as the engine judged it (certification, or Animation + Family genres).
    # A row's `family` setting reads this; the built-in engine derives it from genres alone.
    kids: bool = False

    @property
    def seed_frequency(self) -> int:
        return len(self.seeds)

    @property
    def top_seed(self) -> Seed | None:
        return max(self.seeds, key=lambda s: s.weight) if self.seeds else None


@dataclass(frozen=True)
class Pick:
    """A final ranked recommendation delivered to the user's row.

    `media_type` decides which library the pick's collection lives in. Plex collections belong
    to exactly one library section, and a collection holding items of the wrong type is matched
    by neither `filterMovies` nor `filterTelevision` — so it can never be hidden from other
    users. Delivering a show into a movie collection is therefore a privacy bug, not a cosmetic
    one (SFLIX, 2026-07-12).
    """

    tmdb_id: int
    rating_key: int
    title: str
    rank: int
    reason: str
    media_type: MediaType  # required on purpose: a forgotten default is exactly the bug above
    seed_tmdb_id: int | None = None
    seed_title: str | None = None
    collection_slug: str = ""  # which row produced it, so a user's picks can be grouped per row
    # The library this pick was delivered into. A row targeting >1 library becomes one Plex collection
    # PER library, so effectiveness is tracked per (row, library): `section_key` is the stable Plex
    # section key, `library` its display name ("Movies") for the report label.
    section_key: str = ""
    library: str = ""
    # Which candidate source(s) surfaced this title, and how strongly they vouched for it. Carried
    # all the way to the UI so "why is this here?" is answerable without reading a log: a pick that
    # came from the tail of TMDB's list should LOOK different to one an LLM chose deliberately.
    sources: list[str] = field(default_factory=list)
    affinity: float = 1.0
    # Carried from the Candidate purely so a row can be ORDERED by them (RowSpec.order). Persisted on
    # PickRow, because a carried-forward pick is rebuilt from the DB and would otherwise sort as if it
    # had no rating and no release year. TMDB's vote_average and release year — the only two the
    # candidate pool already holds, so ordering by them costs no extra lookups.
    rating: float = 0.0
    year: int | None = None
    # The `row_recipe` this pick was built under. Compared against tonight's on the next run: a
    # mismatch means the owner changed a setting that decides row contents, so the row rebuilds
    # instead of waiting for its refresh cadence.
    recipe: str = ""
    # When this row's CONTENTS were last chosen — stamped on a rebuild/refresh and carried through
    # untouched on every reuse night. Deliberately not `PickRow.created_at`, which is re-stamped
    # every run because a carried-forward row is re-persisted under the new run: that says "last
    # delivered", and the idle hold needs "last decided". None on picks written before this existed,
    # which reads as "unknown" and falls back to the plain cadence (`_held_for_idle`).
    built_at: datetime | None = None


@dataclass
class RowOverride:
    """One person's per-row tweaks. Any None/False field falls through to the row's own settings."""

    muted: bool = False  # this person doesn't get this row at all
    size: int | None = None  # override the row's size for this person
    recent_count: int | None = None  # override how many recent watches the web-search source searches


@dataclass
class UserProfile:
    """Everything the pipeline needs to know about one enabled user."""

    username: str
    plex_account_id: int
    user_type: UserType
    slug: str = ""
    # What a human should be called in a row title: their Shortlist nickname, else the friendly name
    # Tautulli knows them by, else their Plex username. Purely cosmetic — the SLUG (and therefore the
    # `shortlist_<slug>` label every share filter excludes) is derived from the username and never
    # moves, so renaming someone can't strand the exclusions that keep their row private.
    nickname: str = ""
    history: list[WatchedItem] = field(default_factory=list)
    #: Whether `history` came from a read that can be trusted to have returned EVERYTHING — the only
    #: thing that makes ABSENCE from it evidence. False by default, and deliberately so: withdrawing
    #: pick credit acts on absence and cannot be undone, so a caller that has not proved completeness
    #: must not be able to get it wrong by omission. A fail-soft read that skipped one unreadable
    #: library still returns the other libraries' titles, which is non-empty and looks complete.
    history_complete: bool = False
    excluded_genres: set[str] = field(default_factory=set)
    blocked_seeds: set[int] = field(default_factory=set)
    row_name_template: str | None = None
    # Per-row overrides keyed by collection slug; a slug absent here uses the row's own settings.
    row_overrides: dict[str, RowOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = slugify(self.username)

    @property
    def display_name(self) -> str:
        """What `{user}` renders as — the nickname when they have one, else their Plex username."""
        return self.nickname.strip() or self.username

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}_{self.slug}"


# Shared ("popular on this server") rows live in a namespace no per-person label can collide with.
# `slugify` collapses any run of non-alphanumerics to a SINGLE "_" and strips leading ones, so a
# username can never produce a slug containing "__" — the DOUBLE underscore here makes a shared
# label unreachable from any user slug, so a private row can never be mistaken for a shared one.
SHARED_SLUG_PREFIX = "shared"
SHARED_LABEL_PREFIX = f"{LABEL_PREFIX}__shared_"


@dataclass
class PosterSpec:
    """How a row's Plex collection poster image is produced. Purely cosmetic — a poster never affects
    privacy, promotion, or the leak-safe ordering, and failing to make one never fails a run.

    ``mode`` is "upload" (use ``image`` bytes as-is on every collection of the row) or "generate"
    (compose a prompt from the text fields — which may contain the same ``{user}``/``{library_name}``/
    ``{top_seed}`` placeholders as a row name — and render it with the injected PosterArtist). An empty
    ``mode`` means "leave Plex's own artwork alone".
    """

    mode: str = ""
    image: bytes | None = None  # upload mode: the raw image bytes (the adapter loads them from poster_assets)
    title: str = ""  # generate mode: the headline text
    subtitle: str = ""  # generate mode: secondary text
    style: str = ""  # generate mode: art-style guidance


@dataclass(frozen=True)
class RowSeason:
    """The season a seasonal row builds for on this run (discussion #124), resolved by the server.

    ``anchor`` is that year's day of the season (25 Dec 2026), which is what tells two Christmases apart:
    a new one rebuilds the row rather than carrying last year's picks forward.
    """

    slug: str
    name: str
    emoji: str
    anchor: date


#: `RowSpec.family` values. "include" is the default and the pre-1.10 behaviour.
FAMILY_MODES = ("include", "exclude", "only")


@dataclass
class RowSpec:
    """One curated-row definition the engine delivers, built by the adapter from a Collection row.

    A per-person spec produces one private row per audience member (label ``shortlist_<userslug>``); a
    shared spec produces one public row for the whole audience (label ``shortlist__shared_<slug>`` —
    the DOUBLE underscore puts it in a namespace no user slug can ever collide with; see
    ``SHARED_LABEL_PREFIX``).
    """

    slug: str
    name_template: str
    size: int
    media: str = "both"  # movie | show | both — the type filter; library_keys narrows to specific libraries
    # Specific Plex library section keys to deliver this row into; empty -> every library of the
    # allowed media type (the default, so a server with one movie + one show library is unchanged).
    library_keys: list[str] = field(default_factory=list)
    shared: bool = False
    # None -> visible to everyone; otherwise the set of plex_account_ids this row is built for / seen by.
    audience: set[int] | None = None
    # Shared rows only: a title must have been watched by at least this many distinct people to
    # qualify, so no one person's solo viewing can reach a public row (aggregate-privacy floor).
    min_watchers: int = 2
    # Per-row override of which discovery sources feed this row; empty -> inherit EngineConfig.candidate_sources.
    candidate_sources: list[str] = field(default_factory=list)
    # Per-row cap on already-watched titles, as a fraction of the row (0.0 = all fresh, 1.0 = no
    # filtering). None -> inherit EngineConfig.watched_pct.
    watched_pct: float | None = None
    # Build a REWATCH row: already-finished titles are what the row is FOR, so they are ordered first
    # and unwatched ones only fill what's left.
    #
    # `watched_pct` cannot express this. It is a CEILING — `_apply_watched_cap` shows unwatched titles
    # first and merely PERMITS up to that fraction of finished ones — so on a library with plenty of
    # unwatched candidates even 1.0 yields a mostly-unwatched row. A row named "Happy to see again"
    # needs the opposite preference, which is this flag.
    #
    # Its finished titles come from the person's own HISTORY (`rows._rewatch_candidates`), never from
    # the similar-titles pool: that pool only holds a finished title when a different watch's search
    # happens to name it, so a person with hundreds of finished films got a row of two (issue #114).
    rewatch: bool = False
    # Rewatch rows only: leave out anything they finished within this many days, so the shelf holds
    # old favourites rather than last night's film. 0 = no cooldown.
    rewatch_cooldown_days: int = 30
    # Shows only: drop any series this person has STARTED, however little of it. Stricter than the
    # normal watched filter, which only drops shows they have FINISHED (>= watched_show_pct) — one they
    # are three episodes into is otherwise still eligible. This is what makes "a series to start" true.
    # Meaningless for movies (a movie with any view is already finished), so it applies to shows only.
    unstarted_only: bool = False
    # Children's / family titles (`Candidate.kids`): "include" keeps them with everything else — how
    # every row has always behaved; "exclude" keeps them out; "only" builds the row from nothing else.
    # For a household watching under one account: the grown-ups' rows exclude, and one row is the
    # family's. Decided from what the engine tagged, never from the person's Plex restrictions.
    family: str = "include"
    # How often this row re-picks its titles, in DAYS: 0 = never once built (frozen), 1 = nightly,
    # N = every N days. None -> inherit EngineConfig.refresh_days.
    refresh_days: int | None = None
    # How long this row may wait when its owner has watched nothing since it was last built, in DAYS.
    # 0 = never wait (rebuild on the cadence whatever they did); None -> inherit
    # EngineConfig.idle_hold_days. An explicit 0 is a real choice, not an absent one — it is how a
    # single row stays lively on a server that holds everything else.
    idle_hold_days: int | None = None
    # How much a title's RELEASE DATE counts when ranking it: 0.0 = ignore age, 1.0 = strongly prefer
    # new. None -> inherit EngineConfig.recency.
    #
    # Not the same axis as `refresh_days`, and the pair is the reason the UI label is "Recent
    # releases" rather than "Newness": that is a CADENCE (how often this row
    # re-picks), this is a PREFERENCE (which titles win when it does). A row can rebuild nightly and
    # still fill with 1990s titles — that combination is exactly what this setting exists for.
    recency: float | None = None
    # How many of this person's most recent watched titles the WEB-SEARCH source searches for this row
    # — one cached search per title ("what to watch if you liked X"). Fewer = tighter/cheaper, more =
    # broader reach. Only affects the llm_web source; TMDB/Trakt still use the full seed set. None ->
    # inherit EngineConfig.recent_count.
    recent_count: int | None = None
    # How many of this person's watched titles SEED this row — the titles every source searches from.
    # Unlike recent_count (which only caps the web-search source), this caps the seed set itself, so it
    # decides what the whole row is derived from. Small values make a row about one or two things they
    # actually watched, which is what a `{top_seed}` ("Because you watched X") title claims; the default
    # blends the whole recent history. None -> inherit EngineConfig.max_seeds.
    max_seeds: int | None = None
    # What this row does for someone with too little history to recommend from ("popular" = the
    # cold-start fallback of top-rated titles, "skip" = don't build it for them at all).
    # None -> inherit EngineConfig.cold_start.
    #
    # Per ROW, not just global, because the right answer differs row to row: a `{top_seed}`
    # ("Because you watched X") row has no seed for a cold user and degrades to the bare default
    # title, so skipping it is usually right — while a plain "Picked for You" row is perfectly happy
    # holding popular titles. Deliberately NOT a per-person override: it answers "what is this row
    # for", like `pick_order`, not "how does this person want it".
    cold_start: str | None = None
    # The name to use when `name_template` cannot be rendered — a `{top_seed}` row for someone with
    # no watch behind any of their picks. Empty means the row is NOT built for that person, because
    # the engine never substitutes a name of its own (issue #84).
    #
    # Added LAST, deliberately: several call sites build a RowSpec positionally, so a new field in
    # the middle silently shifts every argument after it. The same hazard bit HubAnchor.anchor_row.
    fallback_name: str = ""
    # How many of this person's most recent watches this row may be built from, of which ONE (per media
    # type) is chosen each run — the row cycles a step a day rather than sitting on their newest watch
    # for ever. 1 (the default) is the original behaviour: always the most recent.
    #
    # Distinct from `max_seeds`, which is the axis people reach for first and the wrong one: raising
    # that BLENDS more watches into one row, diluting the very claim a `{top_seed}` title makes. This
    # keeps the row about a single watch and moves WHICH one (issue #57).
    #
    # Per-row with a plain default rather than an inheritable global (like `pick_order`): whether a row
    # is about one fixed watch or a rotation is a property of what that row IS, not a server policy.
    seed_window: int = 1
    # How this row's picks are ORDERED in the delivered collection — "best" (our ranking), "rating"
    # (highest TMDB score first), "newest" (most recent release first), "shuffle" (a different order
    # each day), "new_first" (titles that arrived this run lead) or "rotate" (the front advances by one
    # title a day, so every pick gets a turn there). Plex only sorts a collection by release date,
    # alphabetically, or by the custom order we write, so every one of these is applied here and
    # delivered as that custom order.
    #
    # "new_first" and "rotate" are issue #63's two asks. Both are PRESENTATION, like the rest of this
    # setting: neither changes which titles the row holds or which ones leave it, so neither can be
    # used to make a row cycle faster — that is `refresh_days`, the refresh cadence.
    #
    # Per-row with a plain default rather than an inheritable global (like `media` and `rewatch`, not
    # like `refresh_days`): the right order is a property of what a row IS, so a server-wide default
    # would be a setting nobody sets.
    pick_order: str = "best"
    # Which surfaces the OWNER's own collection appears on: "both" (Home + Library Recommended, the
    # default), "home", "library", or "off" (neither — the Collections tab only, since promote()
    # always browse-hides). "off" is a STRING, not None: `placement_friends=None` already means
    # "inherit", so a None sentinel here would be two different things at once.
    placement: str = "both"
    # The same, for each FRIEND's (shared user's) own collection — "home" means Friends' Home there.
    # None = inherit from `placement` (backward compat); set explicitly to diverge.
    placement_friends: str | None = None
    # This row is hidden TODAY by its day schedule (issue #102), as opposed to being switched off
    # permanently. Both resolve `placement` to "off", so the placement alone cannot tell them apart —
    # and promotion needs to, because it stops guessing about unidentifiable collections only when a
    # schedule could be hiding one. Set by the server when it resolves the schedule; the engine never
    # reads a clock.
    hidden_by_schedule: bool = False
    # LEGACY, and the engine no longer reads it. It used to pin the row to the top of its library's
    # Recommended shelf with `ManagedHub.move(after=None)` on every promote — the one insert that can
    # collapse a library's hub order (see `place_rows`), and redundant besides, since a row with no
    # placement already sits at the top. Carried only so the row editor can migrate it into a
    # per-library "Top" the first time that row is saved.
    pin_top: bool = False
    # Where THIS row sits in the Recommended shelf, per library, keyed by section key -> HubAnchor.
    # A library ABSENT here means the top of the shelf, which is the shipped default — there is no
    # global default to inherit any more (`EngineConfig.hub_anchors` and the `rows.hub_anchor` setting
    # were retired: they were a second place to set the same thing and disagreed with their own
    # screen). Not "leave it alone" either — Plex appends new hubs at the bottom, so a new row nothing
    # positions starts out of sight; opting out is `HubAnchor.enabled`, set deliberately per row.
    hub_anchors: dict[str, HubAnchor] = field(default_factory=dict)
    # Optional custom poster for this row's Plex collection(s). None -> leave Plex's own artwork alone.
    poster: PosterSpec | None = None
    # The collection's Plex SUMMARY, with the same placeholders as the name (issue #120). "" -> Shortlist
    # leaves the summary alone, so a value another tool (agregarr, Kometa) put there survives.
    description: str = ""
    # Put before the row's name to make its Plex SORT TITLE (issue #120), e.g. "!010_". It orders the
    # row in the library's Collections tab only — Home and the Recommended shelf go by hub position,
    # which is `hub_anchors`. "" -> the sort title is left alone. Always prefix + the CURRENT name, so
    # a renamed or `{top_seed}` row goes on sorting under the prefix.
    sort_title_prefix: str = ""
    # The seasons this row follows, in calendar order (discussion #124). [] -> not a seasonal row.
    seasons: list[str] = field(default_factory=list)
    # The season it builds for on this run, resolved by the server from today's date (the engine reads no
    # clock). None on a seasonal row means it is between seasons: DORMANT — not gathered, not built, and
    # its collection kept hidden until its next season.
    season: RowSeason | None = None

    @property
    def dormant(self) -> bool:
        """A seasonal row with no season tonight: kept hidden and untouched until its next one."""
        return bool(self.seasons) and self.season is None

    @property
    def _effective_friends_placement(self) -> str:
        """Resolved friends placement: explicit override, or inherit from owner placement."""
        return self.placement_friends if self.placement_friends is not None else self.placement

    @property
    def show_home(self) -> bool:
        """The owner sees their OWN row on their Home screen (Plex `promotedToOwnHome`)."""
        return self.placement in ("both", "home")

    @property
    def show_friends_home(self) -> bool:
        """Each friend sees their OWN row on their Home screen (Plex `promotedToSharedHome`)."""
        return self._effective_friends_placement in ("both", "home")

    @property
    def show_owner_library(self) -> bool:
        """The OWNER's own collection sits on its library's Recommended shelf."""
        return self.placement in ("both", "library")

    @property
    def show_friends_library(self) -> bool:
        """Each FRIEND's own collection sits on its library's Recommended shelf.

        Separate from `show_owner_library` because every person gets their OWN collection, so Plex's
        single `promotedToRecommended` flag is set per collection — the owner/friends split is real,
        not cosmetic. Friends only ever see their own row on that shelf (their share filter excludes
        everyone else's), but the OWNER has no share filter to hang an exclude on, so turning this on
        also puts every friend's row on the owner's shelf. Plex limitation, surfaced in the UI.
        """
        return self._effective_friends_placement in ("both", "library")

    @property
    def show_library(self) -> bool:
        """Recommended-shelf flag for a SHARED row — one public collection rather than one per
        person, so there is nothing to split: it shows if either audience asked for it."""
        return self.show_owner_library or self.show_friends_library

    @property
    def label(self) -> str | None:
        """The privacy label for a shared row; per-person rows use the user's own label instead."""
        return f"{SHARED_LABEL_PREFIX}{self.slug}" if self.shared else None


@dataclass(frozen=True)
class SeerrTarget:
    """Which Overseerr/Jellyseerr instance the server talks to.

    No quality profile, root folder or tag: the *seerr applies its own rules, and each person files
    their own requests through it (`SeerrPersonClient`). Shortlist only says which title is wanted.
    """

    url: str
    api_key: str
    #: Legacy field, kept so older callers constructing a target positionally still work. Nothing
    #: reads it: requests are filed as the person themselves, never as a configured stand-in.
    request_as_user_id: int = 0


@dataclass(frozen=True)
class HubAnchor:
    """Where a library's Shortlist rows should sit in Plex's managed-recommendation shelf: the very
    TOP (``to_top=True``), or right after (``before=False``) / before (``before=True``) either a
    foreign collection (``anchor_title``) or another Shortlist ROW (``anchor_row``, a row slug).
    ``to_top`` ignores both; ``anchor_row`` wins over ``anchor_title`` when both are set.

    ``anchor_row`` is a slug and not a title on purpose. A per-person row is one Plex collection PER
    PERSON — forty accounts means forty collections whose titles differ only by the invisible
    per-account marker — so a title can only ever name ONE person's copy, which is meaningless as
    "put my row after Picked for You". The slug names the row itself, and each library resolves it to
    whichever of its collections are that row's (issue #81).

    Re-applied at the end of every run so a co-managing tool (e.g. Kometa, which can push our rows to
    the bottom of the shelf) can't leave them buried. Only OUR hubs are moved; a FOREIGN anchor is
    read-only. A row anchor is one of ours and therefore also moves — which is why the rows of a
    library are placed in dependency order, never in one block.
    """

    anchor_title: str = ""
    before: bool = False
    to_top: bool = False
    # LAST, and they must stay last: `HubAnchor(title, before, to_top)` is constructed positionally in
    # places, so a new field anywhere earlier silently re-binds their arguments — inserting one
    # second turned `HubAnchor("Gems Anchor", False)` into a row anchor of `False`.
    anchor_row: str = ""
    #: The owner's per-row switch. OFF means Shortlist never positions this row, so it sits wherever
    #: Plex put it: a newly created hub goes to the BOTTOM of the shelf and stays there.
    enabled: bool = True


# The seeded default row title. ``{library_name}`` renders each library's own name at delivery, so a
# multi-library server gets "✨ Movies Picked for You" / "✨ TV Shows Picked for You" — distinct titles,
# which per-person rows REQUIRE (they share one label and are told apart only by title). With no library
# (a preview or a row-level summary) it collapses to DEFAULT_ROW_NAME. Kept in lockstep with
# settings_store's ``row.name_template`` default and web's DEFAULT_ROW_TEMPLATE.
DEFAULT_ROW_TEMPLATE = "✨ {library_name} Picked for You"

# How large a row may be. THE definition — the server's three size validators (`row.size`, a row's
# own `size`, a per-user `row_size`) all import these rather than restating 5 and 40, and web mirrors
# them as ROW_SIZE_MIN/ROW_SIZE_MAX (pinned by tests/unit/test_web_constant_parity.py). Duplicating
# the maximum is how the pool cap below silently stopped clearing it.
MIN_ROW_SIZE = 5
MAX_ROW_SIZE = 40

# The slowest refresh cadence a row may be set to, in days. A VALIDATION bound (reject nonsense),
# not a behaviour cap: the engine handles any period. The old 0..1 freshness fraction was stretched
# onto 1..14 days, so a fortnight was the slowest expressible cadence and a monthly row could not be
# asked for at all — the ceiling is a year now because nothing about the mechanism objects.
MAX_REFRESH_DAYS = 365


@dataclass
class EngineConfig:
    """Static configuration for one engine run (adapters build this from settings)."""

    row_size: int = 15
    row_name_template: str = DEFAULT_ROW_TEMPLATE
    # How many candidates per media type survive the pre-rank cut and are offered to the picker.
    # DERIVED from the row ceiling, never restated: this was a flat 40 while `row.size` was validated
    # up to 40, so at the top of the range the pool and the row were the same size — every surviving
    # candidate had to go in, and a refresh night had nothing spare to swap the weakest third for.
    # Twice the ceiling because that is what a refresh actually needs: it keeps ~2/3 of the row and
    # must find the rest among candidates NOT already in it, with slack left for watched-filtering.
    # Only a sort and a slice over a list already in memory — the gather happened before this, and
    # MDBList ratings are fetched per PICK (rows.py), so a larger pool costs no extra API calls.
    candidates_pre_rank: int = MAX_ROW_SIZE * 2
    # How many of a person's most recent watched titles the web-search source searches per row (one
    # cached Exa search each). Row-overridable via RowSpec.recent_count.
    recent_count: int = 10
    # When True (default), a DISABLED (opted-out) Shortlist user has EVERY shared row hidden too — even
    # public "Popular on this server" rows — so disabling someone removes them from Shortlist entirely.
    hide_shared_from_disabled: bool = True
    min_history: int = 10  # below this -> cold-start row
    # What a cold-start user gets, server-wide: "popular" (a row of the server's top-rated titles) or
    # "skip" (no row built at all, and any row they already have is REMOVED — skipping has to mean
    # gone, or last month's row sits on their Home going stale for ever). Row-overridable via
    # RowSpec.cold_start. Defaults to "popular": the pre-existing behaviour, so an upgrade never
    # silently takes rows away.
    cold_start: str = "popular"
    min_completion: float = 0.7  # history completion threshold for "meaningful" watch
    # How many watched titles seed a row (the most recently watched win, balanced across media types).
    # Row-overridable via RowSpec.max_seeds.
    max_seeds: int = 30
    # Which service's score a row with `pick_order="rating"` sorts on: tmdb (free, already on every
    # candidate) or imdb/trakt/tomatoes/metacritic via MDBList.
    rating_source: str = "tmdb"
    # Titles that must never seed a SHARED row, server-wide.
    #
    # Per-person blocks deliberately do NOT apply here: a shared row is public, and letting one
    # person's "don't seed this" quietly reshape what everyone else sees would make an individual
    # preference into a server-wide edit nobody else can see or undo.
    blocked_shared_seeds: set[int] = field(default_factory=set)
    # A title someone rated at or below this in Plex (0..10, so 2 = one star, which is also where
    # thumbs-down lands) stops seeding THEIR rows. None = ignore Plex ratings entirely.
    #
    # Like `blocked_shared_seeds` above, and for the same reason, this never applies to a shared row:
    # one person rating a film badly must not remove it from a row everyone else can see. It is
    # applied at the per-person seed derivation only.
    dislike_threshold: float | None = 2.0
    # Cap on already-watched titles in a row, as a fraction of the row. 0.0 (default): all fresh —
    # drop every finished title (a movie you watched, or a show you've seen >= watched_show_pct of;
    # a partly-watched show or one with a new season stays eligible). 1.0: no filtering. Between:
    # at most that fraction of the row may be things already finished. Overridable per row.
    watched_pct: float = 0.0
    # A show watched to >= this fraction of its episodes counts as finished. 0.8, not 0.9: a returning
    # show a person is caught up on sits a few episodes short of 100% (the newest ep just aired, or one
    # was marked-not-played), so 0.9 kept re-recommending shows they've clearly finished — MooHouse's
    # "Deadliest Catch: The Viking Returns" at 8/9 = 89% slipped under the 0.9 bar (2026-07-21). The
    # season-worth floor in `_watched_titles` catches long shows; this catches near-complete short ones.
    watched_show_pct: float = 0.8
    # Refresh cadence in DAYS: 0 (the dataclass default) = frozen, never rebuilt once built; 1 =
    # every night; N = every N days. Overridable per row. `settings_store` defaults the PRODUCT to 8.
    #
    # Was a 0..1 "freshness" fraction stretched onto 1..14 days by a curve, so the stored number
    # described nothing (0.55 meant 7 days), the far end of the scale was a constant duplicated in
    # TypeScript, and no cadence slower than a fortnight was expressible. Migration 0065 converted
    # every stored value through that same curve, so no row's cadence moved.
    #
    # It sets HOW OFTEN a row rebuilds, never how much of it turns over: a refresh keeps the strongest
    # ~two-thirds and swaps the weakest third (`_KEEP_FRACTION`, rows.py), at every cadence including
    # nightly. The old name promised "rotate the whole row daily and reach deep down the ranked list",
    # a magnitude nothing implements — and folding turnover in here would tie more variety to worse
    # picks, since the only way to swap more of a row is to reach further down the ranked list.
    refresh_days: int = 0
    # The IDLE CEILING in days: how long a row may wait when the person it belongs to has watched
    # nothing since it was last built. 0 (the default, and every existing install) = off, so a row
    # always rebuilds on its `refresh_days` cadence whatever they have been doing.
    #
    # The pair is deliberately two numbers, not one: `refresh_days` is "is it this row's night?",
    # this is "is there anything new to say?". Only when BOTH allow it does a row re-pick. See
    # `rows._held_for_idle` for why the ceiling is a ceiling and not a freeze.
    idle_hold_days: int = 0
    # How much a title's release date counts when ranking it: 0.0 (default) = ignore age entirely,
    # which is how this ranked before the setting existed; 1.0 = every ~8 years of age halves a
    # title's weight. A WEIGHT, never a filter — an old title is only ever asked to be a better
    # match. Overridable per row (see RowSpec.recency for why it is not the refresh cadence).
    #
    # The DATACLASS defaults to 0.0 so a library caller opts in rather than inheriting an opinion.
    # The product does not: `settings_store` defaults `recommendations.recency` to 0.5 for every
    # install, existing servers included, and each row adopts it on its next refresh night.
    recency: float = 0.0
    # Which candidate sources to pool (see engine/candidates.py). Empty/default = TMDB similar only,
    # preserving legacy behaviour; owners widen recall by enabling more.
    candidate_sources: list[str] = field(default_factory=lambda: ["tmdb_similar"])
    # Which backend the llm_web source searches with — exactly one: 'native' (the provider's own
    # web-search tool, Claude/GPT/Gemini only), 'exa', or 'searxng'. Either external is the only path
    # for a local Ollama model. ('auto', which unioned native with an external, was removed in 1.3.)
    web_search_provider: str = "native"
    # Master switch for touching the Recommended-shelf ORDER. False -> Shortlist never reorders the
    # shelf (skips the whole order phase), so a co-managing tool (agregarr/Kometa) owns the order and
    # the two don't fight. True (default) -> apply the configured anchors. Independent of delivery and
    # promotion — turning it off still delivers and hides rows; it only stops the reordering.
    manage_shelf_order: bool = True
    # How much a person's measured genre avoidance counts when ranking, 0.0 (ignore it, the default
    # and every existing install) .. 1.0. A WEIGHT, never a filter: an avoided genre is only ever
    # asked to be a better match, and `ranking.negative_multiplier` floors the total so it can shade
    # the order without deciding it. Off by default in BOTH layers — unlike `recency`, which the
    # product deliberately turned on for existing servers; that was its own decision, not a
    # precedent.
    genre_avoidance: float = 0.0
    # How much "continues a story you already started" counts, 0.0 (off, the default) .. 1.0.
    # Movie-only — TMDB has no franchise concept for TV.
    franchise: float = 0.0
    # How much shared top-billed cast counts, 0.0 (off, the default) .. 1.0. Prolific actors are
    # discounted before this applies, so it means "shares someone NOTABLE", not "shares anyone".
    cast: float = 0.0
    # Wall-clock gap, in seconds, between the two independent "still unlabelled?" reads that
    # `delivery.sweep_broken_rows` demands before DELETING an orphan row — the one irreversible write
    # in the engine. A transient PMS miss (a mid library-index rebuild) clears within seconds; a
    # genuine orphan's label never arrives however long you wait, so the wait is real discriminating
    # power that a same-instant re-read does not have.
    #
    # The DATACLASS defaults to 0 (immediate, so tests stay fast and a library caller inherits no
    # opinion). `settings_store` defaults the PRODUCT to a real delay.
    orphan_confirm_delay_s: float = 0.0
    dry_run: bool = False
    # The curated rows to deliver. Empty -> a single default per-person row synthesized from
    # row_name_template/row_size, so existing callers behave exactly as before.
    rows: list[RowSpec] = field(default_factory=list)
    # Whether the caller MANAGES rows (the server does; direct/legacy engine callers may not). It is the difference
    # between "no rows configured" — synthesize the legacy default — and "every row is switched
    # OFF", which must deliver nothing. Without it, disabling every row in the UI silently rebuilt
    # "✨ Picked for You" for everyone: the Rows page said off, Plex said on.
    rows_defined: bool = False
    # Per-person rows DISABLED in the UI: no longer delivered, but their collections still sit on
    # their owners' Home (the label keeps them excluded from everyone else, so it's not a leak — just
    # "off" that isn't gone). Each is removed like a mute on the next run. Static-titled rows only; a
    # {top_seed} row can't be re-titled without picks, so it's left until the row is re-enabled.
    retired_rows: list[RowSpec] = field(default_factory=list)
    # Row slugs to actually (re)build this run — a per-row scheduled run only rebuilds its own rows.
    # None = build every row (a full run). Only the DELIVERY loop is scoped: privacy classification,
    # the leak-safe share-filter sync, the unhidable-row sweep, and shelf promotion all still see the
    # FULL `rows` set, so a row not built this run keeps its excludes, its placement, and its privacy.
    build_only: frozenset[str] | None = None
    # True when the caller handed us a SUBSET of the roster ("Run now" for one person) rather than
    # everyone. Shared rows are then not built at all: a "popular on this server" row assembled from
    # whoever happened to be selected is not a server-wide row, and it would be published to
    # everyone. It also stops the engine reporting "only 1 person is in this row's audience — it can
    # never build" about a perfectly healthy 10-person row. Default False = "this IS the roster",
    # the honest reading for a direct library caller.
    users_scoped: bool = False

    def should_build(self, spec: RowSpec) -> bool:
        """Whether this run rebuilds ``spec`` (scoped run) or every row (full run)."""
        return self.build_only is None or spec.slug in self.build_only

    def default_row_spec(self) -> RowSpec:
        """The single default per-person row, synthesized when no rows are configured.

        Its name_template is left empty so it falls through to the per-user override (or config
        default) at delivery — preserving the legacy per-user row-name behaviour.
        """
        return RowSpec(slug="picked", name_template="", size=self.row_size)

    def per_person_rows(self) -> list[RowSpec]:
        """Per-person specs to deliver; a single default row only when rows aren't managed at all."""
        if not self.rows:
            return [] if self.rows_defined else [self.default_row_spec()]
        return [row for row in self.rows if not row.shared]

    def shared_rows(self) -> list[RowSpec]:
        """Shared ('popular on this server') specs to deliver."""
        return [row for row in self.rows if row.shared]


@dataclass
class StageCounts:
    """Per-stage counts surfaced in run reports and SSE progress."""

    history: int = 0
    seeds: int = 0
    candidates: int = 0
    in_library: int = 0
    pre_ranked: int = 0
    picks: int = 0


@dataclass(frozen=True)
class WrittenDetails:
    """What Shortlist last wrote to one collection's summary and sort title, from the delivery ledger.

    None means Shortlist has no value there, which is the only thing that makes clearing a row's field
    safe: the revert touches a field only while Plex still holds exactly what Shortlist wrote, so a
    value somebody set by hand or with another tool is never wiped (issue #120).
    """

    summary: str | None = None
    title_sort: str | None = None


@dataclass
class CollectionDiff:
    """What delivery changed (or would change, in dry-run) on the user's collections."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # rows destroyed this run (swept, or rebuilt)
    collection_title: str = ""
    created: bool = False
    # The Plex ratingKey of the collection this landed in. The delivery LEDGER's whole point: it is
    # the only stable handle on "which object on the server is this row, for this person, in this
    # library". Titles are not — a `{top_seed}` row renders differently every run, so nothing computed
    # from config can find it later. 0 in a dry run and whenever the PMS didn't hand one back.
    rating_key: int = 0


@dataclass
class OwnedRow:
    """Every Shortlist collection belonging to one user, across libraries.

    A user gets at most one collection per library section (movies, shows), all carrying the
    same `shortlist_<slug>` label — which is what the share-filter excludes key off. The privacy
    check must know about ALL of them: a leak in any library is a leak.
    """

    label: str  # as stored by Plex, which title-cases labels
    rating_keys: list[int] = field(default_factory=list)
    # The TYPES of library the rows are in ("movie", "show"). A share filter is per type
    # (`filterMovies`, `filterTelevision`), so this is which filters have a row of theirs to hide.
    section_types: set[str] = field(default_factory=set)


@dataclass
class UserRunReport:
    """Outcome of the pipeline for a single user; users never affect each other."""

    username: str
    slug: str
    status: str = "pending"  # pending | ok | cold_start | skipped | error
    picks: list[Pick] = field(default_factory=list)
    counts: StageCounts = field(default_factory=StageCounts)
    diff: CollectionDiff | None = None
    # {"row_slug", "library_key"} for every collection this run DELETED in-run (a muted/retired row, or
    # one a cold start skips). The adapter forgets these ledger entries on persist, the same way the
    # on-demand reconciles call `_forget_deliveries`.
    #
    # Without it the ledger keeps a ratingKey whose collection is gone, and these paths RE-RUN: a cold
    # user is skipped again every night, re-presenting the same dead key for as long as they stay cold.
    # Plex reuses `metadata_items.id`, so that key can come to name a different collection under this
    # same label — and `promote_user_rows` reads the ledger too, so it is not only removals at stake.
    removed_deliveries: list[dict] = field(default_factory=list)
    # This person's MISSING titles — what the engine would suggest that no library holds, best first,
    # as plain dicts (`rows._missing_titles`). The per-person request surface; the adapter persists it
    # as their suggestions. Empty on the cold path and for engines that do not serve the surface.
    missing: list[dict] = field(default_factory=list)
    # Each delivered collection, as (section key, marked TITLE), mapped to the slug of the row that
    # produced it, so the promote phase applies the right row's placement/pin. Keyed by library as well
    # as title: a {top_seed} title differs library to library, and two of one person's rows may share a
    # title when they build in different libraries (issue #121). Transient (not persisted).
    placement_titles: dict[tuple[str, str], str] = field(default_factory=dict)
    # Per-(row, library) delivery result, so the UI can show "added X to Movies, Y to TV" instead of
    # one merged list. Each entry: row_slug/row_title, library_key/library_title, added/removed/kept/
    # deleted, created, and that library's own ranked picks. Persisted on RunUser.breakdown.
    breakdown: list[dict] = field(default_factory=list)
    privacy_synced: bool = False
    error: str | None = None
    # Why a NON-failing outcome happened — set alongside `skipped`, never for an error. "Skipped"
    # with no explanation sent a beta user hunting for a bug that wasn't there (issue #3): a shared
    # row with one enabled user can never reach its 2-watcher floor, and nothing on screen said so.
    # Distinct from `error` because the UI counts every non-null `error` as a failed user.
    reason: str | None = None
    duration_s: float = 0.0
    # Total AI tokens this user cost this run — the llm_web source (web-search title discovery) is
    # the only thing that spends them now.
    llm_tokens: int = 0
    # The output share of `llm_tokens`. Billed at several times the input rate, so the run page shows
    # the two apart rather than one total that hides where the money went.
    llm_output_tokens: int = 0
    # The same total split by WHERE it went: {"llm_web": N}. Lets the UI answer "what did the AI
    # actually spend tokens on" per person, not just a lump sum.
    llm_tokens_by_step: dict[str, int] = field(default_factory=dict)
    # Exa web searches run for this user (the llm_web external backend). Tracked apart from tokens:
    # Exa bills per search request, not per token, so the two must never be summed together.
    exa_searches: int = 0
    # Searches served from the shared 14-day cache instead of billed. Reported alongside exa_searches
    # so a fully-cached run reads "1 searched · N from cache", not a bare "1" that looks like nothing ran.
    exa_cache_hits: int = 0
    # A per-user, JSON-serializable record of the whole pipeline — seeds derived, each source's queries
    # and returns, the LLM/Exa prompts, and the ranked pool — so the UI can show "exactly what happened
    # for this person" without re-running anything. Purely diagnostic; the engine never reads it back.
    # {} when tracing produced nothing (a skipped/cold user). Persisted on RunUser.trace.
    trace: dict = field(default_factory=dict)
    # Every per-person row and what this run decided about it FOR THIS PERSON, as
    # ``{row_slug: "due" | "not_due" | "muted" | "not_in_audience" | "out_of_season"}``.
    #
    # `reason` says why somebody built nothing as one sentence for the whole person, which cannot be
    # attributed to a row — so a rows-first view had no way to put a skipped person under the rows
    # they were skipped for, and the largest group on a run page fell outside the tree entirely.
    # Recorded for EVERY user, not just skipped ones, so the tree is complete for a successful run too.
    #
    # "due" is intent, not outcome: it says this run meant to build the row, and the person's own
    # `status` says what became of it. Naming it "built" would claim a success that a later error in
    # the pipeline can still take away. {} on a cold-start skip, which never reaches the decision.
    rows_considered: dict[str, str] = field(default_factory=dict)
    # Seconds spent on work EVERY row shares — the watch-history fetch and the candidate gather.
    # All AI spend happens here (see `pool_costs`), so on a typical person this dwarfs the rows.
    # Reported as its own line rather than divided between rows, which would invent a split.
    setup_s: float = 0.0
    # Per-row cost keyed by row slug: {"duration_s": wall clock, "blocked_s": of which, waiting on
    # the shared Plex write lock}. duration_s INCLUDES blocked_s; work time is the difference.
    # At concurrency 1 blocked_s is always ~0; at 8 it is what explains a row that looks slow.
    row_timing: dict[str, dict[str, float]] = field(default_factory=dict)
    # One entry per candidate-pool COMPUTATION: {"label", "tokens", "exa_searches", "duration_s",
    # "rows": [slug, ...]}. Pools are memoised per `pool_key` and usually shared by every row, so
    # `rows` is what lets the UI say "one pool, used by both rows" instead of splitting the tokens.
    pool_costs: list[dict] = field(default_factory=list)
    # INTERNAL cursor, never persisted: which row `_timed_lock` charges write-lock waits to.
    # None means setup, whose wait is already inside `setup_s`.
    lock_bucket: str | None = None


@dataclass
class RunReport:
    """Aggregate outcome of one engine run."""

    started_at: datetime
    finished_at: datetime | None = None
    dry_run: bool = False
    users: list[UserRunReport] = field(default_factory=list)
    # Rows deleted because Plex could not hide them, keyed by the slug that owned them. Kept at
    # run level because the sweep covers the whole SERVER: a leaking row belonging to a paused or
    # disabled user is still a leaking row, and nobody would ever see it in a per-user report.
    swept_rows: dict[str, list[str]] = field(default_factory=dict)
    # Labels of rows the converge phase pulled off the OWNER's Home because this run's promote could
    # not reach them (their user is paused, disabled, deselected, errored — or the row was promoted
    # by an older build). Run level for the same reason as the sweep: these people are by definition
    # absent from the user list, so a per-user report would never show it (plex-safety rule 10).
    converged: list[str] = field(default_factory=list)
    # Labels of collections DELETED because Shortlist no longer knows the user they belong to.
    # Separate from `converged` because this is the one irreversible action converge takes, and
    # "what was destroyed at 03:31" must be answerable on its own (plex-safety rule 10).
    orphans_removed: list[str] = field(default_factory=list)
    # Share filters we changed, keyed by plex account id. Editing someone's Plex share permissions
    # is the most sensitive write Shortlist makes, and most of the accounts we write to are not in
    # any run's user list — so without this, "what changed on whose share at 03:31" would have no
    # answer for them at all (plex-safety rule 10).
    filter_writes: dict[int, dict] = field(default_factory=dict)
    # Managed-recommendation shelf outcomes for this run — a run-level audit of a server-wide Plex
    # write (plex-safety rule 10). TWO kinds of entry, and a consumer has to branch on them:
    #   * a MOVE — `moved` (the row titles) and `verified` (did the shelf actually end up that way);
    #   * a placement that could NOT be applied — `placed: False`, `moved: []`, `reason` in
    #     a refused anchor, and deliberately NO `verified`, because nothing was asked of Plex.
    # Reporting the second as the first is exactly what the Jobs detail line used to do: a buried row
    # announced as "repositioned". Empty when every library was already in place, when none holds a
    # row of ours, or when `manage_shelf_order` is off — NOT when no anchor is configured, which
    # falls back to moving every row to the top and so fills this normally.
    hub_orderings: list[dict] = field(default_factory=list)
    error: str | None = None  # a run-level failure (e.g. the sweep itself could not run)
    # Why promotion was blocked this run, one entry per account whose share filter could not be
    # written — the accounts a row would otherwise be visible to. Without these the operator sees
    # only "promotion skipped — a privacy sync failed", which names neither the account nor the
    # reason and sends them to the container logs (issue #1, mrjohnpoz).
    promotion_blockers: list[str] = field(default_factory=list)
    # {username: [ratingKey, ...]} — rows this account can SEE that are not its own, on an account
    # Plex will not accept a hide-list for. Not a blocker: nothing we do can hide these, so stopping
    # the run would punish everyone for one account. It is reported instead, because an exposure the
    # owner is not told about is the actual failure (see privacy.unhidden_rows_visible_to).
    unhideable_rows: dict[str, list[int]] = field(default_factory=dict)
    # {plex account id: why} — accounts the owner asked us to LEAVE ALONE whose excludes we could not
    # actually take back off. Not a blocker (failing to remove an exclude leaves them more private
    # than asked, never less), but it is a state change the owner made that did not reach Plex, and
    # §12's whole register is that shape. `pipeline._leave_sharing_alone` fills it.
    left_alone_failures: dict[int, str] = field(default_factory=dict)
    # {plex account id: username} — accounts whose filter held one of our excludes where Plex ORs it with a
    # restriction the OWNER set (`X|label!=shortlist_*`), which this run moved to where Plex applies it.
    # The pre-#116 merge wrote that shape onto every account with a restriction of its own, switching
    # the owner's restriction off; repairing it switches it back on, and the people on those accounts
    # will notice what they can see shrink. Reported so the owner hears it from Shortlist first.
    restrictions_restored: dict[int, str] = field(default_factory=dict)
    # {username: why} — accounts whose share filter Plex itself cannot read (a literal `&` inside one of the
    # owner's labels makes that account's Home answer HTTP 500 — measured 2026-09-13), so no exclude of
    # ours can be written into it and verified. Not a blocker, by owner decision: one label name must not
    # take every other person's rows off Home. Read beside `unhideable_measured`, which says the privacy
    # loop ran — an empty dict clears the alert only on a run that looked.
    unreadable_filters: dict[str, str] = field(default_factory=dict)
    # {username: [ratingKey, ...]} — accounts whose share filter Shortlist DID write, that can still
    # see other people's rows. The read-back proves plex.tv STORED our exclusions; this asks whether
    # Plex ACTS on them.
    #
    # Written by `pipeline._verify_filters_enforced`, persisted by `run_persistence` and read by both
    # the "Plex is ignoring the privacy filter" notification and `GET /api/privacy/status`. Read it
    # beside `filters_enforcement_measured`, never alone: empty means "nothing exposed" ONLY when
    # that flag says a check actually ran.
    filters_not_enforced: dict[str, list[int]] = field(default_factory=dict)
    # Whether the enforcement spot-check actually RAN. Without it an empty result is ambiguous — "we
    # looked and every account was clean" and "we never got that far" are the same empty dict — so the
    # alert could never clear itself: written only when non-empty, one bad night pinned an
    # undismissable red card through every clean run after it. Same shape as `unhideable_measured`.
    filters_enforcement_measured: bool = False
    # Whether this run actually GOT AS FAR AS looking. An empty `unhideable_rows` is ambiguous on its
    # own — "we checked and nobody is exposed" and "we died in the sweep phase" produce the same
    # dict — and the readers treat the latest measuring run as the truth. Without this flag a run
    # that failed early cleared a live exposure alert and every "Sees N rows of others'" badge while
    # the exposure was untouched, which is the exact silence the check exists to end.
    unhideable_measured: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and all(u.status != "error" for u in self.users)


@dataclass(frozen=True)
class FilterSnapshot:
    """A user's plex.tv share filters, captured before Shortlist's first mutation."""

    plex_account_id: int
    username: str
    taken_at: datetime
    filters: dict[str, str]  # filterAll/filterMovies/filterTelevision/filterMusic/filterPhotos
