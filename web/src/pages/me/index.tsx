import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HelpCircle, LayoutGrid, LogOut, Undo2 } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorMessage } from "@/lib/api";
import { useSession } from "@/lib/queries";
import type { DismissedItem, FamilyLane, PickItem } from "@/lib/types";
import { cn } from "@/lib/utils";

import "./me.css";

/*
 * A person's own picks: the titles their watch history points at that the library does not have.
 * Two layouts over ONE filtered list — a poster grid (wide screens) and a swipe deck (phones):
 * right requests, left hides for good, up snoozes for 30 days, down skips to the back of the deck.
 * Ported from the stdlib `picks` app it replaces; the gestures, the reasons and the impression
 * logging are the same so the events recorded stay comparable.
 */

type Action = "request" | "never" | "later" | "skip";
type Layout = "auto" | "grid" | "deck";
type Sort = "rank" | "rating" | "votes" | "year" | "title";
type MediaFilter = "all" | "movie" | "show";

const SNOOZE_DAYS = 30;
const REASONS: Record<string, string> = {
  requested: "you requested this",
  on_the_way: "already requested by someone",
  quota: "request quota used up",
  no_seerr_user: "no request account is linked to you",
  seerr_down: "the request service is not answering",
};

const keyOf = (i: PickItem) => `${i.tmdb_id}:${i.media_type}`;

