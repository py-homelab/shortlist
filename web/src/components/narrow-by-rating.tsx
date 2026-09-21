import {
  ALWAYS_OFFERED,
  byAge,
  CHILDRENS_RATINGS,
} from "@/lib/content-ratings";
import type { TransferResult } from "@/lib/types";

/**
 * The "only some ratings" control on the watching-account copy.
 *
 * The list of ratings on offer GROWS once a preview has run: the preview reports every rating the
 * history holds, and a rating nobody offered is a rating nobody can tick — a library with
 * "TV-Y7-FV" or a British "U" in it would otherwise lose those titles with no way to say so.
 */
export function NarrowByRating({
  ratings,
  seen,
  onChange,
}: {
  /** null = copy everything. */
  ratings: string[] | null;
  /** `ratings_seen` from the last preview, when there has been one. */
  seen: Record<string, number> | null;
  onChange: (next: string[] | null) => void;
}) {
  const offered = [
    ...new Set([
      ...ALWAYS_OFFERED,
      ...Object.keys(seen ?? {}).filter((r) => r !== ""),
      ...(ratings ?? []),
    ]),
  ].sort(byAge);
  const unrated = seen?.[""] ?? 0;

  return (
    <div className="space-y-2 rounded-md border border-dashed p-3 text-sm">
      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          className="mt-1"
          checked={ratings !== null}
          onChange={(e) =>
            onChange(e.target.checked ? [...CHILDRENS_RATINGS] : null)
          }
        />
        <span>
          <span className="font-medium">
            Only copy titles with certain content ratings
          </span>
          <span className="block text-xs text-muted-foreground">
            For a children&rsquo;s profile. Copies just the titles Plex rates
            inside the list you tick and that account is allowed to see &mdash;
            on Plex, and in the history Shortlist keeps for it. Nothing already
            watched on that account is un-marked or rewound: a title you have
            both watched is topped up to your play count, and a copy that would
            have to do more than that is refused instead.
          </span>
        </span>
      </label>
      {ratings !== null && (
        <fieldset className="space-y-2 pl-6">
          <legend className="sr-only">Content ratings to copy</legend>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {offered.map((rating) => (
              <label key={rating} className="flex items-center gap-1.5 text-xs">
                <input
                  type="checkbox"
                  checked={ratings.includes(rating)}
                  onChange={(e) =>
                    onChange(
                      e.target.checked
                        ? [...ratings, rating]
                        : ratings.filter((r) => r !== rating),
                    )
                  }
                />
                <span>{rating}</span>
                {seen?.[rating] !== undefined && (
                  <span className="text-muted-foreground">
                    ({seen[rating]})
                  </span>
                )}
              </label>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            {seen === null
              ? "Press Preview to see which ratings this history holds, and how much sits under each."
              : unrated > 0
                ? `${unrated} watched ${unrated === 1 ? "title has" : "titles have"} no rating in Plex. Those always stay behind — give them a rating in Plex first if they belong on that account.`
                : "Every watched title has a rating in Plex."}
          </p>
          {ratings.length === 0 && (
            <p className="text-xs text-destructive-text">
              Tick at least one rating, or untick the box above to copy
              everything.
            </p>
          )}
        </fieldset>
      )}
    </div>
  );
}

/** What a narrowed preview found — or, when the copy would be refused, why. */
export function NarrowedPreview({ preview }: { preview: TransferResult }) {
  if (preview.refused) {
    return (
      <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
        <p>
          <strong>This copy would be refused, and nothing was changed.</strong>{" "}
          That account already has watching of its own that this copy would
          un-tick, rewind or overwrite, and a copy narrowed by rating never
          does that. Undo the earlier copy below, or mark these unwatched in
          Plex, then preview again.
        </p>
        {preview.in_the_way.length > 0 && (
          <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs text-muted-foreground">
            {preview.in_the_way.map((title) => (
              <li key={title}>{title}</li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  if (preview.ratings.length === 0) return null;

  const total = preview.kept + preview.left_out;
  const rows = Object.entries(preview.ratings_seen).sort(([a], [b]) =>
    a === "" ? 1 : b === "" ? -1 : byAge(a, b),
  );
  return (
    <div className="space-y-2">
      <p>
        Only <strong>{preview.kept}</strong> of {total} watched films and
        episodes would be copied &mdash; the ones rated{" "}
        {preview.ratings.join(", ")}. The other {preview.left_out} stay behind.
        {preview.hidden_from_target > 0 &&
          ` ${preview.hidden_from_target} of those ${preview.hidden_from_target === 1 ? "is" : "are"} rated inside your list, but that account's own Plex restrictions hide ${preview.hidden_from_target === 1 ? "it" : "them"}.`}
      </p>
      <div className="overflow-x-auto">
        <table className="text-xs">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="pr-4 font-normal">Rating</th>
              <th className="pr-4 text-right font-normal">Watched</th>
              <th className="text-right font-normal">Would copy</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([rating, count]) => (
              <tr key={rating || "unrated"}>
                <td className="pr-4">{rating || "No rating"}</td>
                <td className="pr-4 text-right tabular-nums">{count}</td>
                <td className="text-right tabular-nums">
                  {preview.ratings_kept[rating] ?? 0}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {preview.residue_cleared > 0 && (
        <p className="text-xs text-muted-foreground">
          An earlier copy left {preview.residue_cleared} history{" "}
          {preview.residue_cleared === 1 ? "entry" : "entries"} in Shortlist
          for titles this copy doesn&rsquo;t carry. The real run removes them,
          so they stop shaping that account&rsquo;s rows. Plex isn&rsquo;t
          touched by that.
        </p>
      )}
      {preview.kept_preview.length > 0 && (
        <>
          <p className="text-xs text-muted-foreground">
            What would go across
            {preview.kept_preview.length >= 50 ? " (the first 50)" : ""}:
          </p>
          <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs text-muted-foreground">
            {preview.kept_preview.map((title) => (
              <li key={title}>{title}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
