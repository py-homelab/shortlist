"""Which season a seasonal row is in, on which day (discussion #124).

All of it is date arithmetic on pure functions, so every edge is pinned here: a window that crosses New
Year in either direction, two windows claiming one day, the pre-build night before a season starts, and
the gaps between seasons where the row is dormant.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from shortlist.engine import seasons
from shortlist.engine.models import MediaType
from shortlist.engine.rows import row_shown_today

ALL = ["halloween", "christmas", "valentines"]


class TestCatalogue:
    def test_the_three_seasons_are_on_their_fixed_dates(self):
        assert (seasons.SEASONS["halloween"].month, seasons.SEASONS["halloween"].day) == (10, 31)
        assert (seasons.SEASONS["christmas"].month, seasons.SEASONS["christmas"].day) == (12, 25)
        assert (seasons.SEASONS["valentines"].month, seasons.SEASONS["valentines"].day) == (2, 14)

    def test_the_catalogue_is_in_calendar_order(self):
        """The editor lists seasons in this order, so a year reads top to bottom."""
        assert list(seasons.SEASONS) == ["valentines", "halloween", "christmas"]

    def test_the_keyword_ids_are_the_measured_ones(self):
        """Measured on TMDB 2026-09-15 (`/search/keyword`): a wrong id matches real, unrelated films."""
        assert seasons.SEASONS["halloween"].keywords[0] == 3335
        assert seasons.SEASONS["christmas"].keywords[0] == 207317
        assert seasons.SEASONS["valentines"].keywords[0] == 160404

    def test_halloween_and_valentines_widen_with_a_film_genre_and_christmas_does_not(self):
        """Keywords alone tag 63 Halloween and 20 Valentine's films on a real 10k-film library — too few for
        rows that differ person to person. Christmas tags 340 and has no genre to widen into."""
        assert seasons.SEASONS["halloween"].movie_genres == (27,)
        assert seasons.SEASONS["valentines"].movie_genres == (10749,)
        assert seasons.SEASONS["christmas"].movie_genres == ()


class TestNormaliseSlugs:
    def test_unknown_seasons_are_refused(self):
        with pytest.raises(ValueError, match="easter"):
            seasons.normalise_slugs(["christmas", "easter"])

    def test_duplicates_collapse_and_the_order_follows_the_calendar(self):
        assert seasons.normalise_slugs(["christmas", "valentines", "christmas"]) == ["valentines", "christmas"]

    def test_an_empty_list_stays_empty(self):
        assert seasons.normalise_slugs([]) == []


class TestShownOn:
    def test_a_season_shows_from_its_lead_through_its_day(self):
        assert seasons.shown_on(["halloween"], 30, 0, date(2026, 9, 30)) is None
        assert seasons.shown_on(["halloween"], 30, 0, date(2026, 10, 1)).season.slug == "halloween"
        assert seasons.shown_on(["halloween"], 30, 0, date(2026, 10, 31)).season.slug == "halloween"
        assert seasons.shown_on(["halloween"], 30, 0, date(2026, 11, 1)) is None

    def test_days_after_keep_it_up_past_its_day(self):
        assert seasons.shown_on(["christmas"], 0, 1, date(2026, 12, 26)).season.slug == "christmas"
        assert seasons.shown_on(["christmas"], 0, 1, date(2026, 12, 27)) is None

    def test_a_zero_lead_shows_it_on_its_day_alone(self):
        assert seasons.shown_on(["valentines"], 0, 0, date(2027, 2, 13)) is None
        assert seasons.shown_on(["valentines"], 0, 0, date(2027, 2, 14)).season.slug == "valentines"

    def test_the_window_carries_its_dates(self):
        window = seasons.shown_on(["christmas"], 30, 2, date(2026, 12, 1))
        assert (window.anchor, window.starts, window.ends) == (
            date(2026, 12, 25),
            date(2026, 11, 25),
            date(2026, 12, 27),
        )

    def test_a_lead_that_starts_in_the_previous_year_is_found(self):
        """Valentine's with a 60-day lead starts 16 Dec — the anchor is NEXT year's 14 Feb."""
        window = seasons.shown_on(["valentines"], 60, 0, date(2026, 12, 20))
        assert window.anchor == date(2027, 2, 14)

    def test_days_after_that_run_into_the_next_year_are_found(self):
        window = seasons.shown_on(["christmas"], 0, 10, date(2027, 1, 3))
        assert window.anchor == date(2026, 12, 25)

    def test_a_season_not_chosen_never_shows(self):
        assert seasons.shown_on(["valentines"], 30, 0, date(2026, 12, 20)) is None

    def test_no_seasons_means_nothing_shows(self):
        assert seasons.shown_on([], 30, 0, date(2026, 12, 20)) is None

    def test_when_two_windows_claim_a_day_the_coming_season_wins(self):
        """Halloween lingering into November meets Christmas starting early: Halloween is over, so the
        row moves on to what is coming."""
        window = seasons.shown_on(["halloween", "christmas"], 60, 5, date(2026, 11, 2))
        assert window.season.slug == "christmas"

    def test_when_two_coming_seasons_claim_a_day_the_nearer_one_wins(self):
        window = seasons.shown_on(["halloween", "christmas"], 90, 0, date(2026, 10, 20))
        assert window.season.slug == "halloween"

    def test_a_season_on_its_own_day_beats_one_still_to_come(self):
        """Its own day is the nearest a season can be: Halloween holds 31 Oct against an early Christmas."""
        window = seasons.shown_on(["halloween", "christmas"], 55, 0, date(2026, 10, 31))
        assert window.season.slug == "halloween"

    def test_unknown_slugs_are_ignored_rather_than_raising(self):
        """A retired season in an old database must not crash every run — it simply never shows."""
        assert seasons.shown_on(["easter", "halloween"], 30, 0, date(2026, 10, 5)).season.slug == "halloween"


class TestBuildOn:
    def test_the_night_before_a_season_starts_builds_it(self):
        """The pre-build night: the run before the window builds the row (hidden), so the midnight flip
        shows a fresh row rather than last season's."""
        assert seasons.shown_on(["halloween"], 30, 0, date(2026, 9, 30)) is None
        assert seasons.build_on(["halloween"], 30, 0, date(2026, 9, 30)).season.slug == "halloween"

    def test_two_nights_before_is_dormant(self):
        assert seasons.build_on(["halloween"], 30, 0, date(2026, 9, 29)) is None

    def test_in_season_it_builds_the_season_it_shows(self):
        assert seasons.build_on(["halloween"], 30, 0, date(2026, 10, 15)).season.slug == "halloween"

    def test_a_hand_over_builds_todays_season_not_tomorrows(self):
        """Halloween is on screen on its own day; switching the row to Christmas at 03:30 on 31 Oct would
        put Christmas picks on Halloween."""
        window = seasons.build_on(["halloween", "christmas"], 55, 0, date(2026, 10, 31))
        assert window.season.slug == "halloween"

    def test_the_day_after_a_season_ends_is_dormant(self):
        assert seasons.build_on(["halloween"], 30, 0, date(2026, 11, 1)) is None


