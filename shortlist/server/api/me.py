"""A person's own picks page: their request surface, and what they do with it.

Gated by `require_person` — a signed-in person, or the owner — and scoped to that account and
nothing else. Nothing here reaches another person's data, and nothing here is the owner's to reach
without being that person: the owner sees `/api/me` as themselves.

GET  /api/me                          who they are here, their Plex rows, the *seerr link and quota
GET  /api/me/suggestions?family=...   their deck: items, queued (already on the way), genres
GET  /api/me/dismissed                what they said never / later to
POST /api/me/act                      request | never | later | skip | undo — one title, as them
POST /api/me/seen                     impressions, batched, fire-and-forget from the page
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from shortlist.engine.clients.seerr import SeerrClient, SeerrError, SeerrPersonClient
from shortlist.engine.models import SeerrTarget
from shortlist.server.api.schemas import PassthroughModel, StrictRequestModel
from shortlist.server.auth import ROLE_OWNER, require_person
from shortlist.server.db.models import User
from shortlist.server.services import picks_service as picks
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/me", tags=["me"], dependencies=[Depends(require_person)])


def _seerr_cache(request: Request) -> picks.SeerrCache:
    state = request.app.state
    cache = getattr(state, "picks_seerr", None)
    if cache is None:

        def make_client() -> SeerrClient | None:
            with state.sessions() as session:
                store = SettingsStore(session, state.secrets)
                url = (store.get("seerr.url") or "").strip()
                api_key = store.get("seerr.apikey") or ""
            if not url or not api_key:
                return None
            return SeerrClient(SeerrTarget(url=url, api_key=api_key))

        cache = state.picks_seerr = picks.SeerrCache(make_client)
    return cache


def _limiter(request: Request) -> picks.RateLimiter:
    state = request.app.state
    limiter = getattr(state, "picks_limiter", None)
    if limiter is None:
        limiter = state.picks_limiter = picks.RateLimiter()
    return limiter


def _person(request: Request, session) -> User:
    """The `users` row behind the signed-in account. The owner has one too (user sync injects it);
    an owner without one has no picks, and says so rather than 500s."""
    identity = require_person(request)
    user = session.query(User).filter(User.plex_account_id == identity["account_id"]).first()
    if user is None:
        raise HTTPException(status_code=403, detail="this account has no place on the server yet")
    return user


class MeOut(PassthroughModel):
    state: str
    name: str
    account_id: int
    role: str
    # adult | family | kids as of the last run, or null. A family household gets the family lane.
    household: str | None = None
    built_at: str | None = None
    rows: list[dict]
    seerr: dict
    counts: dict


@router.get("", response_model=MeOut)
async def me(request: Request) -> dict:
    identity = require_person(request)

    def load() -> dict:
        with request.app.state.sessions() as session:
            user = _person(request, session)
            seerr = _seerr_cache(request).view(user.plex_account_id)
            view = picks.for_this_account(user, picks.build_view(session, user, seerr))
            session.commit()  # lapsed `later`s deleted on read
            return {
                "state": view["state"],
                "name": user.nickname or user.friendly_name or user.username,
                "account_id": user.plex_account_id,
                "role": identity.get("role") or ROLE_OWNER,
                "household": picks.household_label(user),
                "built_at": view["built_at"].isoformat() if view["built_at"] else None,
                "rows": view["rows"],
                "seerr": view["seerr"],
                "counts": {
                    "items": len(view["items"]),
                    "queued": len(view["queued"]),
                    "hidden_available": view["hidden_available"],
                    "family": sum(1 for i in view["items"] if i["kids"]),
                },
            }

    return await run_in_threadpool(load)


class MySuggestionsOut(PassthroughModel):
    state: str
    items: list[dict]
    queued: list[dict]
    hidden_available: int
    family: str
    has_family: bool
    household: str | None = None
    genres: list[dict]
    built_at: str | None = None
    seerr: dict


@router.get("/suggestions", response_model=MySuggestionsOut)
async def suggestions(request: Request, family: str = "auto") -> dict:
    if family not in picks.FAMILY_MODES:
        raise HTTPException(status_code=422, detail=f"family must be one of {list(picks.FAMILY_MODES)}")

    def load() -> dict:
        with request.app.state.sessions() as session:
            user = _person(request, session)
            seerr = _seerr_cache(request).view(user.plex_account_id)
            view = picks.for_this_account(user, picks.build_view(session, user, seerr))
            session.commit()
            lane = picks.family_lane(user, family)
            shown = picks.family_filter(view["items"], lane)
            return {
                "state": view["state"],
                "items": shown,
                "queued": view["queued"],
                "hidden_available": view["hidden_available"],
                "family": lane,
                # The family toggle is for a household that shares the account with children. For anyone
                # else children's titles are already in the list, so there is nothing to switch to.
                "has_family": picks.household_label(user) == "family" and any(i["kids"] for i in view["items"]),
                "household": picks.household_label(user),
                "genres": picks.genre_counts(shown),
                "built_at": view["built_at"].isoformat() if view["built_at"] else None,
                "seerr": view["seerr"],
            }

    return await run_in_threadpool(load)


class MyDismissedOut(PassthroughModel):
    never: list[dict]
    later: list[dict]


@router.get("/dismissed", response_model=MyDismissedOut)
async def dismissed(request: Request) -> dict:
    def load() -> dict:
        with request.app.state.sessions() as session:
            user = _person(request, session)
            active = picks._load_dismissals(session, user.id)
            session.commit()
            out: dict[str, list[dict]] = {"never": [], "later": []}
            for row in sorted(active.values(), key=lambda r: r.created_at, reverse=True):
                out[row.kind].append(
                    {
                        "tmdb_id": row.tmdb_id,
                        "media_type": row.media_type,
                        "title": row.title,
                        "year": row.year,
                        "until": row.until.isoformat() if row.until else None,
                        "created_at": row.created_at.isoformat(),
                    }
                )
            return out

    return await run_in_threadpool(load)


class PickActionIn(StrictRequestModel):
    action: Literal["request", "never", "later", "skip", "undo"]
    tmdb_id: int = 0
    media_type: Literal["movie", "show", ""] = ""
    surface: Literal["deck", "grid"] | None = None
    position: int | None = Field(default=None, ge=0)
    # undo only: take back the newest dismissal rather than a named title
    last: bool = False
    # undo only: what the page is reversing, for the event (a skip leaves no dismissal to find)
    undone: Literal["never", "later", "skip"] | None = None


class PickActionOut(PassthroughModel):
    ok: bool
    code: str | None = None
    message: str | None = None


@router.post("/act", response_model=PickActionOut)
async def act(body: PickActionIn, request: Request) -> dict:
    limiter = _limiter(request)

    def do() -> dict:
        with request.app.state.sessions() as session:
            user = _person(request, session)
            if limiter.limited(user.id, "all", picks.RATE_ACTIONS_PER_MIN):
                raise HTTPException(status_code=429, detail="Too many actions — try again in a minute.")
            if body.action == "undo":
                if body.last:
                    restored = picks.clear_dismissal(session, user.id, None, None, newest=True)
                    tmdb_id, media_type = (restored["tmdb_id"], restored["media_type"]) if restored else (0, "")
                else:
                    tmdb_id, media_type = body.tmdb_id, body.media_type
                    restored = picks.clear_dismissal(session, user.id, tmdb_id, media_type, newest=False)
                if tmdb_id and media_type:
                    undone = body.undone or (restored or {}).get("kind")
                    picks.record_event(
                        session,
                        user.id,
                        tmdb_id,
                        media_type,
                        "undo",
                        surface=body.surface,
                        position=body.position,
                        meta={"undone": undone} if undone else None,
                    )
                session.commit()
                return {"ok": True, "restored": restored}

            seerr = _seerr_cache(request).view(user.plex_account_id)
            view = picks.for_this_account(user, picks.build_view(session, user, seerr))
            item = next(
                (i for i in view["items"] if i["tmdb_id"] == body.tmdb_id and i["media_type"] == body.media_type), None
            )
            if item is None:
                # Never act on a title outside their own current set: a stolen session cannot request,
                # or dismiss, arbitrary titles, and a stale page cannot act on last night's list.
                session.commit()
                raise HTTPException(status_code=403, detail="That title is not in your current suggestions.")
            source = item.get("source") or ""
            if body.action == "skip":
                # "Seen, not now": the title stays fully eligible. No dismissal — only the event, which an
                # engine reads as a weak negative, distinct from never (hard) and later (soft).
                picks.record_event(
                    session,
                    user.id,
                    item["tmdb_id"],
                    item["media_type"],
                    "skip",
                    surface=body.surface,
                    position=body.position,
                    source=source,
                )
                session.commit()
                return {"ok": True, "skipped": True}
            if body.action in ("never", "later"):
                until = picks.set_dismissal(session, user.id, item, body.action)
                picks.record_event(
                    session,
                    user.id,
                    item["tmdb_id"],
                    item["media_type"],
                    body.action,
                    surface=body.surface,
                    position=body.position,
                    source=source,
                )
                session.commit()
                return {"ok": True, "dismissed": {"kind": body.action, "until": until.isoformat() if until else None}}
            # request
            if limiter.limited(user.id, "request", picks.RATE_REQUESTS_PER_MIN):
                raise HTTPException(status_code=429, detail="Too many requests — try again in a minute.")
            if not item["requestable"]:
                session.commit()
                return {
                    "ok": False,
                    "code": item["reason_not_requestable"] or "not_requestable",
                    "message": "This title cannot be requested right now.",
                }
            client = _seerr_cache(request)._make_client()
            if client is None or seerr.user_id is None:
                session.commit()
                return {"ok": False, "code": "no_seerr_user", "message": "No request account is linked to you."}
            try:
                person = SeerrPersonClient(client)
                outcome = person.request_as(seerr.user_id, item["tmdb_id"], picks.media_type_of(item))
            except SeerrError as e:
                logger.warning("picks: request as {} failed ({})", user.username, e)
                session.commit()
                return {"ok": False, "code": "upstream", "message": f"{client.app_name} is not answering right now."}
            _seerr_cache(request).invalidate_user(seerr.user_id)
            if not outcome["ok"]:
                session.commit()
                return {"ok": False, "code": outcome["code"], "message": outcome["message"]}
            picks.log_request(session, user.id, item, seerr.user_id, outcome)
            picks.record_event(
                session,
                user.id,
                item["tmdb_id"],
                item["media_type"],
                "request",
                surface=body.surface,
                position=body.position,
                source=source,
                meta={"seerr_request_id": outcome.get("request_id"), "status": outcome.get("status")},
            )
            session.commit()
            return {"ok": True, "status": outcome.get("status"), "request_id": outcome.get("request_id")}

    return await run_in_threadpool(do)


class PickSeenItemIn(StrictRequestModel):
    tmdb_id: int
    media_type: Literal["movie", "show"]
    surface: Literal["deck", "grid"]
    position: int | None = Field(default=None, ge=0)


class PickSeenIn(StrictRequestModel):
    items: list[PickSeenItemIn] = Field(max_length=picks.SEEN_BATCH_MAX)


class PickSeenOut(PassthroughModel):
    ok: bool
    logged: int


@router.post("/seen", response_model=PickSeenOut)
async def seen(body: PickSeenIn, request: Request) -> dict:
    limiter = _limiter(request)

    def do() -> dict:
        with request.app.state.sessions() as session:
            user = _person(request, session)
            if limiter.limited(user.id, "seen", picks.RATE_SEEN_PER_MIN):
                raise HTTPException(status_code=429, detail="slow down")
            seerr = _seerr_cache(request).view(user.plex_account_id)
            view = picks.for_this_account(user, picks.build_view(session, user, seerr))
            mine = {(i["tmdb_id"], i["media_type"]): i.get("source") or "" for i in view["items"]}
            rows = [
                (it.tmdb_id, it.media_type, it.surface, it.position, mine[(it.tmdb_id, it.media_type)])
                for it in body.items
                if (it.tmdb_id, it.media_type) in mine  # only their own current set; the rest is dropped
            ]
            logged = picks.record_shown(session, user.id, rows) if rows else 0
            session.commit()
            return {"ok": True, "logged": logged}

    return await run_in_threadpool(do)
