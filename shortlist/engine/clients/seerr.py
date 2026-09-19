"""Overseerr / Jellyseerr client: what a person's own picks page needs from the *seerr.

The two products share one API (``/api/v1``, ``X-Api-Key``), so one client serves both. The *seerr
owns the download apps: quality profile, root folder, 4K routing and approval are its rules, and
Shortlist has no opinion about them — it only says which title a person wants, filed AS that person
(``X-API-User``, see `SeerrPersonClient.request_as`).

Shows are keyed by TMDB id, not TheTVDB, so no TVDB crossing exists here.
"""

from __future__ import annotations

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.models import MediaType, SeerrTarget

#: ``MediaInfo.status``, mapped to the vocabulary the picks page speaks.
#:
#: Only the codes that mean the same thing across the whole family are mapped, and that restraint is
#: load-bearing, because **the number 6 does not**. Overseerr's published spec calls it DELETED;
#: Seerr's own shipped `seerr-api.yml` says DELETED too — and its running code
#: (`/app/dist/constants/media.js`, read off a live 3.4.1) says:
#:
#:     UNKNOWN=1 PENDING=2 PROCESSING=3 PARTIALLY_AVAILABLE=4 AVAILABLE=5 BLOCKLISTED=6 DELETED=7
#:
#: So 6 is "the owner said never" on one product and "it was removed" on another — opposite meanings
#: for the same number, and the vendor's own spec is wrong about its own code. 7 is undocumented
#: everywhere yet accounted for 821 of 5,000 sampled rows on a real server.
#:
#: Everything unmapped therefore falls through to "not known", i.e. requestable, which is the safe
#: direction for a DELETED title.
_STATUS_BY_CODE = {
    # Its own word, not "queued": PENDING means the request is sitting in the *seerr waiting for a
    # person, which must not be dressed up as the machine working.
    2: "awaiting_approval",  # PENDING
    3: "queued",  # PROCESSING — approved and handed to the download app, which may not have it yet
    4: "queued",  # PARTIALLY_AVAILABLE — some of a show has landed; the rest is still wanted
    5: "downloaded",  # AVAILABLE
}

#: What "downloading" is actually decided by. ``PROCESSING`` is not it: on a real server 76 rows were
#: PROCESSING and exactly ONE was downloading — the rest are approved-but-unreleased films and airing
#: series, resting there indefinitely. ``downloadStatus`` is the download client's own live view
#: (it carries sizeLeft/timeLeft/estimatedCompletionTime), so a non-empty one is the only honest
#: "moving right now" signal the API offers.
_DOWNLOADING = "downloading"

#: ``mediaType`` as Overseerr spells it, per Shortlist ``MediaType``.
_MEDIA_TYPE = {MediaType.MOVIE: "movie", MediaType.SHOW: "tv"}

#: Overseerr's permission bits, read off a live Seerr 3.4.1 (`/app/dist/lib/permissions.js`) rather
#: than guessed — the same source that settled the status enum, and for the same reason: the
#: published spec documents `permissions` only as "a number".
#:
#: ADMIN is special. That file's own comment: "If the user has the admin permission, true will always
#: be returned from this check" — so an admin auto-approves everything without carrying any of the
#: AUTO_APPROVE bits, which is exactly the account most owners' API keys belong to.
#: `userType` on a Seerr account: 2 is a local account created in Overseerr itself; 1 is a Plex user,
#: and the live instance this was built against had nothing else. Only LOCAL is named, because the
#: classification defaults to "person" for anything it cannot read — see `is_plex_user`.
_USER_TYPE_LOCAL = 2

_PERM_ADMIN = 2
#: Not an auto-approve bit by name, but Overseerr's own approval rule treats it as one. Read from
#: where the rule lives (`/app/dist/entity/MediaRequest.js` on a live 3.4.1), not from the enum file:
#:
#:     status: user.hasPermission([AUTO_APPROVE, AUTO_APPROVE_MOVIE, MANAGE_REQUESTS], {type:'or'})
#:               ? MediaRequestStatus.APPROVED : PENDING
#:
#: Omitting it labelled such an account "requests wait for approval" on the settings screen while its
#: titles in fact went straight through to Radarr unreviewed — the exact inversion that screen exists
#: to prevent.
_PERM_MANAGE_REQUESTS = 16
_PERM_AUTO_APPROVE = 128
_PERM_AUTO_APPROVE_MOVIE = 256
_PERM_AUTO_APPROVE_TV = 512

