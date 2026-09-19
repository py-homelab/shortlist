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

The title sources and AI web search are Shortlist's own engine's; they disappear from the page while
an external engine is chosen, and come back when you switch back. Switching back is the whole
rollback: nothing else changes.

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
{"name": "recommendarr", "version": "0.2.0", "surfaces": ["library"], "serves_cold": true, "ready": true}
```

`name` is what the run trace and the row provenance call it. `serves_cold` says whether the engine
wants to be asked about people with fewer watches than **Enough watch history** — Shortlist's own
cannot seed a search from a thin history, so it cold-starts those people up front; an engine that
answers `true` is asked like anyone else, and only if it returns nothing do they get the cold-start
row. `ready: false` means it has no build to serve from yet; the Test button says so.

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
- `season` is a seasonal row's title set (`{"movie": [...], "show": [...]}`, null otherwise), for
  an engine that wants to rank within it; Shortlist filters the answer to the season's titles itself.

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

Only `tmdb_id` and `media_type` (`movie` | `show`) are required per item; a malformed item is
skipped, not fatal. Send a token as `Authorization: Bearer …` if you set one. Shortlist retries a
timeout or a 5xx a few times, then treats the pool as failed and applies the fallback setting.

## What the run trace shows

Each person's run page lists the engine as a source: `engine:<name>`, how many titles it returned,
how many survived the library and the row's rules, and how long it took. A person whose engine call
failed shows that source as **failed** with the reason, followed by whichever engine actually built
the row. A pick's provenance line reads *suggested by Your engine (name)*.
