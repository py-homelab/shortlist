import { useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { usePatchUser } from "@/lib/queries";
import type { User } from "@/lib/types";

export type HouseholdOverride = "auto" | "adult" | "family" | "kids";

const OVERRIDES: { value: HouseholdOverride; label: string }[] = [
  { value: "auto", label: "Decide from what they watch" },
  { value: "adult", label: "Grown-up viewing" },
  { value: "family", label: "A family sharing the account with children" },
  { value: "kids", label: "A child’s own account" },
];

const LABELS: Record<string, string> = {
  adult: "grown-up viewing",
  family: "a family sharing the account with children",
  kids: "a child’s own account",
};

function asOverride(value: unknown): HouseholdOverride {
  return value === "adult" || value === "family" || value === "kids" ? value : "auto";
}

/** What the last run concluded, and from what — so "why does my family row not appear?" has an
 *  answer on this page. */
export function householdSummary(household: User["household"]): string {
  const hh = household as
    | {
        label?: string | null;
        source?: string;
        kids_titles?: number | null;
        window_titles?: number | null;
        window_days?: number | null;
      }
    | null
    | undefined;
  if (!hh || !hh.label) return "Not decided yet — the next run with an engine that reports viewing will say.";
  const what = LABELS[hh.label] ?? hh.label;
  if (hh.source === "override") return `Set by you: ${what}.`;
  const counts =
    hh.window_titles != null && hh.kids_titles != null
      ? ` — ${hh.kids_titles} children’s titles of ${hh.window_titles} watched in the last ${
          hh.window_days ? Math.round(hh.window_days / 30) + " months" : "while"
        }`
      : "";
  return `Last run: ${what}${counts}.`;
}

/**
 * Who watches under this account. A family sharing one Plex account with its children gets its own
 * rows without children's titles and a family row that carries them; nobody else is split at all.
 * Decided from their viewing unless the owner says otherwise here.
 */
export function UserHousehold({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const [value, setValue] = useState<HouseholdOverride>(() =>
    asOverride((user.prefs as Record<string, unknown> | undefined)?.household),
  );
  const [saved, setSaved] = useState(false);

  const save = (next: HouseholdOverride) => {
    setValue(next);
    setSaved(false);
    patchUser.mutate(
      { id: user.id, patch: { prefs: { household: next } } },
      { onSuccess: () => setSaved(true) },
    );
  };

  return (
    <Card>
      <CardContent className="space-y-2 pt-6">
        <div className="flex items-center gap-2">
          <Label htmlFor="user-household">Who watches under this account</Label>
          <SavedIndicator show={saved} />
        </div>
        <select
          id="user-household"
          value={value}
          onChange={(e) => save(asOverride(e.target.value))}
          className="h-9 w-full max-w-sm rounded-md border bg-background px-3 text-sm"
        >
          {OVERRIDES.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <p className="text-sm text-muted-foreground">{householdSummary(user.household)}</p>
        <p className="text-sm text-muted-foreground">
          A family sharing the account gets rows without children’s titles, and a family row with them.
          Everyone else sees children’s titles ranked with everything else. The thresholds are in
          Settings → Finding titles.
        </p>
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(patchUser.error, "Couldn’t save this. Try again.")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
