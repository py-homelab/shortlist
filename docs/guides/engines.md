---
title: Bring your own recommendation engine
description: Plug a recommendation engine you run yourself into Shortlist, and the small HTTP protocol it has to speak.
heading: Bring your own engine
nav_order: 4
---

Shortlist's own engine finds titles by asking TMDB (and optionally Trakt or the web) for things
similar to what each person recently watched, then ranks them with a handful of dials. That is a
good default and needs no setup. It is not the only way to do it — a collaborative-filtering model,
an embedding index, a model trained on your own household's history — and if you have one, Shortlist
can use it in place of its own, for every row, without giving up anything it does around the
ranking: reading each person's history, keeping watched titles out, honouring their Plex restrictions,
building per-library collections, and hiding every row from everyone it isn't for.

## Switching engines

**Settings → Finding titles → Engine.** Choose **An engine of your own (HTTP)**, enter its address,
and press **Test** — it answers with the engine's name and whether it is ready. Choose what should
happen when the engine is down or has nothing for someone: fall back to Shortlist's own engine for
that person tonight (the default — the row still builds, and the run trace says which engine ranked
it), or leave their rows as they are.

The title sources and AI web search are Shortlist's own engine's; they disappear from the settings
page while an external engine is chosen, and come back when you switch back. A row can still name
its own sources in its editor — that row is then built by Shortlist's own engine from them (see
[How row settings work with an engine](#how-row-settings-work-with-an-engine)). Switching back is the
whole rollback: nothing else changes.

What an engine can see: for each person, their **seeds** (the recent watches Shortlist would search
from) and their **watched titles** — what, when, how many times, and their Plex rating if they gave
one — plus the titles their libraries hold. That leaves Shortlist for the one address you configured
and nowhere else. If the engine is on a different machine, put it behind TLS or a private network,
and set a token.

## The protocol

Two JSON endpoints. Everything else about the engine — language, storage, how often it rebuilds —
is its own business.

### `GET /v1/info`

```json
{"name": "recommendarr", "version": "0.4.0", "surfaces": ["library", "missing"],
 "features": ["season", "seed_focus"], "serves_cold": true, "ready": true}
```

`features` lists which of the request's optional asks the engine acts on — `season` and
`seed_focus`, described below. The Test button names any it lacks: those rows still build, from the
engine's overall order, but only approximate what their settings ask for.

`name` is what the run trace and the row provenance call it. `serves_cold` says whether the engine
wants to be asked about people with fewer watches than **Enough watch history** — Shortlist's own
cannot seed a search from a thin history, so it cold-starts those people up front; an engine that
answers `true` is asked like anyone else, and only if it returns nothing do they get the cold-start
row. A row set to skip a cold start (`recommendations.cold_start`, or the row's own setting) is skipped
for them whatever `serves_cold` says; their request page is still asked for. `ready: false` means it
has no build to serve from yet; the Test button says so.

Optional health fields let Shortlist tell you when an engine is up but serving old lists — the one
failure nothing else notices, because a stale engine still answers:

| Field | Meaning |
|---|---|
| `stale` | `true` when the engine's lists are too old to pass off as current. |
| `age_hours` | Hours since the lists being served were built. |
| `last_build_ok` | `false` when the most recent rebuild failed (older lists are still served). |
| `last_build_error` | One line on why, when it failed. |

The Test button fails on a stale engine and names the age and the build error; a failed build with
lists still fresh is a pass with a warning.

### `POST /v1/recommend`

Once per person per distinct candidate pool (rows that share sources, media and libraries share one
call). The request:

```json
{
  "protocol": 1,
  "plex_account_id": 12345678,
  "surface": "library",
  "media": ["movie", "show"],
  "limit_per_media": 80,
  "run_day": "2026-09-18",
  "seeds": [{"tmdb_id": 1396, "media_type": "show", "title": "Breaking Bad", "weight": 0.93}],
  "history": [
    {"tmdb_id": 1396, "media_type": "show", "title": "Breaking Bad", "year": 2008,
     "watched_at": "2026-09-10T21:14:00+00:00", "watch_count": 62,
     "viewed_leaf_count": 62, "leaf_count": 62, "user_rating": null}
  ],
  "library": {"movie": [603, 604, 605], "show": [1396, 1399]},
  "exclude": [[1396, "show"]],
  "excluded_genres": ["Horror"],
  "seed_focus": false,
  "season": null
}
```

- `library` is the candidate universe: the TMDB ids the row may recommend from, per media type,
  already narrowed to the row's own libraries when it is pinned to some. An engine that knows the
  library already may ignore it; anything it returns outside it is dropped anyway.
- `exclude` is what the row's rules keep out — watched titles, started series, whatever the row's
  already-watched setting says. `excluded_genres` are the person's own. Both are applied again to
  the answer, so an engine that ignores them loses nothing but effort.
- `limit_per_media` is how many titles per media type the row build will look at; more are cut,
  never re-ranked.
- `surface` is `library` for rows. `missing` (titles the libraries do **not** hold) is reserved.
- `season` is a seasonal row's title set (`{"movie": [...], "show": [...]}`, null otherwise). An
  engine with the `season` feature narrows its candidates to it **before** its `limit_per_media` cut;
  Shortlist filters the answer to the season's titles either way, but without the feature a seasonal
  row only gets the season titles that happened to make the engine's head.
- `seed_focus` is true when the row narrows its seeds on purpose — fewer watches than the server's
  default, or a window it cycles through (a *Because you watched …* row). An engine with the
  `seed_focus` feature then ranks by closeness to `seeds` rather than the person's whole taste, and
  names the matching seed in `seed`.

The answer, best first:

```json
{
  "engine": {"name": "recommendarr", "version": "0.2.0"},
  "ordered": true,
  "items": [
    {"tmdb_id": 1399, "media_type": "show", "title": "Game of Thrones", "year": 2011,
     "genres": ["Drama", "Sci-Fi & Fantasy"], "rating": 8.4, "vote_count": 24000,
     "poster_path": "/…jpg", "overview": "…", "language": "en",
     "reason": "Because you watched Breaking Bad", "kids": false,
     "seed": {"tmdb_id": 1396, "title": "Breaking Bad", "media_type": "show"}}
  ],
  "trace": {"built_at": 1789000000}
}
```

- The order of `items` **is** the ranking. Shortlist keeps it: its own scoring, and the spread across
  seeds it applies to its own engine's pool, both step aside for an external order.
- `reason` becomes the pick's "why you're seeing this" line as written. Without one, Shortlist says
  the title was chosen by your engine.
- `seed` names the watch the title follows from; it fills `{top_seed}` row names and the seed shown
  beside the pick.
- `kids` is what a row's **Children's & family titles** setting reads.
- `trace` is free-form and shown on the person's run page next to Shortlist's own trace.
- `household` (optional, top level): `{"label", "kids_titles", "window_titles", "window_days"}` —
  how many of the titles this person watched over the engine's recent window were children's titles.
  Shortlist turns the counts into adult / family / kids with its own thresholds (see
  [Children's and family titles](rows.md#childrens-and-family-titles)); `label` is the engine's own
  suggestion, shown beside Shortlist's.

Only `tmdb_id` and `media_type` (`movie` | `show`) are required per item; a malformed item is
skipped, not fatal. Send a token as `Authorization: Bearer …` if you set one. Shortlist retries a
timeout or a 5xx a few times, then treats the pool as failed and applies the fallback setting.

## How row settings work with an engine

Everything a row does around the ranking is Shortlist's and works the same with any engine. What the
ranking itself is asked to do is sent to the engine, or applied to its answer:

| Row setting | With an engine of your own |
|---|---|
| Size, films/shows, libraries, audience, placement, name, poster | Unchanged — Shortlist's. |
| Already-watched percentage, *Only series they haven't started* | Sent as `exclude`, and applied again to the answer. |
| Rebuild cadence, idle hold, pick order (best, rating, newest, shuffle, new first, rotate) | Unchanged — they decide when a row is rebuilt and how its titles are arranged. |
| *Happy to see again* (rewatch) | Built from history as always; "close to what they watch now" reads the watches the engine says its best titles follow from. |
| Children's & family titles | Reads each item's `kids`. |
| Seasons | Sent as `season` (see above). |
| Watches this row is built from (below the default), cycling | Sent as `seed_focus` with the row's own `seeds`. |
| Recent releases | The server-wide value is left to the engine's own ranking. A row that sets its own re-weights the engine's order by release date with the same formula Shortlist's engine uses — the engine's position stands in for its score. |
| Sources (the row names its own) | That row is built by Shortlist's own engine from those sources, as it would be with no engine. Rows on the server's sources go to your engine. |
| *Popular on this server* (shared) | Built from what people watched, never by an engine. |

Rows that share one engine answer — same libraries, media, exclusion rule and seeds — draw from it
**without replacement**, in row order, per person: the first row takes the best titles, the next the
best of what is left. Without that, rows differing only in size or cadence would each show the same
head of one list. Shortlist's own engine keeps its own behaviour here.

## What the run trace shows

Each person's run page lists the engine as a source: `engine:<name>`, how many titles it returned,
how many survived the library and the row's rules, and how long it took. A person whose engine call
failed shows that source as **failed** with the reason, followed by whichever engine actually built
the row. A pick's provenance line reads *suggested by Your engine (name)*.

## When the engine needs attention

Each run records the engine's health as it found it at the start (the fields above, or that it
could not be reached) and how many people's rows fell back to Shortlist's own engine that night, and
why. When the newest run found stale lists, a failed build, an unreachable engine or any fallback,
the dashboard bell raises **"… is not serving fresh recommendations"** with the details. A later
run that finds the engine healthy clears it; dismissing it hides it until a run finds trouble again.
The record is an audit event under the `engine.status` scope, so the event log keeps the history.
