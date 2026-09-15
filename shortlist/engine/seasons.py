"""Seasonal rows (discussion #124): the season catalogue and which season a row is in on a given day.

A seasonal row follows the calendar — Halloween films in October, Christmas films in December — and is
hidden between seasons. Everything here is pure date arithmetic; the server resolves it against its own
clock when it builds a row's spec, exactly as it resolves a day schedule (`rows.row_is_shown`), so the
engine never reads a clock.

A season's WINDOW runs from ``lead_days`` before its day to ``after_days`` after it. The row is SHOWN
inside a window, and BUILT inside it or on the night before it opens (`build_on`): that pre-build night
fills the row while it is still hidden, so the midnight flip that shows it puts a fresh row on screen
rather than last season's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from shortlist.engine.clients.tmdb import DISCOVER_MIN_VOTES
from shortlist.engine.models import MediaType, RowSeason


@dataclass(frozen=True)
class Season:
    """One entry in the catalogue: when it is, and which TMDB titles belong to it.

    ``keywords`` are TMDB keyword ids, OR'd together, for films and shows alike. ``movie_genres`` widens
    the FILM list with whole genres; TMDB's TV genre list has neither Horror nor Romance, so shows are
    keywords only.
    """

    slug: str
    name: str
    emoji: str
    month: int
    day: int
    description: str
    keywords: tuple[int, ...]
    movie_genres: tuple[int, ...] = ()
    #: Films a KEYWORD found are dropped when they carry one of these genres and none of ``movie_genres``.
    #: TMDB tags any film with a Halloween scene, so a rom-com or a drama set on the night reads as noise
    #: in a spooky row.
    keyword_excluded_genres: tuple[int, ...] = ()


# Keyword ids measured on TMDB 2026-09-15 (`/search/keyword`, then `/discover` against a real library).
# A wrong id is silent: it matches real, unrelated titles. The related keywords were chosen from what the
# library's untagged seasonal films actually carried ("christmas romance", "christmas spirit"...).
#
# The genres are there because keywords alone tag 63 Halloween and 20 Valentine's films on a 10k-film
# library, too few for rows that differ person to person; with Horror / Romance it is 670 and ~960.
SEASONS: dict[str, Season] = {
    season.slug: season
    for season in (
        Season(
            slug="valentines",
            name="Valentine's Day",
            emoji="💘",
            month=2,
            day=14,
            description="Valentine's films and romance",
            keywords=(160404, 376604),  # valentine's day, happy valentine's day
            movie_genres=(10749,),  # Romance
        ),
        Season(
            slug="halloween",
            name="Halloween",
            emoji="🎃",
            month=10,
            day=31,
            description="Halloween films and horror",
            # halloween, trick or treating, halloween night, halloween party, halloween costume
            keywords=(3335, 180193, 232795, 9694, 182794),
            movie_genres=(27,),  # Horror
            # Romance, Drama. Measured on a real library: of 23 non-horror Halloween keyword films, these
            # two genres cut exactly the misses — When We First Met, War Pony, Ed Wood, Brick, In America
            # (and, arguably, Donnie Darko) — and keep Hocus Pocus, Casper, Beetlejuice and E.T.
            keyword_excluded_genres=(10749, 18),
        ),
        Season(
            slug="christmas",
            name="Christmas",
            emoji="🎄",
            month=12,
            day=25,
            description="Christmas films",
            # christmas, christmas romance, christmas spirit, christmas special, christmas music, christmas
            # tree, christmas story, santa claus, christmas eve
            keywords=(207317, 272698, 193048, 255088, 186933, 5570, 196450, 1991, 260365),
        ),
    )
}

#: How early a seasonal row may start showing, and how long it may stay up afterwards, in days.
MAX_LEAD_DAYS = 90
MAX_AFTER_DAYS = 30


@dataclass(frozen=True)
class SeasonWindow:
    """One year's run of a season: its day (``anchor``) and the first and last days the row shows it."""

    season: Season
    anchor: date
    starts: date
    ends: date


def normalise_slugs(slugs: list[str]) -> list[str]:
    """De-duplicated, in calendar order. Raises ValueError naming anything the catalogue does not have."""
    unknown = sorted({slug for slug in slugs if slug not in SEASONS})
    if unknown:
        raise ValueError(f"unknown season(s) {unknown} — choose from {list(SEASONS)}")
    return [slug for slug in SEASONS if slug in set(slugs)]


def _windows(slugs: list[str], lead_days: int, after_days: int, around: date) -> list[SeasonWindow]:
    """Every window of these seasons anchored in the year before, of, and after ``around``.

    Three years because a window can cross New Year either way: Valentine's with a long lead opens in
    December, and Christmas with days after closes in January. Unknown slugs are skipped rather than
    raised: a season retired from the catalogue must leave an old row hidden, not crash every run.
    """
    windows = []
    for slug in slugs:
        season = SEASONS.get(slug)
        if season is None:
            continue
        for year in (around.year - 1, around.year, around.year + 1):
            anchor = date(year, season.month, season.day)
            windows.append(
                SeasonWindow(
                    season=season,
                    anchor=anchor,
                    starts=anchor - timedelta(days=lead_days),
                    ends=anchor + timedelta(days=after_days),
                )
            )
    return windows


