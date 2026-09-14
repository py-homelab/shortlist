# Notifications design (Wave 4)

From the September 2026 audit — see `.claude/docs/audit-2026-09-programme.md`.
Shortlist has **no external notifications at all**; the only alert is an in-app bell the owner must
remember to open, for a tool that runs unattended overnight on someone else's server.

## The fact that shapes everything

**Shortlist already has a mature in-app notification system** — `shortlist/server/notifications.py`
(711 lines) is a registry of ~13 pure builder functions (`_last_run_problem`, `_failed_jobs`,
`_rows_we_cannot_hide`, `_filters_not_enforced`, `_recent_service_errors`, `_playback_listener_down`,
`_mdblist_quota`, `_requests_found_nothing`, `_shelf_contention`, `_rows_with_no_name_for_newcomers`,
`_owner_sees_all_rows`, `_runs_paused`, `_update_available`), each recomputing "is this true right
now" from current DB state and returning
`{id, severity, title, body, action_url, action_label, dismissable}`.

**This is not a system to design from scratch — it is an external pipe to wire onto an existing one.**

## 1. Which events go out

| Event                                                                                 | Verdict                                                                                                    |
| ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Whole run failed (`Run.status == "error"`)                                            | **Urgent** — the stated gap                                                                                |
| Run partly failed (`users_error > 0`)                                                 | **Digest** — common enough that paging trains the owner to ignore                                          |
| Plex/plex.tv unreachable                                                              | **Not a separate event** — it _causes_ the run/job failures; don't add a third detector for one root cause |
| Plex token expired                                                                    | **Urgent** — persistent and fully blocking (see open question 1)                                           |
| `privacy.sync` job failed                                                             | **Urgent, no debounce delay** — the job whose whole purpose is keeping rows hidden                         |
| Row could not be hidden (`_rows_we_cannot_hide`, `_filters_not_enforced`)             | **Urgent, highest priority** — already non-dismissable in-app because it describes a LIVE exposure         |
| Other job failed                                                                      | **Urgent if `writes_plex`** (that flag already exists in `jobs.py`), digest otherwise                      |
| Request needs approval                                                                | **Digest, count only** — a workflow item, nothing is broken                                                |
| New release available                                                                 | **In-app only** — the paradigm case of a notifier training itself to be ignored                            |
| MDBList quota, shelf contention, unnamed rows, owner-sees-all, requests-found-nothing | **In-app only**                                                                                            |

**Rule:** notify externally only for (a) an unattended failure the owner cannot otherwise see before
their next login, or (b) a live privacy exposure.

## 2. Severity model

Reuse the registry's existing `severity`, plus one **new orthogonal** field `urgency` — they don't
line up 1:1 (a partial-run failure is `warning` severity but should still go out, just not at 3am).

- **Urgent** — short debounce, then send.
- **Digest** — one message a day at a fixed local time (default 08:00). **Skipped entirely if empty** —
  no "all clear" ping nobody asked for.
- **In-app only** — never leaves the server.

**Quiet hours are opt-in, OFF by default.** Some urgent events genuinely should interrupt sleep — that
is the feature. An owner who disagrees sets `notify.quiet_hours.*`, and urgent sends during the window
are held and merged into the next digest.

## 3. Channels — webhook + email

**Generic webhook** (JSON POST with a `style` selector: generic / Discord / Slack) is the
highest-leverage primitive: it is what Sonarr/Radarr/Overseerr call "Webhook", and self-hosters
already know how to point one at Discord, Slack, Home Assistant or n8n. Three tiny body-shape
formatters cover more real-world ground than one vendor SDK.

**Email (SMTP)** needs no third-party account, and stdlib `smtplib`/`email.mime` means **zero new
dependencies** — no `requirements.lock` regeneration.

Channel interface is one protocol: `send(title, body, severity, action_url) -> None`, raises on
failure. A third channel is a follow-up class — nothing pre-built to only receive test pings.

## 4. Delivery — reuse the job queue, don't build a second one

Retry-with-backoff, a `failed` terminal state and dead-letter visibility **already exist and are
tested** in `shortlist/server/services/jobs.py`.

**Hooks (event-driven, no new poller):**