function store(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode */
  }
}
function load(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function metaLine(item: PickItem): string {
  const parts = [item.media_type === "show" ? "TV show" : "Movie"];
  if (item.rating)
    parts.push(
      `★ ${Number(item.rating).toFixed(1)}` +
        (item.vote_count ? ` (${item.vote_count.toLocaleString()})` : ""),
    );
  if (item.language && item.language !== "en")
    parts.push(item.language.toUpperCase());
  if (item.media_type === "show") parts.push("season 1 first");
  return parts.join(" · ");
}

function posterUrl(item: PickItem): string | null {
  return item.poster_path && /^\/[\w.-]+\.(jpg|png)$/i.test(item.poster_path)
    ? `https://image.tmdb.org/t/p/w342${item.poster_path}`
    : null;
}

/** Ranks a person's titles hold in the list AS SERVED under the current filter and sort, whatever
 *  has been swiped away since — what an engine needs to know "how far down was this when they saw it". */
function sortItems(items: PickItem[], sort: Sort): PickItem[] {
  const by: Record<Sort, (a: PickItem, b: PickItem) => number> = {
    rank: (a, b) => (a.rank ?? 1e9) - (b.rank ?? 1e9),
    rating: (a, b) => (b.rating || 0) - (a.rating || 0),
    votes: (a, b) => (b.vote_count || 0) - (a.vote_count || 0),
    year: (a, b) => (b.year || 0) - (a.year || 0),
    title: (a, b) => (a.title || "").localeCompare(b.title || ""),
  };
  return [...items].sort(by[sort]);
}

function Card({
  item,
  compact,
  withButtons,
  onAct,
  onOpen,
  className,
  cardRef,
  observe,
}: {
  item: PickItem;
  compact?: boolean;
  withButtons?: boolean;
  onAct?: (item: PickItem, action: Action) => void;
  onOpen?: (item: PickItem) => void;
  className?: string;
  cardRef?: (el: HTMLElement | null) => void;
  observe?: (el: HTMLElement | null, item: PickItem) => void;
}) {
  const poster = posterUrl(item);
  return (
    <article
      ref={(el) => {
        cardRef?.(el);
        observe?.(el, item);
      }}
      className={cn("picks-card", className)}
      data-key={keyOf(item)}
    >
      {poster ? (
        <img
          className="picks-poster"
          src={poster}
          alt=""
          loading="lazy"
          draggable={false}
        />
      ) : (
        <div className="picks-noposter">no poster</div>
      )}
      <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-hidden px-2.5 py-2">
        <div className="text-sm font-bold leading-tight">
          {item.title}
          {item.year ? ` (${item.year})` : ""}
        </div>
        <div className="text-xs text-muted-foreground">{metaLine(item)}</div>
        {!compact && (
          <>
            <div className="flex flex-wrap gap-1">
              {item.genres.slice(0, 3).map((g) => (
                <span
                  key={g}
                  className="rounded-full bg-amber-950/60 px-2 py-0.5 text-[0.7rem] text-amber-200"
                >
                  {g}
                </span>
              ))}
              {item.reason && (
                <span className="rounded-full bg-muted px-2 py-0.5 text-[0.7rem] text-muted-foreground">
                  {item.reason}
                </span>
              )}
            </div>
            {item.overview && (
              <p
                className="picks-overview m-0 text-[0.82rem] text-muted-foreground"
                title="tap to read"
                onClick={() => onOpen?.(item)}
              >
                {item.overview}
              </p>
            )}
          </>
        )}
        {!item.requestable && (
          <div className="text-xs text-amber-300">
            {REASONS[item.reason_not_requestable ?? ""] ?? "not requestable"}
          </div>
        )}
        {withButtons && onAct && (
          <div className="mt-auto flex gap-1">
            <Button
              size="sm"
              variant="outline"
              className="flex-1 border-red-400/60 text-red-300"
              onClick={() => onAct(item, "never")}
              aria-label={`Not for me: ${item.title}`}
            >
              ✕
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="flex-1 border-sky-400/60 text-sky-200"
              onClick={() => onAct(item, "later")}
              aria-label={`Later: ${item.title}`}
            >
              ⏰
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="flex-1 border-emerald-400/60 font-semibold text-emerald-200"
              disabled={!item.requestable}
              title={REASONS[item.reason_not_requestable ?? ""] ?? ""}
              onClick={() => onAct(item, "request")}
              aria-label={`Request: ${item.title}`}
            >
              ♥ Request
            </Button>
          </div>
        )}
      </div>
      {(["never", "request", "later", "skip"] as const).map((k) => (
        <div key={k} className={`picks-label ${k}`} data-label={k}>
          {k === "never" ? "nope" : k}
        </div>
      ))}
    </article>
  );
}

/** The top card of the deck: pointer drags paint the labels, and a far or fast enough release acts. */
function DeckCard({
  item,
  busy,
  onAct,
  onOpen,
}: {
  item: PickItem;
  busy: boolean;
  onAct: (item: PickItem, action: Action, el: HTMLElement | null) => void;
  onOpen: (item: PickItem) => void;
}) {
  const ref = useRef<HTMLElement | null>(null);
  const drag = useRef({ sx: 0, sy: 0, dx: 0, dy: 0, t0: 0, on: false, moved: false });

  const paint = () => {
    const el = ref.current;
    if (!el) return;
    const { dx, dy } = drag.current;
    const w = el.offsetWidth || 300;
    const h = el.offsetHeight || 500;
    el.style.transform = `translate(${dx}px,${dy}px) rotate(${dx / 20}deg)`;
    const vertical = Math.abs(dy) > Math.abs(dx);
    const set = (name: string, value: number) => {
      const label = el.querySelector<HTMLElement>(`[data-label="${name}"]`);
      if (label) label.style.opacity = String(Math.max(0, Math.min(1, value)));
    };
    set("never", !vertical && dx < 0 ? -dx / (w * 0.35) : 0);
    set("request", !vertical && dx > 0 ? dx / (w * 0.35) : 0);
    set("later", vertical && dy < 0 ? -dy / (h * 0.25) : 0);
    set("skip", vertical && dy > 0 ? dy / (h * 0.25) : 0);
  };

  const onDown = (e: ReactPointerEvent<HTMLElement>) => {
    if (busy || e.button) return;
    drag.current = { sx: e.clientX, sy: e.clientY, dx: 0, dy: 0, t0: performance.now(), on: true, moved: false };
    ref.current?.classList.remove("snap");
    ref.current?.setPointerCapture(e.pointerId);
  };
  const onMove = (e: ReactPointerEvent<HTMLElement>) => {
    const d = drag.current;
    if (!d.on) return;
    d.dx = e.clientX - d.sx;
    d.dy = e.clientY - d.sy;
    if (Math.abs(d.dx) + Math.abs(d.dy) > 6) d.moved = true;
    paint();
  };
  const onUp = (e: ReactPointerEvent<HTMLElement>) => {
    const d = drag.current;
    if (!d.on) return;
    d.on = false;
    const el = ref.current;
    const w = el?.offsetWidth || 300;
    const h = el?.offsetHeight || 500;
    const dt = Math.max(1, performance.now() - d.t0);
    const vx = Math.abs(d.dx) / dt;
    const vy = Math.abs(d.dy) / dt;
    const vertical = Math.abs(d.dx) < Math.abs(d.dy);
    const far = Math.abs(d.dy) > h * 0.25 || vy > 0.6;
    let action: Action | null = null;
    if (vertical && far) action = d.dy < 0 ? "later" : "skip";
    else if (!vertical && (Math.abs(d.dx) > w * 0.35 || vx > 0.6))
      action = d.dx > 0 ? "request" : "never";
    if (action === "request" && !item.requestable) {
      toast(REASONS[item.reason_not_requestable ?? ""] ?? "not requestable");
      action = null;
    }
    if (action) {
      onAct(item, action, el);
    } else {
      el?.classList.add("snap");
      d.dx = d.dy = 0;
      paint();
      const target = e.target as HTMLElement;
      if (!d.moved && target?.classList?.contains("picks-overview")) onOpen(item);
    }
  };

  return (
    <div
      className="contents"
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
    >
      <Card item={item} cardRef={(el) => (ref.current = el)} className={busy ? "opacity-60" : ""} />
    </div>
  );
}

export function flyOut(el: HTMLElement | null, action: Action) {
  if (!el) return;
  el.classList.add("gone");
  el.style.transform =
    action === "later"
      ? "translateY(-120vh)"
      : action === "skip"
        ? "translateY(120vh)"
        : `translate(${action === "request" ? "" : "-"}120vw,0) rotate(${action === "request" ? 20 : -20}deg)`;
}

export default function MePage() {
  const session = useSession();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // "auto" = what their household calls for: a family sharing the account sees children's titles in
  // their own lane (the Family toggle), anyone else sees them mixed in.
  const [family, setFamily] = useState<FamilyLane>("auto");
  const [layout, setLayout] = useState<Layout>(() => (load("picks.mode") as Layout) || "auto");
  const [type, setType] = useState<MediaFilter>(() => (load("picks.type") as MediaFilter) || "all");
  const [genre, setGenre] = useState<string>(() => load("picks.genre") || "");
  const [sort, setSort] = useState<Sort>(() => (load("picks.sort") as Sort) || "rank");
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 767px)").matches);
  const [gone, setGone] = useState<Set<string>>(() => new Set());
  const [skipped, setSkipped] = useState<Set<string>>(() => new Set());
  const [history, setHistory] = useState<{ item: PickItem; action: Action }[]>([]);
  const [queuedExtra, setQueuedExtra] = useState<PickItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [sheet, setSheet] = useState<PickItem | null>(null);
  const [help, setHelp] = useState(false);
  // The gestures explained once, on a phone, the first time there is a card to swipe.
  const [helpSeen, setHelpSeen] = useState(() => !!load("picks.helpSeen"));
  const [hidden, setHidden] = useState(false);

  const me = useQuery({ queryKey: ["me"], queryFn: api.getMe });
  const suggestions = useQuery({
    queryKey: ["me", "suggestions", family],
    queryFn: () => api.getMySuggestions(family),
  });
  const dismissed = useQuery({
    queryKey: ["me", "dismissed"],
    queryFn: api.getMyDismissed,
    enabled: hidden,
  });
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      queryClient.clear();
      navigate("/login");
    },
  });

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    const onChange = () => setNarrow(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const mode: "grid" | "deck" = layout === "auto" ? (narrow ? "deck" : "grid") : layout;
  useEffect(() => {
    document.documentElement.classList.toggle("picks-deck", mode === "deck");
    return () => document.documentElement.classList.remove("picks-deck");
  }, [mode]);

  // The list as served, reset whenever the server answers anew; positions are ranks in it.
  const served = useMemo(
    () => (suggestions.data?.items ?? []) as unknown as PickItem[],
    [suggestions.data],
  );
  // A fresh answer from the server starts a fresh session: nothing swiped, nothing shown yet.
  // Reset during render (React's documented pattern for state derived from a changed input); the
  // impressions-sent set is re-keyed to the served list where it is used, outside render.
  const seenSent = useRef<{ list: PickItem[]; set: Set<string> }>({ list: served, set: new Set() });
  const [servedSeen, setServedSeen] = useState(served);
  if (served !== servedSeen) {
    setServedSeen(served);
    setGone(new Set());
    setSkipped(new Set());
  }

  const pass = useCallback(
    (i: PickItem) =>
      (type === "all" || i.media_type === type) &&
      (!genre || i.genres.includes(genre)),
    [type, genre],
  );
  const ranks = useMemo(
    () => new Map(sortItems(served.filter(pass), sort).map((i, n) => [keyOf(i), n])),
    [served, pass, sort],
  );
  const items = useMemo(() => {
    const list = sortItems(served.filter(pass).filter((i) => !gone.has(keyOf(i))), sort);
    return [...list.filter((i) => !skipped.has(keyOf(i))), ...list.filter((i) => skipped.has(keyOf(i)))];
  }, [served, pass, sort, gone, skipped]);
  const queued = useMemo(
    () =>
      [...queuedExtra, ...((suggestions.data?.queued ?? []) as unknown as PickItem[])].filter(
        (i) => type === "all" || i.media_type === type,
      ),
    [queuedExtra, suggestions.data, type],
  );
  const genres = (suggestions.data?.genres ?? []) as { name: string; count: number }[];

  // Impressions: the top card, or a grid tile half in view. Batched, fire-and-forget.
  const seenQueue = useRef<{ tmdb_id: number; media_type: "movie" | "show"; surface: "deck" | "grid"; position: number | null }[]>([]);
  const seenTimer = useRef<number | undefined>(undefined);
  const flushSeen = useCallback(() => {
    window.clearTimeout(seenTimer.current);
    if (!seenQueue.current.length) return;
    const batch = seenQueue.current.splice(0, 200);
    api.seen({ items: batch }).catch(() => {});
  }, []);
  const seen = useCallback(
    (item: PickItem, surface: "deck" | "grid") => {
      if (seenSent.current.list !== served) seenSent.current = { list: served, set: new Set() };
      const k = `${keyOf(item)}@${surface}`;
      if (seenSent.current.set.has(k)) return;
      seenSent.current.set.add(k);
      seenQueue.current.push({
        tmdb_id: item.tmdb_id,
        media_type: item.media_type,
        surface,
        position: ranks.get(keyOf(item)) ?? null,
      });
      window.clearTimeout(seenTimer.current);
      seenTimer.current = window.setTimeout(flushSeen, 1500);
    },
    [ranks, flushSeen, served],
  );
  useEffect(() => {
    const onHide = () => flushSeen();
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("pagehide", onHide);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("pagehide", onHide);
      flushSeen();
    };
  }, [flushSeen]);
  // Grid tiles count as shown once half of one is in the viewport. The observer is made on first
  // use, outside render; `seenRef` keeps it pointed at the current `seen` without rebuilding it.
  const seenRef = useRef(seen);
  useEffect(() => {
    seenRef.current = seen;
  }, [seen]);
  const watcher = useRef<IntersectionObserver | null>(null);
  const observe = useCallback((el: HTMLElement | null, item: PickItem) => {
    if (!el || typeof IntersectionObserver === "undefined") return;
    if (!watcher.current) {
      watcher.current = new IntersectionObserver(
        (entries, observer) => {
          entries.forEach((en) => {
            if (!en.isIntersecting) return;
            observer.unobserve(en.target);
            const it = (en.target as HTMLElement & { _item?: PickItem })._item;
            if (it) seenRef.current(it, "grid");
          });
        },
        { threshold: 0.5 },
      );
    }
    (el as HTMLElement & { _item?: PickItem })._item = item;
    watcher.current.observe(el);
  }, []);
  const top = mode === "deck" ? items[0] : undefined;
  useEffect(() => {
    if (top) seen(top, "deck");
  }, [top, seen]);

  const act = useCallback(
    async (item: PickItem, action: Action, el: HTMLElement | null = null) => {
      if (busy) return;
      setBusy(true);
      const where = { surface: mode, position: ranks.get(keyOf(item)) ?? null };
      const key = keyOf(item);
      try {
        if (action === "skip") {
          flyOut(el, action);
          setSkipped((s) => new Set(s).add(key));
          setHistory((h) => [...h, { item, action }]);
          const r = await api.act({ action, tmdb_id: item.tmdb_id, media_type: item.media_type, ...where });
          if (!r.ok) throw new Error(r.message ?? "could not save that");
          toast("Skipped — back of the deck");
        } else if (action !== "request") {
          flyOut(el, action);
          setGone((g) => new Set(g).add(key));
          setHistory((h) => [...h, { item, action }]);
          const r = await api.act({ action, tmdb_id: item.tmdb_id, media_type: item.media_type, ...where });
          if (!r.ok) throw new Error(r.message ?? "could not save that");
          toast(action === "never" ? "Hidden — not for you" : `Snoozed for ${SNOOZE_DAYS} days`);
        } else {
          const r = await api.act({ action: "request", tmdb_id: item.tmdb_id, media_type: item.media_type, ...where });
          if (r.ok) {
            flyOut(el, action);
            setGone((g) => new Set(g).add(key));
            setQueuedExtra((q) => [{ ...item, requestable: false, reason_not_requestable: "requested" }, ...q]);
            setHistory((h) => [...h, { item, action }]);
            void queryClient.invalidateQueries({ queryKey: ["me"], exact: true });
            toast(r.status === "approved" ? "Requested and approved — on its way" : "Requested — waiting for approval");
          } else if (r.code === "duplicate") {
            flyOut(el, action);
            setGone((g) => new Set(g).add(key));
            setQueuedExtra((q) => [{ ...item, requestable: false, reason_not_requestable: "on_the_way" }, ...q]);
            toast(r.message ?? "already requested");
          } else {
            el?.classList.remove("gone");
            if (el) el.style.transform = "";
            toast(r.message ?? "request failed");
          }
        }
      } catch (e) {
        // Put it back: the server did not record it.
        setGone((g) => {
          const n = new Set(g);
          n.delete(key);
          return n;
        });
        setSkipped((s) => {
          const n = new Set(s);
          n.delete(key);
          return n;
        });
        setHistory((h) => (h.at(-1)?.item === item ? h.slice(0, -1) : h));
        toast(apiErrorMessage(e, "network error"));
      } finally {
        setBusy(false);
      }
    },
    [busy, mode, ranks, queryClient],
  );

  const undo = useCallback(async () => {
    const last = history.at(-1);
    if (!last) return;
    setHistory((h) => h.slice(0, -1));
    if (last.action === "request") {
      toast("Requests are taken back in the request app");
      return;
    }
    try {
      const r = await api.act({
        action: "undo",
        tmdb_id: last.item.tmdb_id,
        media_type: last.item.media_type,
        undone: last.action,
        surface: mode,
        position: ranks.get(keyOf(last.item)) ?? null,
      });
      if (!r.ok) throw new Error(r.message ?? "could not undo");
      const key = keyOf(last.item);
      if (last.action === "skip")
        setSkipped((s) => {
          const n = new Set(s);
          n.delete(key);
          return n;
        });
      else
        setGone((g) => {
          const n = new Set(g);
          n.delete(key);
          return n;
        });
      toast("Restored");
    } catch (e) {
      toast(apiErrorMessage(e, "could not undo"));
    }
  }, [history, mode, ranks]);

  const helpOpen = help || (!helpSeen && mode === "deck" && items.length > 0);
  const closeHelp = () => {
    setHelp(false);
    setHelpSeen(true);
    store("picks.helpSeen", "1");
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setHelp(false);
        setSheet(null);
        return;
      }
      const target = e.target as HTMLElement;
      if (mode !== "deck" || !items[0] || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName) || helpOpen || sheet)
        return;
      const el = document.querySelector<HTMLElement>(".picks-stack .picks-card:not(.next)");
      if (e.key === "ArrowLeft") void act(items[0], "never", el);
      else if (e.key === "ArrowRight") void act(items[0], "request", el);
      else if (e.key === "ArrowUp") void act(items[0], "later", el);
      else if (e.key === "ArrowDown") void act(items[0], "skip", el);
      else if (e.key === "u") void undo();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mode, items, helpOpen, sheet, act, undo]);


  if (me.isPending || suggestions.isPending) {
    return (
      <main className="mx-auto max-w-4xl px-4 py-10">
        <Skeleton className="h-96 w-full" />
      </main>
    );
  }
  if (me.isError || suggestions.isError) {
    return (
      <main className="mx-auto max-w-md px-4 py-16 text-center">
        <h2 className="text-lg font-semibold">Picks is resting</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          {apiErrorMessage(me.error ?? suggestions.error, "Something is not answering right now. Try again in a few minutes.")}
        </p>
        <Button className="mt-4" variant="outline" onClick={() => void me.refetch()}>
          Try again
        </Button>
      </main>
    );
  }

  type QuotaPart = { limit: number; remaining: number | null; restricted: boolean; days: number | null };
  const quota = me.data.seerr?.quota as { movie?: QuotaPart; tv?: QuotaPart } | undefined;
  const quotaText =
    me.data.seerr?.linked && quota
      ? (["movie", "tv"] as const)
          .map((k) => {
            const part = quota[k];
            const icon = k === "movie" ? "🎬" : "📺";
            return part?.limit ? `${icon} ${part.remaining}/${part.limit}` : `${icon} ∞`;
          })
          .join("  ")
      : "";
  const quotaRestricted = !!(quota?.movie?.restricted || quota?.tv?.restricted);
  const noPicks = me.data.state === "no_picks";
  const emptyTitle = served.length ? "Nothing matches this filter" : noPicks ? "No picks yet" : "All caught up";
  const emptyText = served.length
    ? "Try another type or genre."
    : noPicks
      ? "Watch a few things on Plex and check back after the next nightly build."
      : "New picks arrive after each nightly build. Swipe history is under “hidden”.";
  const setLayoutAndStore = (m: Layout) => {
    setLayout(m);
    store("picks.mode", m);
  };
  const onAct = (item: PickItem, action: Action) => void act(item, action, null);
  const deckEl = () => document.querySelector<HTMLElement>(".picks-stack .picks-card:not(.next)");

  return (
    <div className={cn("flex flex-col", mode === "deck" ? "h-full min-h-0" : "min-h-screen")}>
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b bg-card px-3 py-2">
        <div className="text-base font-bold text-amber-400">
          Picks{" "}
          <span className="ml-1 text-sm font-normal text-muted-foreground">· {me.data.name}</span>
        </div>
        <div className="flex-1" />
        {quotaText && (
          <span
            className={cn("whitespace-nowrap text-xs", quotaRestricted ? "font-semibold text-red-400" : "text-muted-foreground")}
            title={`Request quota (remaining/limit per ${quota?.movie?.days || quota?.tv?.days || "?"} days)`}
          >
            {quotaText}
          </span>
        )}
        <Button
          size="sm"
          variant="outline"
          onClick={() => setLayoutAndStore(layout === "auto" ? "grid" : layout === "grid" ? "deck" : "auto")}
          title="Switch layout"
        >
          <LayoutGrid aria-hidden="true" /> layout: {layout}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setHidden((h) => !h)}>
          hidden
        </Button>
        <Button size="sm" variant="outline" onClick={() => setHelp(true)} aria-label="How this works">
          <HelpCircle aria-hidden="true" />
        </Button>
        {session.data?.admin === true && (
          <Button size="sm" variant="ghost" onClick={() => navigate("/")}>
            Admin
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => logout.mutate()} aria-label="Sign out">
          <LogOut aria-hidden="true" />
        </Button>
      </header>

      <div className="flex flex-wrap items-center gap-2 border-b bg-background px-3 py-1.5">
        <div className="inline-flex overflow-hidden rounded-md border" role="group" aria-label="Type">
          {(["all", "movie", "show"] as const).map((t) => (
            <button
              key={t}
              type="button"
              className={cn("px-3 py-1 text-xs", type === t ? "bg-amber-400 font-semibold text-black" : "text-muted-foreground")}
              onClick={() => {
                setType(t);
                store("picks.type", t);
              }}
            >
              {t === "all" ? "All" : t === "movie" ? "Movies" : "TV"}
            </button>
          ))}
        </div>
        <select
          aria-label="Genre"
          className="h-7 max-w-[11rem] rounded-md border bg-background px-2 text-xs"
          value={genres.some((g) => g.name === genre) ? genre : ""}
          onChange={(e) => {
            setGenre(e.target.value);
            store("picks.genre", e.target.value);
          }}
        >
          <option value="">Any genre</option>
          {genres.map((g) => (
            <option key={g.name} value={g.name}>
              {g.name} ({g.count})
            </option>
          ))}
        </select>
        <select
          aria-label="Sort"
          className="h-7 rounded-md border bg-background px-2 text-xs"
          value={sort}
          onChange={(e) => {
            setSort(e.target.value as Sort);
            store("picks.sort", e.target.value);
          }}
        >
          <option value="rank">Best match</option>
          <option value="rating">Top rated</option>
          <option value="votes">Most voted</option>
          <option value="year">Newest</option>
          <option value="title">A–Z</option>
        </select>
        {(suggestions.data?.has_family || family === "only") && (
          <Button
            size="sm"
            variant={family === "only" ? "default" : "outline"}
            onClick={() => setFamily(family === "only" ? "auto" : "only")}
            title="Show family titles instead"
          >
            👪 Family
          </Button>
        )}
        <span className="ml-auto text-xs text-muted-foreground">
          {items.length} of {served.length}
        </span>
      </div>

      {Boolean(me.data.seerr?.error) && (
        <div className="mx-3 mt-2 rounded-md border border-amber-700 bg-amber-950/50 px-3 py-2 text-sm text-amber-200">
          The request service is not answering right now — requests are paused until it is back.
        </div>
      )}

      {mode === "grid" ? (
        <main className="px-3 pb-12 pt-3">
          {items.length === 0 ? (
            <div className="mx-auto my-8 max-w-xl text-center text-muted-foreground">
              <h2 className="text-lg font-semibold text-foreground">{emptyTitle}</h2>
              <p className="mt-1 text-sm">{emptyText}</p>
            </div>
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3 md:grid-cols-[repeat(auto-fill,minmax(170px,1fr))]">
              {items.map((i) => (
                <Card key={keyOf(i)} item={i} withButtons onAct={onAct} onOpen={setSheet} observe={observe} />
              ))}
            </div>
          )}
          {queued.length > 0 && (
            <details className="mt-5 text-muted-foreground">
              <summary className="cursor-pointer py-1 font-semibold">
                {queued.length} already requested or on the way
              </summary>
              <div className="mt-2 grid grid-cols-[repeat(auto-fill,minmax(130px,1fr))] gap-3">
                {queued.map((i) => (
                  <Card key={keyOf(i)} item={i} compact />
                ))}
              </div>
            </details>
          )}
        </main>
      ) : (
        <main className="flex min-h-0 flex-1 flex-col items-center gap-2 px-3 pb-[calc(0.6rem+env(safe-area-inset-bottom))] pt-2">
          <div className="picks-stack">
            {items.length === 0 ? (
              <div className="picks-card">
                <div className="p-4">
                  <div className="text-sm font-bold">{emptyTitle}</div>
                  <p className="mt-1 text-xs text-muted-foreground">{emptyText}</p>
                </div>
              </div>
            ) : (
              <>
                {items[1] && <Card key={keyOf(items[1])} item={items[1]} className="next" />}
                <DeckCard
                  key={keyOf(items[0]!)}
                  item={items[0]!}
                  busy={busy}
                  onAct={(item, action, el) => void act(item, action, el)}
                  onOpen={setSheet}
                />
              </>
            )}
          </div>
          <div className="flex w-[min(100vw-1.6rem,440px)] gap-1.5">
            <Button variant="outline" className="flex-1 border-red-400/60 text-red-300" disabled={!items[0] || busy} onClick={() => items[0] && void act(items[0], "never", deckEl())} title="Not interested (←)">
              ✕ Not for me
            </Button>
            <Button variant="outline" className="flex-1 border-slate-400/60" disabled={!items[0] || busy} onClick={() => items[0] && void act(items[0], "skip", deckEl())} title="Skip for now (↓)">
              ↓ Skip
            </Button>
            <Button variant="outline" className="flex-1 border-sky-400/60 text-sky-200" disabled={!items[0] || busy} onClick={() => items[0] && void act(items[0], "later", deckEl())} title="Maybe later (↑)">
              ⏰ Later
            </Button>
            <Button
              variant="outline"
              className="flex-1 border-emerald-400/60 font-semibold text-emerald-200"
              disabled={!items[0] || busy || !items[0].requestable}
              title={items[0] && !items[0].requestable ? (REASONS[items[0].reason_not_requestable ?? ""] ?? "") : "Request (→)"}
              onClick={() => items[0] && void act(items[0], "request", deckEl())}
            >
              ♥ Request
            </Button>
          </div>
          <div className="flex items-center gap-4 text-xs text-muted-foreground">
            <Button size="sm" variant="outline" disabled={!history.length} onClick={() => void undo()}>
              <Undo2 aria-hidden="true" /> undo
            </Button>
            <span>{items.length ? `${items.length} left` : ""}</span>
          </div>
        </main>
      )}

      {hidden && (
        <section className="fixed inset-x-0 bottom-0 z-40 max-h-[70vh] overflow-auto border-t bg-card px-4 pb-6 pt-3">
          <div className="mb-2 flex items-center justify-between">
            <strong>Hidden titles</strong>
            <Button size="sm" variant="outline" onClick={() => setHidden(false)}>
              close
            </Button>
          </div>
          {dismissed.isPending ? (
            <Skeleton className="h-16 w-full" />
          ) : (
            (["never", "later"] as const).map((kind) => {
              const list = (dismissed.data?.[kind] ?? []) as unknown as DismissedItem[];
              if (!list.length) return null;
              return (
                <div key={kind}>
                  <h3 className="mt-2 text-sm font-semibold">{kind === "never" ? "Not for me" : "Maybe later"}</h3>
                  <ul>
                    {list.map((d) => (
                      <li key={`${d.tmdb_id}:${d.media_type}`} className="flex items-center justify-between gap-2 border-b py-1.5 text-sm">
                        <span>
                          {d.title}
                          {d.year ? ` (${d.year})` : ""}
                          {d.until ? ` · until ${d.until.slice(0, 10)}` : ""}
                        </span>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={async () => {
                            const r = await api.act({ action: "undo", tmdb_id: d.tmdb_id, media_type: d.media_type });
                            if (r.ok) {
                              toast("Restored");
                              void queryClient.invalidateQueries({ queryKey: ["me"] });
                            }
                          }}
                        >
                          restore
                        </Button>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })
          )}
          {!dismissed.isPending && !(dismissed.data?.never?.length || dismissed.data?.later?.length) && (
            <p className="text-sm text-muted-foreground">Nothing hidden.</p>
          )}
        </section>
      )}

      <Dialog open={!!sheet} onOpenChange={(open) => !open && setSheet(null)}>
        <DialogContent>
          {sheet && (
            <>
              <DialogHeader>
                <DialogTitle>
                  {sheet.title}
                  {sheet.year ? ` (${sheet.year})` : ""}
                </DialogTitle>
                <DialogDescription>
                  {metaLine(sheet)}
                  {sheet.genres.length ? ` · ${sheet.genres.join(", ")}` : ""}
                </DialogDescription>
              </DialogHeader>
              <p className="text-sm">{sheet.overview || "No synopsis available."}</p>
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={helpOpen} onOpenChange={(open) => (open ? setHelp(true) : closeHelp())}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>How Picks works</DialogTitle>
            <DialogDescription>
              These are titles your own Plex watch history points at that the library does not have
              yet. Nothing downloads on its own — a request waits for approval, just like requesting
              in the request app.
            </DialogDescription>
          </DialogHeader>
          <ul className="space-y-2 text-sm">
            <li><b>Swipe right</b> (or ♥ Request) — request it. Shows request their first season; add more later.</li>
            <li><b>Swipe left</b> (or ✕ Not for me) — hide it for good. Restore any time from <em>hidden</em>.</li>
            <li><b>Swipe up</b> (or ⏰ Later) — snooze it; it comes back in {SNOOZE_DAYS} days.</li>
            <li><b>Swipe down</b> (or ↓ Skip) — not now; it goes to the back of the deck and stays in your picks.</li>
            <li><b>Undo</b> takes back your last hide, snooze or skip. A request is taken back in the request app.</li>
          </ul>
          <p className="text-xs text-muted-foreground">
            Filter by type and genre above the cards; tap a synopsis to read all of it. On a keyboard:
            ← → ↑ ↓ and <kbd>u</kbd>. The counter in the header is your request quota (🎬 movies · 📺 TV).
          </p>
          <Button onClick={closeHelp}>Got it</Button>
        </DialogContent>
      </Dialog>
    </div>
  );
}
