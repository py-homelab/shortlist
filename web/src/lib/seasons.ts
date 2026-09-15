import { isPresetCron, timeFromCron } from "@/lib/format";
import type { Season, SeasonStatus } from "@/lib/types";

/**
 * Seasonal rows (discussion #124): a row that follows the calendar and is hidden between seasons.
 *
 * Which season a row is in TODAY is never worked out here. Days turn over on the SERVER's clock — the
 * one the runs, the midnight job and Plex follow — so the row's `season_status` comes from the API,
 * exactly as `shown_today` does for a day schedule. What this file computes is date-free: a season's
 * window as dates in any year, for the editor to say "shows 1 Oct – 31 Oct".
 */

/** "31 Oct" (in the reader's own date order) for an ISO date, read as a calendar date, not an instant. */
export function seasonDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** When a season shows on a row with these days before and after, e.g. "1 Oct – 31 Oct".
 *
 *  Worked out in a fixed, non-leap year, and the label never names the year. In a leap year a
 *  Valentine's window reaching past 28 February (15 or more days after) ends a day earlier on the
 *  server than it reads here; where the row is TODAY always comes from the server's `season_status`. */
export function seasonWindowLabel(
  season: Pick<Season, "month" | "day">,
  leadDays: number,
  afterDays: number,
): string {
  const anchor = Date.UTC(2026, season.month - 1, season.day);
  const iso = (ms: number) => new Date(ms).toISOString().slice(0, 10);
  return `${seasonDate(iso(anchor - leadDays * DAY_MS))} – ${seasonDate(iso(anchor + afterDays * DAY_MS))}`;
}

/** The one line the Rows card and the editor say about where a seasonal row is today. */
export function seasonStatusLine(status: SeasonStatus | null | undefined): string {
  if (!status) return "";
  if (status.showing) {
    return `Showing ${status.showing.emoji} ${status.showing.name} until ${seasonDate(status.showing.ends)}`;
  }
  if (status.next) {
    return `Hidden until ${status.next.emoji} ${status.next.name} starts on ${seasonDate(status.next.starts)}`;
  }
  return "";
}

/** Whether a row's schedule runs it every day — what a seasonal row needs to change nightly and to switch
 *  seasons on the day they start. */
export function isNightly(cron: string): boolean {
  const trimmed = cron.trim();
  return trimmed !== "" && isPresetCron(trimmed) && !timeFromCron(trimmed).weekly;
}