- `services/run_service.py` where `Run.status` is finalised to `"error"` (~line 481)
- `services/jobs.py` `_finish()` where a job goes `failed` and already writes the `job.failed` audit
  event (~line 605)
- Everything else evaluates once a day inside the digest job.

**Retry:** `max_attempts=5` with a longer tail than the default `_BACKOFF_S = (30, 300, 900)` —
`(60, 300, 1800, 3600, 10800)`, spanning ~4h, since a webhook/SMTP outage lasts hours not minutes.
Each attempt makes **one** HTTP call — deliberately not layering the client's own retry underneath
the queue's retry.

**Channel down for a day:** after ~4h the job lands `Job.status == "failed"`, which **already** fires
the existing `_failed_jobs` in-app alert — the one channel that always works, because it doesn't
depend on the broken thing. That falls out of reusing the queue; nothing to build.

**On recovery, don't replay stale messages verbatim** — "the most recent run failed" is simply wrong
if it's a day late. Collapse everything pending for that channel into one recovery message:
_"This channel was unreachable for ~14h. 3 alerts happened — open Shortlist to see them."_
Threshold `notify.max_stale_hours`, default 6.

**40 users failing at once is already handled at source.** Every builder returns at most ONE dict
summarising an aggregate — `_last_run_problem` already says "5 people failed", not five
notifications. The debounce is for a narrower case: several _different_ builders tripping in one bad
night.

## 5. Bundling and dedup

**Debounce, not a calendar hour.** An hour is too long to sit on "the run failed".

- On first urgent fire, look for an already-`queued` `notify.send` job for that channel whose
  `not_before` is still in the future. If found, **append** to its payload (an UPDATE, no new job).
  Otherwise enqueue with `not_before = now + notify.coalesce_window_s` (default 120s).
- Requires a nullable **`not_before` column on `jobs`** — today `_claim()` only defers _retries_; a
  brand-new job is claimable instantly. `_claim()` gains
  `Job.not_before.is_(None) | Job.not_before <= now`. Small, generically useful, worth flagging for
  review on its own.

**"Already read in-app, don't send"** — uses a distinction the code already makes:

- **Dismissable ids:** at _send_ time (not enqueue time, so a dismissal during the debounce counts),
  re-read `store.get(DISMISSED_KEY)` and drop any item now dismissed → `status="skipped_read"`.
  Reuses the exact key the bell already writes.
- **Non-dismissable ids** (`rows-we-cannot-hide`, `filters-not-enforced`, `runs-paused`,
  `playback-listener-down`): these can _never_ be marked read by design — hiding the alert would hide
  the live exposure. Different guard: don't re-notify the same `notification_id` more than once per
  `notify.reminder_interval_hours` (default 24h), checked against the outbox table. So an exposure
  gets an immediate alert, then a daily reminder while it remains true, and is never silently dropped.

## 6. Settings and secrets

Add to `DEFAULTS` in `settings_store.py` (`KNOWN_KEYS` in `api/settings.py` derives automatically):

```python
"notify.enabled": False,                 # master switch, opt-in
"notify.webhook.enabled": False,
"notify.webhook.style": "generic",       # generic | discord | slack
"notify.email.enabled": False,
"notify.email.smtp_host": "",
"notify.email.smtp_port": 587,
"notify.email.smtp_user": "",
"notify.email.smtp_tls": True,
"notify.email.from_addr": "",
"notify.email.to_addr": "",               # single address — admin-only
"notify.coalesce_window_s": 120,
"notify.digest_cron": "",                 # blank = built-in default
"notify.quiet_hours.enabled": False,
"notify.quiet_hours.start": "",           # "HH:MM" local
"notify.quiet_hours.end": "",
```

Add to `SECRET_KEYS` (Fernet at rest, redacted by `all_public()`, `REDACTED_PLACEHOLDER` round-trip
already handled by the settings PUT path):

```python
("notify.webhook.url",)  # a Discord/Slack webhook URL IS a bearer token in a URL
("notify.email.smtp_password",)
```

**Support-bundle gap:** `api/support.py`'s `_SECRET_PATTERN` only catches `token|apikey|key|secret`
labelled pairs — it would NOT catch a raw webhook URL echoed inside an httpx exception in
`Job.error`. **The `notify.send` handler must run exceptions through the existing `redact()` helper
before writing `Job.error`**, exactly as other job handlers do.