#: 403 is a WORKING key whose account lacks a permission — not a bad key, which is what a shared
#: "rejected the API key" message said, sending owners off to regenerate a key that was fine.
#:
#: The permission is named PER CALL, because the reads a scoped key trips want different ones: the
#: media read needs Manage Requests, while listing the accounts (to match a person to their own
#: *seerr account) needs Manage Users. One shared message sent the owner to grant the wrong
#: permission on the very screen meant to diagnose it. Filing is different again: see `_post_status`.
_FORBIDDEN = "{app} accepted the API key but refused this — its account needs the {permission} permission"
_MANAGE_REQUESTS = "Manage Requests"
_MANAGE_USERS = "Manage Users"


class SeerrError(RuntimeError):
    """An Overseerr/Jellyseerr call failed — connection, auth, or a rejected request.

    Never carries the URL or api key: the message is surfaced in the UI and written to events, and a
    *seerr api key is a secret like any other (plex-safety rule 9).
    """


class SeerrClient:
    """Talks to one Overseerr/Jellyseerr instance."""

    app_name = "Overseerr"

    #: ``/media``, ``/user`` and ``/request`` are paged. Sized from a measurement, not a guess: a
    #: real server holds 26,941 media rows, and walking it at Overseerr's own UI page size of 100 took
    #: 270 requests and 5.0s against 27 requests and 1.5s at 1000. The endpoint honours far larger
    #: values still (5,000 in 0.19s), but 1000 is where the request count stops being the cost.
    #:
    #: The cap is a hard stop for a server that ignores ``skip`` and answers with a full page for
    #: ever — 500k rows is far past any real library, so reaching it means paging is broken.
    _PAGE_SIZE = 1000
    _MAX_PAGES = 500

    def __init__(
        self,
        target: SeerrTarget,
        *,
        timeout: float = http_retry.DEFAULT_TIMEOUT_S,
        min_write_interval: float = 1.0,
        write_clock: list[float] | None = None,
    ):
        self._target = target
        self._base = target.url.rstrip("/")
        self._timeout = timeout
        self._min_write_interval = min_write_interval
        # Shared per SERVER by the caller: several clients pointing at one instance must not multiply
        # the write rate (plex-safety rule 6).
        self._write_clock = write_clock if write_clock is not None else [0.0]
        # Both memoised per client, and the error deliberately as well: without it an unreachable
        # Overseerr is re-walked (three HTTP retries deep) once per caller, and every one of those
        # walks fails for the same reason.
        self._media_state: dict[tuple[str, int], str] | None = None
        self._media_error: SeerrError | None = None

    @property
    def target(self) -> SeerrTarget:
        """Which instance this client talks to — so a caller can key a client cache by it."""
        return self._target

    def _headers(self, *, as_user: int = 0) -> dict[str, str]:
        headers = {"X-Api-Key": self._target.api_key}
        if as_user:
            # The API key acts AS this account: its permissions, quota and approval, not the key's own
            # (Overseerr, Seerr and Jellyseerr `server/middleware/auth.ts`).
            headers["X-API-User"] = str(as_user)
        return headers

    def _get(self, path: str, *, permission: str = _MANAGE_REQUESTS, **params: object) -> object:
        try:
            r = http_retry.get(
                f"{self._base}/api/v1{path}", headers=self._headers(), params=params or None, timeout=self._timeout
            )
        except httpx.HTTPError as e:
            raise SeerrError(f"{self.app_name} unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise SeerrError(f"{self.app_name} rejected the API key")
        if r.status_code == 403:
            raise SeerrError(_FORBIDDEN.format(app=self.app_name, permission=permission))
        if r.status_code != 200:
            raise SeerrError(f"{self.app_name} GET {path} returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            # A 200 carrying HTML is a reverse proxy or SSO interstitial, not the app. Say which,
            # because "expecting value: line 1" sends people to the wrong place entirely.
            raise SeerrError(f"{self.app_name} returned a non-JSON body — check the URL and any proxy") from e

    def _post_status(self, path: str, body: dict, *, as_user: int = 0) -> tuple[int, object]:
        """A POST whose non-2xx answers are the caller's to read: ``(status code, JSON body or {})``.
        Raises only when the app cannot be reached or rejects the KEY — those are ours, not theirs."""
        self._throttle()
        try:
            r = http_retry.request(
                "POST",
                f"{self._base}/api/v1{path}",
                headers=self._headers(as_user=as_user),
                json=body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as e:
            raise SeerrError(f"{self.app_name} unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise SeerrError(f"{self.app_name} rejected the API key")
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def _throttle(self) -> None:
        """At most one write per ``min_write_interval`` seconds — be a polite client (rule 6 spirit)."""
        self._write_clock[0] = http_retry.throttle(self._write_clock[0], self._min_write_interval)

    def whoami(self) -> int | None:
        """The id of the account this API key acts as, or None if it cannot be read.

        Needed because "Server default" in the UI means *this* account, and what it does — approve
        instantly or file for review — is the single most consequential thing on that screen. Without
        the id there is no way to look it up in the user list and say so.
        """
        try:
            me = self._get("/auth/me")
        except SeerrError as e:
            logger.debug("{}: could not identify the API key's own account ({})", self.app_name, e)
            return None
        return _int_or_none(me.get("id")) if isinstance(me, dict) else None

    def ping(self) -> str:
        """A tiny AUTHENTICATED call for the settings 'Test' button; returns a friendly line.

        Deliberately not ``/status``, which the API declares ``security: []`` — it answers 200 to an
        empty or wrong key, so testing against it would call a broken connection healthy.
        """
        me = self._get("/auth/me")
        who = _name_of(me) if isinstance(me, dict) else ""
        return f"Connected to {self.app_name} as {who or '?'}"

    def users(self) -> list[dict]:
        """``[{id, name, plex_id, ...}]`` — every account the instance knows, so a person signed in to
        Shortlist can be matched to their own *seerr account."""
        out: list[dict] = []
        for row in self._paged("/user", permission=_MANAGE_USERS):
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            perms = _int_or_none(row.get("permissions")) or 0
            out.append(
                {
                    "id": int(row["id"]),
                    "name": _name_of(row) or f"User {row['id']}",
                    # The Plex account behind a Plex-linked user, so a person signed in to Shortlist
                    # can be matched to their own *seerr account and request as themselves. None for
                    # a local account, and for keys without Manage Users (the field is hidden then).
                    "plex_id": _int_or_none(row.get("plexId")),
                    # Whether THIS account's requests skip Overseerr's approval queue — the difference
                    # between "filed" and "already downloading" on a person's own page.
                    "auto_approve_movies": _approves(perms, _PERM_AUTO_APPROVE_MOVIE),
                    "auto_approve_tv": _approves(perms, _PERM_AUTO_APPROVE_TV),
                    # A real person who uses the server, rather than a local account the owner made.
                    # `!= LOCAL`, not `== PLEX`: an absent or unrecognised userType must land on
                    # "person", because that is the cautious side.
                    "is_plex_user": _int_or_none(row.get("userType")) != _USER_TYPE_LOCAL,
                }
            )
        return out

    def media_state(self) -> dict[tuple[str, int], str]:
        """Everything this instance knows about, as ``{(media_type, tmdb_id): status}``.

        One paged walk answers both "does it already have this?" and "what is this title's state?",
        because Overseerr's media table already IS the union of the Plex library and everything
        requested.

        ``media_type`` is Shortlist's own vocabulary (``movie`` / ``show``), not Overseerr's
        (``movie`` / ``tv``), so callers can key it against ``MediaType.value`` directly.

        Memoised: one client should not walk the library twice.
        """
        if self._media_error is not None:
            raise self._media_error
        if self._media_state is None:
            try:
                self._media_state = self._fetch_media_state()
            except SeerrError as e:
                self._media_error = e
                raise
        return self._media_state

    def _fetch_media_state(self) -> dict[tuple[str, int], str]:
        state: dict[tuple[str, int], str] = {}
        rows = 0
        typed = 0
        for row in self._paged("/media"):
            if not isinstance(row, dict):
                continue
            rows += 1
            kind = _media_type_of(row)
            tmdb_id = _int_or_none(row.get("tmdbId"))
            if kind is None or tmdb_id is None:
                continue
            typed += 1
            if row.get("downloadStatus"):
                state[(kind, tmdb_id)] = _DOWNLOADING
                continue
            status = _STATUS_BY_CODE.get(_int_or_none(row.get("status")) or 0)
            if status is not None:
                state[(kind, tmdb_id)] = status
        if typed < rows:
            # `mediaType` is on the live response but NOT in the published `MediaInfo` schema, so a
            # fork or a future version dropping it lands exactly here — and an unusable row is
            # indistinguishable from a library that simply does not hold the title. The run still
            # picks page fails open (a redundant request, never a wrong one), but it must not do so
            # silently.
            #
            # Graded, not all-or-nothing: the guard used to fire only when NOT ONE row was usable,
            # so a version that dropped the field on half its rows passed silently — and half a
            # library quietly becoming re-requestable is the case worth hearing about. A handful of
            # odd rows on a healthy server is normal, so that stays at debug.
            level = "WARNING" if typed * 2 < rows else "DEBUG"
            logger.log(
                level,
                "{}: {} of {} media rows carried no usable mediaType + tmdbId — those titles are "
                "invisible to the already-have check, so a person may be offered them again",
                self.app_name,
                rows - typed,
                rows,
            )
        return state

    def _paged(self, path: str, *, permission: str = _MANAGE_REQUESTS, **params: object) -> list[object]:
        """Walk a ``{pageInfo, results}`` endpoint to the end.

        ``pageInfo`` is believed over the size of the batch, because a server or proxy that CAPS
        ``take`` answers the whole question with page one: a short batch then looks exactly like the
        end of the list, and the walk returns 100 of 26,941 rows with every row perfectly usable, so
        nothing downstream can tell. `pageInfo` is in the same payload saying otherwise.
        """
        out: list[object] = []
        expected: int | None = None
        for _page in range(self._MAX_PAGES):
            payload = self._get(path, permission=permission, take=self._PAGE_SIZE, skip=len(out), **params)
            results = payload.get("results") if isinstance(payload, dict) else None
            batch = results if isinstance(results, list) else []
            info = payload.get("pageInfo") if isinstance(payload, dict) else None
            if isinstance(info, dict) and expected is None:
                expected = _int_or_none(info.get("results"))
            out.extend(batch)
            if not batch:
                break
            if expected is not None:
                if len(out) >= expected:
                    return out
                continue  # more to come, whatever the batch size said
            if len(batch) < self._PAGE_SIZE:
                return out
        else:
            logger.warning(
                "{}: {} paging hit the {}-page safety cap — reporting a partial list",
                self.app_name,
                path,
                self._MAX_PAGES,
            )
        if expected is not None and len(out) < expected:
            # The server said how many there were and we did not get them. Silence here is what a
            # take-capping proxy looks like, and it is indistinguishable from a small library.
            logger.warning(
                "{}: read {} of the {} rows {} says it has — the rest are invisible to this run",
                self.app_name,
                len(out),
                expected,
                path,
            )
        return out


#: What a person's own request page needs to know about one of their requests, by Overseerr's
#: request `status` (`MediaRequestStatus`): PENDING, APPROVED, DECLINED.
_REQUEST_STATUS = {1: "pending", 2: "approved", 3: "declined"}


class SeerrPersonClient:
    """The *seerr calls a PERSON's own picks page makes, on top of a `SeerrClient`.

    Separate from the client the run uses so the run's memoised `media_state` (one walk per run) is
    untouched by page loads, and so what a page may do is exactly this list: read who they are there,
    their quota, their own requests, and file ONE request as them. Every write goes out with
    `X-API-User`, so it lands under that person's permissions, quota and approval — never the key's.
    """

    def __init__(self, client: SeerrClient):
        self._c = client

    @property
    def app_name(self) -> str:
        return self._c.app_name

    def user_for_plex(self, plex_account_id: int) -> dict | None:
        """The *seerr account linked to this Plex account, or None (needs Manage Users on the key)."""
        return next((u for u in self._c.users() if u.get("plex_id") == plex_account_id), None)

    def quota(self, user_id: int) -> dict:
        """``{"movie": {limit, used, remaining, days, restricted}, "tv": {...}}`` — limit 0 = unlimited."""
        raw = self._c._get(f"/user/{user_id}/quota")
        out: dict[str, dict] = {}
        for kind in ("movie", "tv"):
            part = (raw.get(kind) if isinstance(raw, dict) else None) or {}
            limit = _int_or_none(part.get("limit")) or 0
            out[kind] = {
                "limit": limit,
                "used": _int_or_none(part.get("used")) or 0,
                "days": _int_or_none(part.get("days")),
                "remaining": None if not limit else (_int_or_none(part.get("remaining")) or 0),
                "restricted": bool(part.get("restricted")),
            }
        return out

    def requests_by(self, user_id: int) -> dict[tuple[str, int], dict]:
        """This person's own requests, ``{(media_type, tmdb_id): {status, media_status, request_id}}``
        keyed on Shortlist's media words (movie / show), like `media_state`."""
        out: dict[tuple[str, int], dict] = {}
        for row in self._c._paged("/request", requestedBy=user_id, filter="all", sort="added"):
            if not isinstance(row, dict):
                continue
            media = row.get("media") if isinstance(row.get("media"), dict) else {}
            kind = _media_type_of(media)
            tmdb_id = _int_or_none(media.get("tmdbId"))
            if kind is None or tmdb_id is None:
                continue
            out[(kind, tmdb_id)] = {
                "status": _REQUEST_STATUS.get(_int_or_none(row.get("status")) or 0, "pending"),
                "media_status": _STATUS_BY_CODE.get(_int_or_none(media.get("status")) or 0),
                "request_id": _int_or_none(row.get("id")),
            }
        return out

    def first_regular_season(self, tmdb_id: int) -> int | None:
        """The lowest non-specials season number of a show, or None if the app lists none."""
        payload = self._c._get(f"/tv/{tmdb_id}")
        seasons = payload.get("seasons") if isinstance(payload, dict) else None
        listed = (_int_or_none(s.get("seasonNumber")) for s in seasons or [] if isinstance(s, dict))
        numbers = sorted(n for n in listed if n and n > 0)
        return numbers[0] if numbers else None

    def request_as(self, user_id: int, tmdb_id: int, media_type: MediaType) -> dict:
        """File ONE request as this person. Never raises for an answer the app gave on purpose.

        Returns ``{"ok", "code", "message", "request_id", "status"}``: code is ``ok``, or why not —
        ``duplicate`` (already requested or present), ``quota``, ``blocklisted``, ``permission``,
        ``no_seasons`` (a show the app lists no seasons for yet) or ``upstream``. A show asks for its
        FIRST regular season only — the person can ask for more in the app — rather than every
        season, which is the run's choice for a title nobody is watching yet.
        """
        kind = _MEDIA_TYPE.get(media_type)
        if kind is None:
            return {"ok": False, "code": "upstream", "message": f"unsupported media type {media_type!r}"}
        body: dict[str, object] = {"mediaType": kind, "mediaId": int(tmdb_id)}
        if kind == "tv":
            season = self.first_regular_season(tmdb_id)
            if season is None:
                message = f"{self.app_name} lists no seasons for this show yet."
                return {"ok": False, "code": "no_seasons", "message": message}
            body["seasons"] = [season]
        status, payload = self._c._post_status("/request", body, as_user=user_id)
        message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
        if status in (200, 201):
            return {
                "ok": True,
                "code": "ok",
                "message": "",
                "request_id": _int_or_none(payload.get("id")) if isinstance(payload, dict) else None,
                "status": _REQUEST_STATUS.get(_int_or_none(payload.get("status")) or 0, "pending")
                if isinstance(payload, dict)
                else "pending",
            }
        if status == 409:
            return {"ok": False, "code": "duplicate", "message": "Already requested."}
        if status == 202:
            return {"ok": False, "code": "duplicate", "message": "That season is already requested or available."}
        if status == 403:
            lowered = message.lower()
            if "quota" in lowered:
                return {"ok": False, "code": "quota", "message": "Your request quota is used up for now."}
            if "blocklist" in lowered or "blacklist" in lowered:
                return {"ok": False, "code": "blocklisted", "message": f"This title is blocklisted in {self.app_name}."}
            return {"ok": False, "code": "permission", "message": "Your account is not allowed to request this."}
        fallback = f"{self.app_name} answered {status}"
        return {"ok": False, "code": "upstream", "message": http_retry.redact(message) or fallback}


def _approves(permissions: int, specific: int) -> bool:
    """Does this permission value auto-approve that media type?

    Four ways to hold it, and missing any one mislabels a real account: ADMIN (which Overseerr
    treats as holding every permission), MANAGE_REQUESTS (see `_PERM_MANAGE_REQUESTS` — it is in the
    approval rule despite not being named for it), the blanket AUTO_APPROVE, or the per-type bit.
    """
    return bool(permissions & (_PERM_ADMIN | _PERM_MANAGE_REQUESTS | _PERM_AUTO_APPROVE | specific))


def _name_of(row: dict) -> str:
    """What to call one account, most human first; "" when the row names it in no way at all.

    ``displayName`` is what the live API and the Overseerr UI use, and it is NOT in the published
    ``User`` schema — though `GET /user`'s own `sort` enum offers `displayname`, so the field is
    real. The rest of the chain is what that schema does document, so a fork serving only the
    documented fields still names every account.
    """
    for key in ("displayName", "username", "plexUsername", "email"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _media_type_of(row: dict) -> str | None:
    """Overseerr's ``mediaType`` → Shortlist's ``MediaType.value``; None when absent or unrecognised.

    Undocumented in the published ``MediaInfo`` schema but present on every live response. Treated as
    optional rather than assumed, because getting it wrong would cross a movie's tmdb id with a
    show's — TMDB's two id spaces overlap, so id 550 is both a film and a series.
    """
    raw = row.get("mediaType")
    if raw == "movie":
        return MediaType.MOVIE.value
    if raw == "tv":
        return MediaType.SHOW.value
    return None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
