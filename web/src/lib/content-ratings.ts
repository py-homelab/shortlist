/** Content ratings, for narrowing a watching-account copy to a children's profile. */

/**
 * What a children's profile gets by default. Deliberately the strict set: every rating here is one
 * Plex's own "younger kid" restriction admits, so nothing this list copies can be something the
 * profile is then not allowed to see. PG and TV-PG are a household's call, offered but not ticked.
 */
export const CHILDRENS_RATINGS = ["G", "TV-G", "TV-Y", "TV-Y7", "TV-Y7-FV"];

/** Offered before any preview has said what the history actually holds. */
export const ALWAYS_OFFERED = [...CHILDRENS_RATINGS, "PG", "TV-PG"];

/** Youngest first, films before television; anything unrecognised after, by name. */
const ORDER = [
  "G",
  "PG",
  "PG-13",
  "R",
  "NC-17",
  "TV-Y",
  "TV-Y7",
  "TV-Y7-FV",
  "TV-G",
  "TV-PG",
  "TV-14",
  "TV-MA",
];

export function byAge(a: string, b: string): number {
  const [ia, ib] = [ORDER.indexOf(a), ORDER.indexOf(b)];
  if (ia !== -1 || ib !== -1)
    return (ia === -1 ? ORDER.length : ia) - (ib === -1 ? ORDER.length : ib);
  return a.localeCompare(b);
}

/** One stable string per choice, so a preview can be recognised as stale when the choice moves. */
export function ratingsKey(ratings: string[] | null): string {
  // "everything" and "narrowed to nothing ticked" must not share a key: a preview of everything
  // would otherwise stay fresh while the box is ticked and every rating unticked.
  return ratings === null ? "everything" : `only:${[...ratings].sort().join("|")}`;
}
