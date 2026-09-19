"""A person's picks: their request surface as the page sees it, and what they do with it.

Everything here is scoped to ONE person — the `users` row behind the signed-in account — and reads
nothing another person owns. The list itself is `user_suggestions`, written by the run; this joins
it with what the person has already said (`dismissals`) and what the *seerr says about each title
(available, on the way, theirs already), decides what can be requested, and records every action
and impression in `pick_events` for an engine to learn from.

The *seerr side is cached per app for a short while: its media walk is thousands of rows, and a
page load must not repeat it. A request (`act`) re-checks the one title it is about.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.clients.seerr import SeerrClient, SeerrError, SeerrPersonClient
from shortlist.engine.models import MediaType
from shortlist.server.db.models import Delivery, Dismissal, PickEvent, RequestLog, Server, User, UserSuggestion

ACTIONS = ("request", "never", "later", "skip", "undo")
SURFACES = ("deck", "grid")
FAMILY_MODES = ("exclude", "only", "include")
SNOOZE_DAYS = 30
SEEN_BATCH_MAX = 200
#: Per person, per minute. Generous for a human, tight for a script.
RATE_ACTIONS_PER_MIN = 60
RATE_REQUESTS_PER_MIN = 20
RATE_SEEN_PER_MIN = 120
SEERR_TTL_S = 300
SEERR_USER_TTL_S = 60

_MEDIA_TYPE = {"movie": MediaType.MOVIE, "show": MediaType.SHOW}


@dataclass
class SeerrView:
    """What the *seerr knows that the page needs, for one person — or the reason it could not say."""

    linked: bool = False
    user_id: int | None = None
    quota: dict = field(default_factory=dict)
    #: (media_type, tmdb_id) -> this person's own request {status, media_status, request_id}
    mine: dict[tuple[str, int], dict] = field(default_factory=dict)
    #: (media_type, tmdb_id) -> the app's media status word for every title it knows
    media: dict[tuple[str, int], str] = field(default_factory=dict)
    error: str | None = None


def empty_quota() -> dict:
    return {
        kind: {"limit": 0, "used": 0, "days": None, "remaining": None, "restricted": False} for kind in ("movie", "tv")
    }


class SeerrCache:
    """The *seerr reads a page needs, cached per app: the media walk for everyone (`SEERR_TTL_S`),
    each person's own requests and quota for a minute. `client` is None when no *seerr is configured."""

    def __init__(self, make_client):
        self._make_client = make_client
        self._lock = threading.Lock()
        self._media: tuple[float, dict] | None = None
        self._users: tuple[float, list[dict]] | None = None
        self._per_user: dict[int, tuple[float, dict, dict]] = {}

    def invalidate_user(self, user_id: int) -> None:
        with self._lock:
            self._per_user.pop(user_id, None)

    def view(self, plex_account_id: int) -> SeerrView:
        client: SeerrClient | None = self._make_client()
        if client is None:
            return SeerrView(error="no_seerr", quota=empty_quota())
        person = SeerrPersonClient(client)
        now = time.monotonic()
        view = SeerrView(quota=empty_quota())
        try:
            with self._lock:
                media = self._media if self._media and now - self._media[0] < SEERR_TTL_S else None
                users = self._users if self._users and now - self._users[0] < SEERR_TTL_S else None
            if media is None:
                media = (now, client.media_state())
                with self._lock:
                    self._media = media
            if users is None:
                users = (now, client.users())
                with self._lock:
                    self._users = users
            view.media = media[1]
            account = next((u for u in users[1] if u.get("plex_id") == plex_account_id), None)
            if account is None:
                return view
            view.linked, view.user_id = True, int(account["id"])
            with self._lock:
                cached = self._per_user.get(view.user_id)
            if cached is None or now - cached[0] >= SEERR_USER_TTL_S:
                cached = (now, person.quota(view.user_id), person.requests_by(view.user_id))
                with self._lock:
                    self._per_user[view.user_id] = cached
            view.quota, view.mine = cached[1], cached[2]
        except SeerrError as e:
            view.error = str(e)
            logger.warning("picks: the *seerr could not be read ({})", e)
        return view