class TestNextAfter:
    def test_it_names_the_next_window_to_start(self):
        window = seasons.next_after(ALL, 30, 0, date(2026, 11, 10))
        assert (window.season.slug, window.starts) == ("christmas", date(2026, 11, 25))

    def test_it_wraps_into_next_year(self):
        window = seasons.next_after(ALL, 30, 0, date(2026, 12, 26))
        assert (window.season.slug, window.starts) == ("valentines", date(2027, 1, 15))

    def test_a_window_already_open_is_not_next(self):
        window = seasons.next_after(["halloween"], 30, 0, date(2026, 10, 10))
        assert window.starts == date(2027, 10, 1)

    def test_no_seasons_has_no_next(self):
        assert seasons.next_after([], 30, 0, date(2026, 10, 10)) is None


CHRISTMAS_KEYWORDS = "207317|272698|193048|255088|186933|5570|196450|1991|260365"


class _Tmdb:
    """Answers `discover_all` from a table keyed by (media type, the param that defines the query)."""

    def __init__(self, answers: dict[tuple[MediaType, str], list[dict]]):
        self.answers = answers
        self.queries: list[tuple[MediaType, dict]] = []

    def discover_all(self, media_type: MediaType, params: dict) -> list[dict]:
        self.queries.append((media_type, params))
        key = params.get("with_keywords") or params.get("with_genres")
        return self.answers.get((media_type, key), [])