**Privacy-adjacent, flagged deliberately:** the highest-priority events name specific accounts
("sarah can see mike's row"). Sending that to a third-party webhook or plain SMTP hands
identity-mapping data to a system outside Shortlist's control — arguably making a privacy incident
_more_ exposed. **Recommendation: the external body for these says "N accounts can see rows that
aren't theirs — open Shortlist to see who"**; full detail stays in-app.
**This feature warrants Architecture Review** per `.claude/CLAUDE.md` (touches secrets and transmits
identity-shaped data off-server) even though it never writes to Plex.

## 7. Data model

**`events` cannot carry this** — it is an append-only audit log (rule 10); delivery needs mutable
state. Conflating them turns an immutable audit table into a mixed-purpose one.

**One new table `notification_outbox`** — _what_ we decided to tell the owner, decoupled from _how_
it is sent: `id`, `created_at`, `notification_id` (indexed), `kind`, `severity`, `urgency`, `title`,
`body`, `action_url`, `status` (`pending|sent|skipped_read|failed`, indexed), `sent_at`.

**One new column** `jobs.not_before` (nullable DateTime) — see §5.

Migration `0090_notifications.py`, revises `0089`. Follows the project's existing guarded pattern
(`if table not in inspector.get_table_names()`, `if column not in _columns(bind, "jobs")`), with a
real `downgrade()` that drops both.

**New job kinds in `jobs.py` CATALOG:**

- `notify.send` — `manual=False`, `writes_plex=False`, payload `{channel, items: [...]}`
- `notify.test` — `manual=True`; builds one synthetic item and pushes it through the **exact same**
  `notify.send` code path with no debounce. **This is the deliberate antidote to the competitor's
  ten-channels-two-wired flaw** — the test button and a real 3am failure travel identical code.
- `notify.digest_send` — `manual=True`, `schedule_setting="notify.digest_cron"`

## 8. Testing

No network in tests. `respx` is already a dev dependency and already used this way
(`tests/unit/test_arr.py`, `test_search.py`). Email uses stdlib `smtplib` — monkeypatch
`smtplib.SMTP`/`SMTP_SSL` with `MagicMock`.

New `tests/unit/test_notify_delivery.py` must prove:

- **Assert the boundary kwargs, not just "called once"** (per `.claude/rules/testing.md`) — the exact
  JSON body per style, the exact email envelope.
- **Dedup matrix** — a dismissable id already dismissed at send time is skipped; a non-dismissable id
  re-sends only after `reminder_interval_hours`.
- **Coalescing** — two urgent conditions inside the window produce ONE job with two items; outside it,
  two jobs.
- **Retry/dead-letter** — a channel that always raises exhausts `max_attempts`, lands `failed`, and
  that is what makes `_failed_jobs` fire, closing the loop back to the channel that always works.