def shown_on(slugs: list[str], lead_days: int, after_days: int, day: date) -> SeasonWindow | None:
    """The season a row with these settings shows on ``day``, or None between seasons.

    When windows overlap (a long lead meeting days after), a season whose day is still to come — or is
    today — beats one already past, and among those the nearest wins: Halloween lingering into November
    gives way to the Christmas that has started early, but holds its own day.
    """
    open_windows = [w for w in _windows(slugs, lead_days, after_days, day) if w.starts <= day <= w.ends]
    if not open_windows:
        return None
    return min(open_windows, key=lambda w: (w.anchor < day, abs((w.anchor - day).days)))


def build_on(slugs: list[str], lead_days: int, after_days: int, day: date) -> SeasonWindow | None:
    """The season a row builds for on ``day``: the one it shows, else the one it shows tomorrow.

    Never tomorrow's when today already shows one — at a hand-over (Halloween today, Christmas tomorrow)
    the row keeps Halloween on its own day and the next run switches it.
    """
    return shown_on(slugs, lead_days, after_days, day) or shown_on(
        slugs, lead_days, after_days, day + timedelta(days=1)
    )


def last_shown_day(slugs: list[str], lead_days: int, after_days: int, day: date) -> date:
    """The last day the season shown on ``day`` stays on screen — its window's end, or the day before a
    following season takes the row over. ``day`` itself when nothing is shown."""
    showing = shown_on(slugs, lead_days, after_days, day)
    if showing is None:
        return day
    last = day
    while last < showing.ends:
        tomorrow = shown_on(slugs, lead_days, after_days, last + timedelta(days=1))
        if tomorrow is None or tomorrow.anchor != showing.anchor or tomorrow.season != showing.season:
            break
        last += timedelta(days=1)
    return last


def row_season_on(slugs: list[str], lead_days: int, after_days: int, day: date) -> RowSeason | None:
    """What a seasonal row's spec carries on ``day``: the season it builds for, or None when dormant."""
    window = build_on(slugs, lead_days, after_days, day)
    if window is None:
        return None
    return RowSeason(slug=window.season.slug, name=window.season.name, emoji=window.season.emoji, anchor=window.anchor)


def next_after(slugs: list[str], lead_days: int, after_days: int, day: date) -> SeasonWindow | None:
    """The next window to OPEN after ``day`` — what an out-of-season row is waiting for."""
    upcoming = [w for w in _windows(slugs, lead_days, after_days, day) if w.starts > day]
    return min(upcoming, key=lambda w: w.starts, default=None)


class _ListReader(Protocol):
    def discover_all(self, media_type: MediaType, params: dict) -> list[dict]: ...


@dataclass(frozen=True)
class SeasonTitles:
    """A season's titles for one run.

    ``ids`` is every title TMDB puts in the season, whether or not the server has it — what a row's pool
    is filtered to, so a missing Christmas film similar to someone's watches can still be requested.
    ``in_library`` is the subset the server's libraries hold, as TMDB list items — what the season
    source adds to a pool. Adding anything else would turn the whole list into request demand.
    """

    ids: dict[MediaType, frozenset[int]]
    in_library: dict[MediaType, list[dict]]

    def contains(self, tmdb_id: int, media_type: MediaType) -> bool:
        """Whether a title is in this season. By media type: TMDB ids are unique only within one."""
        return tmdb_id in self.ids.get(media_type, frozenset())


def load_titles(tmdb: _ListReader, season: Season, library_index: dict[MediaType, dict[int, int]]) -> SeasonTitles:
    """Read a season's titles from TMDB and split out the ones the libraries hold.

    Films: the season's keywords OR'd together, plus its genre at ``DISCOVER_MIN_VOTES``. Shows: the
    keywords alone. The keyword query takes no vote floor — a made-for-TV Christmas film is exactly the
    season and often has a handful of votes. Raises when TMDB fails, so the rows that need this season
    keep what they have tonight rather than rebuild from half a list.
    """
    keywords = "|".join(str(keyword) for keyword in season.keywords)
    queries: list[tuple[MediaType, dict]] = [
        (MediaType.MOVIE, {"with_keywords": keywords}),
        (MediaType.SHOW, {"with_keywords": keywords}),
    ]
    if season.movie_genres:
        genres = "|".join(str(genre) for genre in season.movie_genres)
        queries.append((MediaType.MOVIE, {"with_genres": genres, "vote_count.gte": DISCOVER_MIN_VOTES}))
    found: dict[MediaType, dict[int, dict]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    excluded = set(season.keyword_excluded_genres)
    for media_type, params in queries:
        for item in tmdb.discover_all(media_type, params):
            genres = set(item.get("genre_ids") or [])
            # A horror drama is still horror: only a film with none of the season's own genres is dropped.
            if (
                "with_keywords" in params
                and media_type is MediaType.MOVIE
                and excluded & genres
                and not genres & set(season.movie_genres)
            ):
                continue
            found[media_type].setdefault(int(item["id"]), item)
    return SeasonTitles(
        ids={media_type: frozenset(items) for media_type, items in found.items()},
        in_library={
            media_type: [item for tmdb_id, item in items.items() if tmdb_id in library_index.get(media_type, {})]
            for media_type, items in found.items()
        },
    )