class TestLoadTitles:
    HALLOWEEN_KEYWORDS = "3335|180193|232795|9694|182794"

    def test_films_are_the_keywords_and_the_genre_and_shows_are_the_keywords_alone(self):
        tmdb = _Tmdb(
            {
                (MediaType.MOVIE, self.HALLOWEEN_KEYWORDS): [{"id": 1}, {"id": 2}],
                (MediaType.MOVIE, "27"): [{"id": 2}, {"id": 3}],
                (MediaType.SHOW, self.HALLOWEEN_KEYWORDS): [{"id": 1}],
            }
        )
        titles = seasons.load_titles(tmdb, seasons.SEASONS["halloween"], {MediaType.MOVIE: {}, MediaType.SHOW: {}})
        assert titles.ids[MediaType.MOVIE] == frozenset({1, 2, 3})
        assert titles.ids[MediaType.SHOW] == frozenset({1})
        # Movie 1 and show 1 are different titles: TMDB ids are unique only within a media type.
        assert (MediaType.SHOW, {"with_genres": "27", "vote_count.gte": 200}) not in tmdb.queries

    def test_the_genre_query_keeps_to_well_voted_titles_and_the_keyword_query_does_not(self):
        """Christmas TV films are exactly the season and often have a handful of votes; a whole genre has
        tens of thousands of titles, so it takes the same floor the discover source uses."""
        tmdb = _Tmdb({})
        seasons.load_titles(tmdb, seasons.SEASONS["halloween"], {MediaType.MOVIE: {}, MediaType.SHOW: {}})
        assert (MediaType.MOVIE, {"with_genres": "27", "vote_count.gte": 200}) in tmdb.queries
        assert (MediaType.MOVIE, {"with_keywords": self.HALLOWEEN_KEYWORDS}) in tmdb.queries

    def test_a_season_with_no_genre_makes_no_genre_query(self):
        tmdb = _Tmdb({})
        seasons.load_titles(tmdb, seasons.SEASONS["christmas"], {MediaType.MOVIE: {}, MediaType.SHOW: {}})
        assert [params for _media, params in tmdb.queries if "with_genres" in params] == []

    def test_only_titles_the_library_holds_are_kept_as_candidates(self):
        """The season source adds these to a person's pool. Anything else would become request demand for
        thousands of films nobody asked for."""
        tmdb = _Tmdb(
            {(MediaType.MOVIE, "207317|272698|193048|255088|186933|5570|196450|1991|260365"): [{"id": 1}, {"id": 2}]}
        )
        library = {MediaType.MOVIE: {2: 9002}, MediaType.SHOW: {1: 9101}}
        titles = seasons.load_titles(tmdb, seasons.SEASONS["christmas"], library)
        assert [item["id"] for item in titles.in_library[MediaType.MOVIE]] == [2]
        assert titles.in_library[MediaType.SHOW] == []
        assert titles.ids[MediaType.MOVIE] == frozenset({1, 2})

    def test_a_title_both_queries_return_is_one_candidate(self):
        tmdb = _Tmdb({(MediaType.MOVIE, self.HALLOWEEN_KEYWORDS): [{"id": 2}], (MediaType.MOVIE, "27"): [{"id": 2}]})
        titles = seasons.load_titles(tmdb, seasons.SEASONS["halloween"], {MediaType.MOVIE: {2: 1}, MediaType.SHOW: {}})
        assert [item["id"] for item in titles.in_library[MediaType.MOVIE]] == [2]

    def test_a_halloween_romance_or_drama_is_not_a_halloween_film(self):
        """TMDB tags any film with a Halloween scene. On a real library that let "When We First Met"
        (a rom-com) and "War Pony" (a drama) into Halloween rows; the family and comedy Halloween films
        — Hocus Pocus, Casper — are what the keywords are there to add."""
        tmdb = _Tmdb(
            {
                (MediaType.MOVIE, self.HALLOWEEN_KEYWORDS): [
                    {"id": 1, "genre_ids": [35, 10749, 14]},  # comedy, romance, fantasy
                    {"id": 2, "genre_ids": [18]},  # drama
                    {"id": 3, "genre_ids": [14, 35, 10751]},  # fantasy, comedy, family
                    {"id": 4, "genre_ids": [27, 18]},  # a horror drama, through the keyword
                ],
                (MediaType.MOVIE, "27"): [{"id": 5, "genre_ids": [27, 18]}],
            }
        )
        titles = seasons.load_titles(tmdb, seasons.SEASONS["halloween"], {MediaType.MOVIE: {}, MediaType.SHOW: {}})
        assert titles.ids[MediaType.MOVIE] == frozenset({3, 4, 5})

    def test_christmas_keeps_its_romances(self):
        """Love Actually is a Christmas film. The exclusion is Halloween's, not every season's."""
        tmdb = _Tmdb({(MediaType.MOVIE, CHRISTMAS_KEYWORDS): [{"id": 1, "genre_ids": [35, 10749, 18]}]})
        titles = seasons.load_titles(tmdb, seasons.SEASONS["christmas"], {MediaType.MOVIE: {}, MediaType.SHOW: {}})
        assert titles.ids[MediaType.MOVIE] == frozenset({1})

    def test_contains_asks_by_media_type(self):
        tmdb = _Tmdb({(MediaType.MOVIE, self.HALLOWEEN_KEYWORDS): [{"id": 5}]})
        titles = seasons.load_titles(tmdb, seasons.SEASONS["halloween"], {MediaType.MOVIE: {}, MediaType.SHOW: {}})
        assert titles.contains(5, MediaType.MOVIE) is True
        assert titles.contains(5, MediaType.SHOW) is False


class TestRowShownToday:
    """The one place a row's weekdays and its seasons combine into "is it on Plex today"."""

    def test_a_row_without_seasons_follows_its_weekdays_alone(self):
        monday = datetime(2026, 8, 31, 12, 0)
        assert row_shown_today([1], [], 30, 0, monday) is True
        assert row_shown_today([2], [], 30, 0, monday) is False

    def test_a_seasonal_row_is_hidden_out_of_season_on_every_day(self):
        assert row_shown_today([], ["halloween"], 30, 0, datetime(2026, 9, 15, 12, 0)) is False

    def test_a_seasonal_row_is_shown_in_season(self):
        assert row_shown_today([], ["halloween"], 30, 0, datetime(2026, 10, 15, 12, 0)) is True

    def test_weekdays_narrow_a_season(self):
        # 2026-10-15 is a Thursday (ISO 4).
        assert row_shown_today([1], ["halloween"], 30, 0, datetime(2026, 10, 15, 12, 0)) is False
        assert row_shown_today([4], ["halloween"], 30, 0, datetime(2026, 10, 15, 12, 0)) is True
