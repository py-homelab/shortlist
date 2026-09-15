# Row types: base class + subclasses, or not? (2026-09-15)

Status: **review complete.** Recommendation: keep one row shape. The owner asked for every bug in §5 to be fixed
(done 2026-09-15). The placeholder module (§4) is not built and awaits a go/no-go.

The question (owner, during discussion #124): seasonal rows touched 37 production files. Should rows be
restructured as a shared base with one subclass per custom row type, so the next type lives in one place?

Method: 12 readers each read every line of one slice of the row code (engine, server, web, design docs:
~48,000 lines), mapping every place a row's type changes behaviour, which of those places tests pin, and
where a type decision touches Plex safety. Every claim below was either traced in code or probed with a
test (probes kept in the session scratchpad, not the repo). Live checks were read-only.

## 1. Answer

**No to one subclass per row type.** The goal is right; that mechanism does not fit this codebase.

**Yes to one targeted extraction: name placeholders.** That is where the repeated sprawl actually is, it
is pure logic with no Plex writes of its own, and it can be proven equivalent before the old code goes.

Everything else seasonal touches is seasonal behaviour living where that behaviour runs, not duplication.

## 2. Why not row kinds — what the code says

1. **Row types combine, and the combinations are used and tested.** A row with one `kind` cannot express
   them without forbidding working rows.
   - seasonal + shared: `rows.py:3528-3543`, `test_seasonal_rows.py:541-590`
   - seasonal + rewatch: `rows.py:551, 2578, 2649`, `test_seasonal_rows.py:509`
   - rewatch + unstarted_only share a pool key and both filter carry-forward: `rows.py:727-729, 2186-2205`
   - Every one of these is an independent column today (`db/models.py:167, 189-197, 247-251`).
2. **Three of the "types" are not row settings at all.**
   - *Shared* is a separate builder, run record, watch table, ledger key and privacy namespace:
     `_shared_row` (`rows.py:3426-3644`), `RunSharedRow` (`db/models.py:438`), `shared_<slug>` ledger key
     (`run_persistence.py:1327`), `shared_label_audiences` (`privacy.py:681`), 8+ branches in `pipeline.py`.
   - *Because you watched* is `{top_seed}` in the name text — including the global and per-person
     templates (`delivery.py:689`), so no row column could hold it.
   - *The default row* is an identity (`slug == "picked"`), not a kind: its name and size are global
     settings (`context_builder.py:1124-1125`), edited in Settings (`settings.py:533-554, 611-622`).
3. **Four of the nine templates are pure presets** (picked-for-you, fresh-finds, from-the-vault,
   movie-night: `row-templates.ts:48-50, 129-133, 181-184, 209-215`). Only seasonal has a large
   behaviour footprint. A framework for one real case is what `implementation-standard.md:22-28` forbids.
4. **The hooks a plug-in would need reach into the safety-critical code.** Moving seasonal behaviour
   behind hooks needs far more than the 9 first drafted, and several sit on identity, deletion or privacy
   ordering:
   - pool identity — a kind's filter must enter `pool_key` or two rows share a gather while excluding
     different things (`rows.py:2174-2221`, `test_ranking.py:806-817`);
   - carry-forward filtering (`rows.py:689-732`), final selection (`rows.py:356-406`), per-person
     prepare (`rows.py:429-458`), rebuild policy (`rows.py:2048, 2697, 2701-2716`);
   - a source registered *inside* `gather_candidates`, because it needs the private `measured` map
     (`candidates.py:841, 1052`);
   - title predictability for identity and deletion (`delivery.py:1094`, `collection_reconcile.py:448`,
     `context_builder.py:1229`) — a kind that answers "predictable" wrongly lets a sibling row's
     collection be matched by title and deleted (`delivery.py:1031-1037`);
   - three-state visibility with the pipeline, not the kind, owning what "dormant" does
     (`pipeline.py:596, 1700-1704`; `rows.py:3108`).
   Each hook is a new way for a future type to break rule 1 or delete the wrong collection.
5. **The engine/server line splits it.** Calendar answers must be pure and importable by the server,
   which calls them with no engine context and for past dates (`jobs.py:1782-1796`); the engine reads no
   clock (`rows.py:101-103`). One base class would span both.
6. **The web editor is not panel-shaped.** Type controls live inside common controls' slots
   (`row-editor.tsx:915-1019, 1111-1124, 1162-1172, 805-831`) and patches cross types
   (`:696, 800, 945-950, 1012, 1103, 1134`). A per-kind settings panel writing only its own settings
   cannot express those.
7. **Recorded owner decisions point the other way:** "every field stays editable after picking. A template
   is a starting point, never a mode" (`row-templates.ts:11-12`); "one flexible concept — a Row"
   (`shortlist-collections-design.md:5-6`).
8. **Migration risk.** A `kind` backfill would have to be guessed from placeholders and flags; migration
   0070 already got exactly that kind of guess wrong (`test_migrations.py:960-993`).

## 3. Where the sprawl actually is

Measured by grep, backend and web:

- `{top_seed}` / `{season}` / `{season_emoji}` checks in **13 backend files**, asking at least six
  different questions (below), at 50+ lines.
- The placeholder token list is re-typed in **8 web files** (`row-card.tsx:39-41`,
  `template-vars-hint.tsx:3-12`, `seasons.ts:14`, `format.ts:265-285`, `row-rename.tsx:227-230`,
  `defaults-section.tsx:49-62`, `step-customize.tsx:170-176`, `rows.tsx:50-51`).
- A second hand-written renderer: `rows._record_demand` (`rows.py:2434-2441`).
- The bug history this pattern has produced: #84 (invented titles), #121 (title claims), the default-row
  name guard needed at three doors (`review-backlog.md:544-558`), and in this change alone the season
  refusal had to be added to create, PATCH, rename, Settings and per-person name.

Adding the next placeholder today repeats all of it. That is the part worth extracting.

## 4. The design: one placeholder module

### 4.1 What it is

`shortlist/engine/placeholders.py` — one table and a handful of pure functions. No I/O, no Plex, no clock.
Engine-side, so the server imports it (the allowed direction).

| token | filled from | filled when | without a value | renderings | where allowed |
|---|---|---|---|---|---|
| `{user}` | person's display name | render | never missing | per person | everywhere |
| `{library_name}` | delivering library | render | removed, spaces collapsed | per library | everywhere |
| `{top_seed}` | top pick's seed | render (needs picks) | unfillable → fallback | unbounded | row name, global template; not fallback, not per-person template |
| `{season}`, `{season_emoji}` | row's season | template resolution (before picks) | unfillable | finite (catalogue) | row name, description, poster, on a row with seasons; not fallback, global or per-person template |

Operations (each replaces named call sites; §4.2):

- `fill(text, *, profile, library_name, top_seed, season)` — the one renderer (`delivery._fill` +
  `fill_season`; replaces the hand renderer in `rows._record_demand`).
- `unfillable(text, *, have_seed, have_season)` — `render_row_name`'s rule.
- `needs_a_run(text)` — "a title only a run can predict": identity by ledger only. Today spelled
  `"{top_seed}" in t or uses_season(t)`.
- `renderings(text)` — every catalogue rendering, for clash keys and claims (`season_renderings`).
- `refusal(text, where, *, row_has_seasons)` — one validator returning the plain-English 422 or None;
  `where` ∈ row name, fallback, global template, per-person template, description, poster.
- `forces_nightly(text)` — `{top_seed}` rows refresh nightly.
- `report_label(text, library_name)` — what reports and alerts show for an unrendered template.
- Web: `web/src/lib/placeholders.ts` — the one token list with meanings and where each is allowed; every
  hint, chip legend and preview renderer reads it.

### 4.2 Every call site that switches

Backend (line numbers at the time of review):

- `engine/delivery.py`: 387-412 (move `SEASON_PLACEHOLDERS`, `uses_season`, `season_renderings`,
  `fill_season` into the module), 415-424 (`_fill`), 454-465 (`render_row_name` unfillable + fallback
  rule), 469-492 (poster), 556-564 (description), 621, 680 (`resolve_row_template`), 758-771 (claims),
  1066/1094 (`remove_row` unrenderable)
- `engine/pipeline.py:1439` — **checks `{top_seed}` only, on the season-filled template.** A season
  placeholder is handled later by the empty render at 1460. The switch must keep that order exactly.
- `engine/rows.py`: 535-544 and 741-757 (`_names_a_seed`, rewatch seeds), 2048 (forced nightly),
  2434-2441 (hand renderer)
- `server/api/collections.py`: 166-185 (fallback validator), 916-924 (`_reject_season_name_without_seasons`)
  and its callers at 1122, 1426-1439, 1999-2001
- `server/api/settings.py:181-186`, `server/api/users.py:78-88`
- `server/api/support.py:1040` (effective cadence)
- `server/notifications.py:381-384, 424, 429` (`_usable_fallback`, `_row_display_name`)
- `server/services/collection_reconcile.py`: 159 (claims), 170-209 (probes, `title_key(s)`), 448
  (rendered titles)
- `server/services/report_service.py:481-490` (report label)
- `server/services/user_sync.py:114` (`{user}` → nickname rename)
- `server/services/context_builder.py:1229-1232` (retire skip)

Web: `format.ts` (`renderRowName`), `row-plex-card.tsx:14-20` (`renderDescription`), `template-vars-hint.tsx`,
`seasons.ts:14`, `row-card.tsx:39-41`, `row-rename.tsx:227-239`, `defaults-section.tsx:49-62`,
`step-customize.tsx:170-176`, `rows.tsx:50-51`, `run-rows.ts` (`rowDisplayName`).

### 4.3 What deliberately stays as it is

- `shared` stays a common build axis with its own builder.
- rewatch, unstarted_only, cooldown stay row flags.
- The seasonal calendar stays in `seasons.py` + `rows.row_shown_today`; its season-list filter stays
  at its five call sites. Extract a "title scope" only when a second list-driven row type exists.
- Candidate sources stay an if-chain until a second non-TMDB source needs `measured`.
- The recipe string format is untouched: any change rebuilds every row once (`rows.py:941-952`).
- No web kind registry.

## 5. Bugs this review found

### 5.1 In the uncommitted seasonal work — all fixed 2026-09-15, each with a failing test first

- **B1 (probe-confirmed).** When a seasonal row's season has nothing in a library and every other source
  fails, `gather_candidates` returns `[]` instead of raising "every candidate source failed": `season`
  is added to `attempted` but can never be in `failures` (`candidates.py:1031, 1062`).
- **B2 (probe-confirmed).** A TMDB genre-map failure for a media type no seed has escapes the season
  block (`add` → `genres_for`, `candidates.py:847`), failing the row's pool — the opposite of its own
  comment ("costs the weight, never the titles").
- **B3 (probe-confirmed).** A person whose only row is seasonal, on a night the list cannot be read, is
  reported `error`. With a sibling row they are `ok`; the cold path skips silently (`rows.py:2534-2541`);
  a shared row reports `skipped` (`rows.py:3530-3534`). Nothing is written in any case; the reporting is
  inconsistent. **Contract chosen:** an unreadable list is a row whose every source is down, on both
  per-person paths — the cold path now warns the same way and fails the person only when every row they
  have is unreadable. Shared rows keep their own `skipped` convention.
- **B4 (code).** Carried-forward picks are not re-checked against the season list (`_reusable_prior`,
  `rows.py:689-732`). Only a season change forces a rebuild, so a title TMDB drops from the list
  mid-season stays until the next refresh night.
- **B5 (code).** Renaming a seasonal row from the rename screen renames nothing on Plex and reports no
  error: the templates render "" without a season, so reconcile skips (`collection_reconcile.py:796-798,
  859-861, 877-883`). The next run renames in place. `{top_seed}` rows still behave this way.
  **Fixed differently than first proposed:** not today's season (a collection can still wear another),
  but every catalogue season, old and new filled with the same one (`_renamed_titles`); a plain name
  becoming seasonal still waits for the run.
- **B6 (code).** `_serialize` reads the clock twice (`collections.py:902, 907`), so across midnight
  `shown_today` and `season_status` can describe different days.
- **B7 (docs/comments).** Stale since #124: the `rows.visibility` job copy and docstring mention only
  day schedules (`jobs.py:325-334, 1739`; `scheduler.py:345-349`); `rows_considered` is documented with
  four values in three places (`engine/models.py:1406`, `db/models.py:418`, `schemas_runs.py:72`).
- **B8 (web).** The Plex-card description preview leaves `{season}` literal (`row-plex-card.tsx:14-20`);
  the preview's Seasons fact omits the days-after setting (`row-preview.tsx:249-256`).

### 5.2 Pre-existing, unrelated to #124 — all fixed 2026-09-15

- **X1 (probe-confirmed).** When a pool is larger than the pre-rank cut, `pre_rank` returns its picks
  sorted *without* genre avoidance, franchise or cast (`ranking.py:261`), and `diversify_by_seed` builds
  the row from that order. Precondition: `genre_avoidance` or `franchise` above 0 with `cast` at 0.
  Not active on the maintainer's server (all three at default 0).
- **X2 (probe-confirmed).** Creating a row with per-row request overrides silently drops them: the POST
  constructor sets no `req_*` column (`collections.py:1136-1178`). The editor offers those controls on a
  new row. Editing after saving works.
- **X3.** `ranking.py:31` names `test_recency_curve_matches_the_engine`, which does not exist: the web/engine
  half-life constant is unpinned.
- **X4.** `CollectionIn` silently ignores unknown keys (`collections.py:148`), contradicting
  `schemas.py`'s header. Fixed for the row body via `StrictRequestModel`; the header now says the older
  request bodies still ignore unknown keys.
- **X5.** The row editor asked "Which watch it follows" on a shared row with a stored seed budget of 1–2,
  while hiding the budget itself (`row-editor.tsx:1196`).
- **B9.** `pipeline.any_row_hidden_today` read placement only, not `dormant` — the engine relied on the
  server's `off` there despite its own comment. Now reads both.
- **X6 (found by the fix-round review).** Renaming a shared row from the rename screen dropped its
  `row_marker(0)` (`collection_reconcile.py`, shared branch), so the next run could not find its own
  collection and built a second one; the B5 shared seasonal rename also compared the marked title against
  unmarked keys and matched nothing. Both fixed; the shared rename tests now use the marked titles
  delivery actually writes (the old test's "no marker" premise was false).
- **Fix-round follow-ups.** A person failed because every due row has nothing to build from is now still a
  promotion candidate while a row of theirs is out of season (`NothingToBuildFrom`, both paths); season
  titles are narrowed to the row's media before the gather; a season left out for want of a genre list is
  recorded as a failed source.
- Stale docstrings in `rows.py` claimed the shared path used `effective_max_seeds` / `effective_recency` /
  `effective_recent_count` / `_candidate_pool`; it uses none of them. Corrected.

## 6. How we avoid breaking things

The risk is concentrated in four predicates that decide identity and deletion: `remove_row`'s
unrenderable test, reconcile's claims and rendered titles, the retire skip, and promotion's static-title
fallback. The plan makes a mistake there fail a test before it can reach Plex.

1. **Characterisation tests first**, for the cells the switch touches that no test pins today:
   - seasonal and `{top_seed}` rename in reconcile; the seasonal claim cell (`collection_reconcile.py:159`)
   - retire skip for a seasonal row (`context_builder.py:1229-1232`)
   - `other_rows` wiring through `_drop_cold_skipped_rows` / `_remove_muted_and_retired` (`rows.py:1741, 1789`)
   - report label for the default row
   - the seasonal inbox row name (`_record_demand`)
   - `{top_seed}` on a shared row never titled after one person (`rows.py:3602-3605`)
   - web: `renderDescription` and the Plex-card caption with season tokens
2. **Old-vs-new equivalence property tests** (hypothesis): generate templates from the five tokens plus
   literal text, blanks and whitespace, and assert every new operation returns exactly what each old call
   site computed — per call site, including `pipeline.py:1439`'s top-seed-only order. Only then delete the
   old code.
3. **Mutation probes on the identity predicates**: invert each of the four, confirm a test fails, restore.
   Line coverage cannot answer "which test protects this": the engine builds people on worker threads,
   and coverage's per-test tagging only follows the main thread (measured this session: a line every run
   executes attributed to 7 tests).
4. **Full gates**: `pytest`, `pnpm test`, `tsc -b --force`, `eslint .`, `vite build`, `pytest -m e2e`.
5. **Live golden comparison on the maintainer's server**, dry run, before and after: every person's
   rendered titles per row and library, and which collection each maps to. Identical or it does not ship.
6. **Architecture Review until a pass finds nothing** (it touches identity and deletion).

## 7. Order of work

1. ~~Fix B1–B9 and X1–X5~~ (done 2026-09-15, each with a failing test first), re-run the gates, commit #124.
   The older fixes (X1–X5) go in their own commits so a later regression names one cause.
2. The placeholder module as its own behaviour-preserving change, per §4 and §6, if approved.

## 8. Decisions for the owner

- Go / no-go on §4 (the placeholder module) after #124 lands.
- B3: one reporting contract when a seasonal list cannot be read — `ok` with a note, `skipped`, or `error`.
- Whether X1–X4 are fixed now or logged to `review-backlog.md`.
