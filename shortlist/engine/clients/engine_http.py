"""Client for an external recommendation engine speaking the ``/v1`` engine protocol.

The protocol is two JSON endpoints, deliberately small so any service can implement it:

``GET  {url}/v1/info``
    ``{"name": str, "version": str, "surfaces": ["library", ...], "serves_cold": bool, "ready": bool}``

``POST {url}/v1/recommend``
    Request (see ``recommend_payload``): who, their seeds and history as Shortlist sees them, the
    candidate universe (tmdb ids per media type), what to exclude, and how many to return per media
    type. Response: ``{"engine": {"name", "version"}, "ordered": bool, "items": [...], "trace": {...}}``
    and optionally ``"household": {"label", "kids_titles", "window_titles", "window_days"}`` — who
    watches under this account, over the engine's recent window. Each item is ``{"tmdb_id",
    "media_type", "title", "year", "genres", "rating", "vote_count",
    "poster_path", "overview", "language", "reason", "kids", "seed": {"tmdb_id", "title",
    "media_type"} | null, "sources": [...]}`` in the engine's final order, best first.

The person's viewing history leaves Shortlist in that request — to the one URL the owner configured
and nowhere else. Optional bearer token; never logged, never in an error (plex-safety rule 9).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import httpx

from shortlist.engine.clients import http_retry
from shortlist.engine.models import MediaType

if TYPE_CHECKING:
    from shortlist.engine.recommender import RecommendRequest

PROTOCOL_VERSION = 1


class EngineError(RuntimeError):
    """The engine call failed (connection, auth, or a malformed answer). Never carries the token."""


def recommend_payload(req: RecommendRequest, run_day: int) -> dict:
    """The JSON body for ``POST /v1/recommend`` — everything in the request that survives serialisation.

    ``library`` lists the tmdb ids the row may recommend from, per media type, so the engine can
    restrict itself to them (an engine that knows the library already may ignore it). The two dates
    are ISO strings; ``watched_at`` at the 1970 epoch means Plex had no date, and is sent as null.
    """
    return {
        "protocol": PROTOCOL_VERSION,
        "plex_account_id": req.user.plex_account_id,
        "surface": req.surface,
        "media": [m.value for m in _kinds(req.media)],
        "limit_per_media": req.limit_per_media,
        "run_day": date.fromordinal(run_day).isoformat() if run_day > 0 else date.today().isoformat(),
        "seeds": [
            {"tmdb_id": s.tmdb_id, "media_type": s.media_type.value, "title": s.title, "weight": s.weight}
            for s in req.seeds
        ],
        "history": [
            {
                "tmdb_id": w.tmdb_id,
                "media_type": w.media_type.value,
                "title": w.title,
                "year": w.year,
                "watched_at": _iso(w.watched_at),
                "watch_count": w.watch_count,
                "viewed_leaf_count": w.viewed_leaf_count,
                "leaf_count": w.leaf_count,
                "user_rating": w.user_rating if w.is_human_rating else None,
            }
            for w in req.history
        ],
        "library": {kind.value: sorted(req.library_index.get(kind, {})) for kind in _kinds(req.media)},
        # None (no exclusion rule) is sent as the seeds, which is what the built-in engine excludes then.
        "exclude": sorted(
            [tid, media.value]
            for tid, media in (
                req.watched_exclusions
                if req.watched_exclusions is not None
                else {(s.tmdb_id, s.media_type) for s in req.seeds}
            )
        ),
        "excluded_genres": sorted(req.excluded_genres),
        "seed_focus": req.seed_focus,
        # A seasonal row: the season's titles, per media type, so the engine can rank within them.
        "season": (
            {kind.value: sorted(ids) for kind, ids in req.season.ids.items()} if req.season is not None else None
        ),
    }


def _kinds(media: str) -> list[MediaType]:
    return [MediaType.MOVIE, MediaType.SHOW] if media == "both" else [MediaType(media)]


def _iso(when: datetime | None) -> str | None:
    if when is None or when.timestamp() <= 0:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).isoformat()


class EngineClient:
    def __init__(self, url: str, *, token: str = "", timeout: float = http_retry.DEFAULT_TIMEOUT_S):
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _check(self, r: httpx.Response, what: str) -> dict:
        if r.status_code in (401, 403):
            raise EngineError(f"the engine rejected the token (HTTP {r.status_code}) — check the engine token setting")
        if r.status_code >= 400:
            raise EngineError(f"the engine answered HTTP {r.status_code} to {what}")
        try:
            body = r.json()
        except ValueError as e:
            raise EngineError(f"the engine answered {what} with something other than JSON") from e
        if not isinstance(body, dict):
            raise EngineError(f"the engine answered {what} with a JSON {type(body).__name__}, not an object")
        return body

    def info(self) -> dict:
        """``GET /v1/info`` — the settings Test button, and ``serves_cold`` at run start."""
        try:
            r = http_retry.get(f"{self._url}/v1/info", headers=self._headers(), timeout=self._timeout)
        except httpx.HTTPError as e:
            raise EngineError(f"engine unreachable ({type(e).__name__})") from e
        body = self._check(r, "/v1/info")
        if not isinstance(body.get("name"), str):
            raise EngineError("the engine's /v1/info has no name — is this an engine?")
        return body

    def recommend(self, payload: dict) -> dict:
        """``POST /v1/recommend``; retried like a read (the request is idempotent)."""
        try:
            r = http_retry.idempotent_post(
                f"{self._url}/v1/recommend", json=payload, headers=self._headers(), timeout=self._timeout
            )
        except httpx.HTTPError as e:
            raise EngineError(f"engine unreachable ({type(e).__name__})") from e
        body = self._check(r, "/v1/recommend")
        if not isinstance(body.get("items"), list):
            raise EngineError("the engine's /v1/recommend answer has no items list")
        return body