- **Digest** — empty sends nothing; non-empty is one message.
- **Secret redaction** — `all_public()` returns `•••••`; a PUT with `REDACTED_PLACEHOLDER` leaves the
  stored value untouched (grep for existing coverage first, don't near-duplicate).
- **Matrix** — channel state (webhook only / email only / both / neither) × urgency.

## 9. Smallest useful v1 — about a day

Cut everything that is not "a run failed overnight and nobody knew":

1. **Webhook only**, generic JSON. No Discord/Slack variants yet; email is the fast-follow.
2. **One event: whole-run failure.** Job-failure and privacy-exposure detection are the highest-value
   additions but carry the §6 redaction nuance — do them right, not rushed.
3. **No outbox table, no debounce.** One event type firing once per run has no burst to coalesce.
   Hook `run_service.py`'s error path straight to a `notify.send` job with the long backoff.
4. **No dismissed-list check** — one notification per real failure is not spam.
5. **Two settings:** `notify.webhook.url` (secret) + `notify.webhook.enabled`.
6. **A test button** (`notify.test`) so the owner can prove it works before trusting it.

One settings key pair, one job kind, one ~30-line sender, one hook, one test file. Everything later
slots on top without reworking this.

## Open questions — decide, don't assume

1. **Plex token expiry** — is there a distinguishable "token rejected" vs "server unreachable" error
   signature in the Plex client today? If not, that is separate small research before it can be its
   own event rather than folding into generic run-failure.
2. **Digest default time** — 08:00 local is a guess; depends when the owner checks their phone
   relative to the ~03:00–06:00 run window.
3. **Redacting names from external privacy alerts (§6)** — is "N accounts, open the app" acceptable,
   or does losing the name defeat a 3am ping (nothing is actionable until they open the app anyway)?
4. **Email recipients** — one `to_addr` assumed since this is admin-only. Some owners forward to a
   co-admin; confirm.
5. **What is v1.1** — job-failure + privacy-exposure detection, or email as second channel?

## v1.1 — the owner picks the events, and the webhook can authenticate (2026-09-14)

Owner decisions, taken in chat on 2026-09-14. They supersede §1's "notify externally only for
failures and exposures": the owner asked for every event that makes sense, including started and
finished, so the RULE moves into the owner's hands and the defaults keep today's quiet behaviour.

**Events** (`notify.webhook.events`, a list; default `["run.failed", "privacy.exposure"]`):

- `run.started`, `run.finished`, `run.partial` (finished, `users_error > 0`), `run.failed`,
  `run.stopped` (aborted by the owner, or found orphaned at boot). Dry runs never send. A run
  cancelled while still queued never started, so it sends nothing.
- `job.started`, `job.finished`, `job.failed`. Failed means out of retries, never an attempt that
  will be retried, and a retry does not send `job.started` again. Started/finished skip `routine`
  kinds (one playback credit per play); failed never does. `privacy.sync` runs every 30 minutes but is
  NOT routine, so it takes the Jobs page's own Recent rule: a scheduled pass never sends
  `job.started`, and one whose result is `quiet` never sends `job.finished`. `notify.send` never reports on itself — a webhook that is
  down would otherwise queue an alert about failing to send an alert, for ever. Titles use the
  catalogue's `label`; the body never carries `Job.detail` or `Job.error`, which can name a person.
- `privacy.exposure`: the newest measured `unhideable_rows` / `filters_not_enforced` /
  `unreadable_filters` is non-empty. Counts only, never names (§6). Checked after every real run.
  Re-sent at most once per 23h while it stays true (a day less an hour: the stamp is when a nightly run
  ends, and a night that ends sooner must not skip to the next); when it clears, the clock resets, so a new
  exposure alerts at once.
- `requests.waiting`: after a real run, when more titles wait for approval than the last time this
  sent. The count it last saw is stored and follows decreases too.
- `update.available`: after a real run, once per newer version.

The run-level checks (privacy, requests, update) run in an executor after the run is persisted —
`check_for_update` can reach GitHub, and that must not block the event loop.

**Wording** lives in `notifications.py` beside `run_failed_alert`, one builder per event, so the bell
and the webhook still share one module for what is said. Every body names no account; the key-set test
covers every event.

**Body:** adds `event` (the id above, `"test"` for the button). Additive, so `version` stays 1.

**Auth:** `notify.webhook.auth_header_name` (default `Authorization`) and
`notify.webhook.auth_header_value` (SECRET_KEYS: Fernet at rest, redacted on read). Sent on every
POST, the test included, only when the value is set. The name must be an RFC 7230 token and the value
must be h11-valid — printable ASCII, whitespace only between words (422 at save). `scrub` removes the
value from any error text, in its escaped bytes form too, as well as the URL.

**State** the sender keeps for itself — `notify.webhook.privacy_sent_at`,
`notify.webhook.requests_seen`, `notify.webhook.update_sent` — is in PRIVATE_KEYS: never public,
never writable through the settings PUT.

**No migration.** Settings rows fall back to DEFAULTS, so an install that already switched the webhook
on keeps its run-failure alert and gains the privacy one.

**UI (reworked the same day, owner decision):** where messages go is a **Webhook** card in Connections —
address, header name and header value in one form, with "Send a test" (never run on page load: it posts
a real message) and Remove, like every other service. A blank header name sends no header, which is
how the owner takes one off without removing the webhook. The Notifications section keeps only the
on/off switch and "What to send" (Runs / Jobs / Everything else), and points at the card when no
address is saved.