class RateLimiter:
    """A sliding minute per (person, bucket); in memory, like the login limiter."""

    def __init__(self) -> None:
        self._hits: dict[tuple[int, str], deque[float]] = {}
        self._lock = threading.Lock()

    def limited(self, user_id: int, bucket: str, per_minute: int) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault((user_id, bucket), deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= per_minute:
                return True
            hits.append(now)
            return False


def _load_dismissals(session: Session, user_id: int) -> dict[tuple[str, int], Dismissal]:
    """This person's standing answers, minus any `later` that has lapsed (deleted as seen)."""
    now = datetime.now(UTC)
    rows = session.scalars(select(Dismissal).where(Dismissal.user_id == user_id)).all()
    active: dict[tuple[str, int], Dismissal] = {}
    for row in rows:
        until = row.until.replace(tzinfo=row.until.tzinfo or UTC) if row.until else None
        if row.kind == "later" and until is not None and until <= now:
            session.delete(row)
            continue
        active[(row.media_type, row.tmdb_id)] = row
    return active


def _item(row: UserSuggestion) -> dict:
    return {
        "tmdb_id": row.tmdb_id,
        "media_type": row.media_type,
        "title": row.title,
        "year": row.year,
        "rating": row.rating,
        "vote_count": row.vote_count,
        "overview": row.overview,
        "language": row.language,
        "poster_path": row.poster_path or None,
        "genres": list(row.genres or []),
        "reason": row.reason,
        "seed_title": row.seed_title,
        "kids": bool(row.kids),
        "rank": row.rank,
        "source": (row.sources or "").split(",")[0] if row.sources else "",
    }


def _place(item: dict, seerr: SeerrView) -> str:
    """Decide the *seerr side of one item in place: `requestable`, `reason_not_requestable`, `seerr`.
    Returns where it goes — `items`, `queued` (someone is already getting it) or `hidden` (there)."""
    key = (item["media_type"], item["tmdb_id"])
    media_status = seerr.media.get(key)
    if media_status == "downloaded":
        return "hidden"
    mine = seerr.mine.get(key)
    state = None
    if mine:
        state = {"declined": "declined", "approved": "approved"}.get(mine["status"], "requested")
    elif media_status in ("awaiting_approval", "queued"):
        state = "on_the_way"
    reason = None
    if state in ("requested", "approved"):
        reason = "requested"
    elif state == "on_the_way":
        reason = "on_the_way"
    elif not seerr.linked:
        reason = "seerr_down" if seerr.error and seerr.error != "no_seerr" else "no_seerr_user"
    elif seerr.quota.get("tv" if item["media_type"] == "show" else "movie", {}).get("restricted"):
        reason = "quota"
    item["seerr"] = {"status": state, "request_id": mine.get("request_id") if mine else None}
    item["requestable"] = reason is None
    item["reason_not_requestable"] = reason
    return "queued" if reason in ("requested", "on_the_way") else "items"


def build_view(session: Session, user: User, seerr: SeerrView) -> dict:
    """Everything the page needs for one person, apart from the family filter (applied by the caller)."""
    dismissed = _load_dismissals(session, user.id)
    rows = session.scalars(
        select(UserSuggestion).where(UserSuggestion.user_id == user.id).order_by(UserSuggestion.rank)
    ).all()
    items: list[dict] = []
    queued: list[dict] = []
    hidden = 0
    for row in rows:
        if (row.media_type, row.tmdb_id) in dismissed:
            continue
        item = _item(row)
        where = _place(item, seerr)
        if where == "hidden":
            hidden += 1
        elif where == "queued":
            queued.append(item)
        else:
            items.append(item)
    server = session.query(Server).first()
    plex_rows = []
    for d in session.scalars(select(Delivery).where(Delivery.user_slug == user.slug)).all():
        plex_rows.append(
            {
                "title": d.title,
                "library_key": d.library_key,
                "url": (
                    f"https://app.plex.tv/desktop/#!/server/{server.machine_id}/details?key=%2Flibrary%2Fcollections%2F{d.rating_key}"
                    if server
                    else None
                ),
            }
        )
    return {
        "state": "ok" if rows else "no_picks",
        "built_at": rows[0].built_at if rows else None,
        "items": items,
        "queued": queued,
        "hidden_available": hidden,
        "rows": plex_rows,
        "seerr": {
            "linked": seerr.linked,
            "user_id": seerr.user_id,
            "quota": seerr.quota,
            "error": None if seerr.error in (None, "no_seerr") else "unreachable",
            "configured": seerr.error != "no_seerr",
        },
    }


def family_filter(items: list[dict], family: str) -> list[dict]:
    if family == "exclude":
        return [i for i in items if not i["kids"]]
    if family == "only":
        return [i for i in items if i["kids"]]
    return list(items)


def genre_counts(items: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for item in items:
        for g in item.get("genres") or []:
            counts[g] = counts.get(g, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def record_event(
    session: Session,
    user_id: int,
    tmdb_id: int,
    media_type: str,
    event: str,
    *,
    surface: str | None = None,
    position: int | None = None,
    source: str = "",
    meta: dict | None = None,
) -> None:
    session.add(
        PickEvent(
            user_id=user_id,
            tmdb_id=tmdb_id,
            media_type=media_type,
            event=event,
            surface=surface if surface in SURFACES else None,
            position=position,
            source=source,
            meta=meta,
        )
    )


def record_shown(session: Session, user_id: int, rows: list[tuple[int, str, str, int | None, str]]) -> int:
    """Impressions, one per person, title, surface and UTC day. Returns how many were new."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    have = {
        (e.tmdb_id, e.media_type, e.surface)
        for e in session.scalars(
            select(PickEvent).where(
                PickEvent.user_id == user_id, PickEvent.event == "shown", PickEvent.shown_day == today
            )
        )
    }
    logged = 0
    for tmdb_id, media_type, surface, position, source in rows:
        if (tmdb_id, media_type, surface) in have:
            continue
        have.add((tmdb_id, media_type, surface))
        session.add(
            PickEvent(
                user_id=user_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                event="shown",
                surface=surface,
                position=position,
                source=source,
                shown_day=today,
            )
        )
        logged += 1
    return logged


def set_dismissal(session: Session, user_id: int, item: dict, kind: str) -> datetime | None:
    until = datetime.now(UTC) + timedelta(days=SNOOZE_DAYS) if kind == "later" else None
    row = session.scalar(
        select(Dismissal).where(
            Dismissal.user_id == user_id,
            Dismissal.tmdb_id == item["tmdb_id"],
            Dismissal.media_type == item["media_type"],
        )
    )
    if row is None:
        row = Dismissal(user_id=user_id, tmdb_id=item["tmdb_id"], media_type=item["media_type"])
        session.add(row)
    row.kind, row.until, row.title, row.year = kind, until, item.get("title") or "", item.get("year")
    row.created_at = datetime.now(UTC)
    return until


def clear_dismissal(
    session: Session, user_id: int, tmdb_id: int | None, media_type: str | None, *, newest: bool
) -> dict | None:
    query = select(Dismissal).where(Dismissal.user_id == user_id)
    if newest:
        row = session.scalars(query.order_by(Dismissal.created_at.desc())).first()
    else:
        row = session.scalar(query.where(Dismissal.tmdb_id == tmdb_id, Dismissal.media_type == media_type))
    if row is None:
        return None
    restored = {"tmdb_id": row.tmdb_id, "media_type": row.media_type, "kind": row.kind, "title": row.title}
    session.delete(row)
    return restored


def log_request(session: Session, user_id: int, item: dict, seerr_user_id: int, outcome: dict) -> None:
    session.add(
        RequestLog(
            user_id=user_id,
            tmdb_id=item["tmdb_id"],
            media_type=item["media_type"],
            seerr_user_id=seerr_user_id,
            seerr_request_id=outcome.get("request_id"),
            seerr_status=outcome.get("status"),
        )
    )


def media_type_of(item: dict) -> MediaType:
    return _MEDIA_TYPE[item["media_type"]]
