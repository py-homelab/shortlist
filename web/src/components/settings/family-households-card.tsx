import { useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAutosavedSettings } from "@/lib/autosave";
import type { Settings } from "@/lib/types";

function readNumber(settings: Settings, key: string, fallback: number): number {
  const value = Number(settings[key]);
  return Number.isFinite(value) ? value : fallback;
}

/**
 * Who shares an account with children. Decided per person from their own recent viewing — the
 * engine counts children's titles, these thresholds turn the counts into a label — and overridable
 * on each person's page. A family household's own rows leave children's titles out (rows set to
 * "Decided per person") and their family row carries them; nobody else is split at all.
 */
export function FamilyHouseholdsCard({ settings }: { settings: Settings }) {
  const [share, setShare] = useState(() => Math.round(readNumber(settings, "family.min_share", 0.15) * 100));
  const [kidsTitles, setKidsTitles] = useState(() => readNumber(settings, "family.min_kids_titles", 4));
  const [kidsAccount, setKidsAccount] = useState(() =>
    Math.round(readNumber(settings, "family.kids_account_share", 0.8) * 100),
  );
  const [minTitles, setMinTitles] = useState(() => readNumber(settings, "family.min_titles", 10));
  const save = useAutosavedSettings({ share, kidsTitles, kidsAccount, minTitles }, () =>
    share >= 0 && share <= 100 && kidsAccount >= 0 && kidsAccount <= 100 && kidsTitles >= 1 && minTitles >= 1
      ? {
          "family.min_share": share / 100,
          "family.min_kids_titles": kidsTitles,
          "family.kids_account_share": kidsAccount / 100,
          "family.min_titles": minTitles,
        }
      : null,
  );
  const field = (id: string, label: string, value: number, set: (n: number) => void, suffix: string, hint: string) => (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          type="number"
          min={suffix === "%" ? 0 : 1}
          max={suffix === "%" ? 100 : 1000}
          className="max-w-[7rem]"
          value={value}
          onChange={(e) => {
            const n = Number(e.target.value);
            if (Number.isFinite(n)) set(Math.round(n));
          }}
        />
        <span className="text-sm text-muted-foreground">{suffix}</span>
      </div>
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  );

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <p className="text-sm text-muted-foreground">
          Some people share their Plex account with their children; most don’t. Each person is sorted
          by their own viewing over the last months: a <strong>family</strong> gets rows without
          children’s titles plus a family row that carries them; a <strong>child’s own account</strong>{" "}
          gets only children’s titles in rows left on Decided per person; everyone else sees children’s titles ranked with
          everything else. Needs an engine that reports viewing (Settings → Engine);
          override anyone on their user page.
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          {field("family-share", "A family at", share, setShare, "%", "or more of what they watched is children’s titles…")}
          {field("family-titles", "…from at least", kidsTitles, setKidsTitles, "titles", "distinct children’s titles, so one cartoon night does not count.")}
          {field("kids-account", "A child’s own account above", kidsAccount, setKidsAccount, "%", "Their rows left on Decided per person hold only children’s titles, and they get no separate family row.")}
          {field("min-titles", "Decide only after", minTitles, setMinTitles, "titles", "watched in the window; with fewer, everyone counts as grown-up viewing.")}
        </div>
        <SaveStatus
          isPending={save.isPending}
          isError={save.isError}
          error={save.error}
          saved={save.saved}
          onRetry={save.retry}
        />
      </CardContent>
    </Card>
  );
}
