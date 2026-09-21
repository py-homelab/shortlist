import { useState } from "react";

import { SavedIndicator } from "@/components/saved-indicator";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { usePatchUser } from "@/lib/queries";
import { parseLabels } from "@/lib/title-labels";
import type { User } from "@/lib/types";

function stored(user: User, key: "admit_labels" | "hide_labels"): string[] {
  const value = (user.prefs as Record<string, unknown> | undefined)?.[key];
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/**
 * The owner's own Plex labels for one account, on top of the ratings its Plex restriction allows.
 *
 * Ratings say "safe"; a label says "for this person". They are set HERE rather than in Plex's
 * restriction form for two reasons: that form can only AND a label list with a rating list (which
 * hides everything unlabelled), and saving it drops every exclude Shortlist has written to the account.
 */
export function UserTitleLabels({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const [admit, setAdmit] = useState(() => stored(user, "admit_labels").join(", "));
  const [hide, setHide] = useState(() => stored(user, "hide_labels").join(", "));
  const [saved, setSaved] = useState(false);

  const unchanged =
    parseLabels(admit).join("|") === stored(user, "admit_labels").join("|") &&
    parseLabels(hide).join("|") === stored(user, "hide_labels").join("|");
  // Shortlist only writes this account's share filter while it manages their sharing, and Plex
  // refuses label restrictions outright on an account with one of its own restriction presets.
  const cannot =
    user.user_type === "owner"
      ? "Plex never restricts the server owner's own account, so there is nothing for these to be written into."
      : !user.manage_sharing
    ? "Shortlist is set to leave this account's Plex sharing alone, so it won't write these. Turn that back on above to use them."
    : user.restricted && user.restriction_profile
      ? "This account uses one of Plex's built-in restriction presets, and Plex won't accept label rules while one is applied. Set the preset to None in Plex and restrict it by your own allowed ratings instead."
      : null;

  const save = () => {
    setSaved(false);
    patchUser.mutate(
      {
        id: user.id,
        patch: {
          prefs: {
            admit_labels: parseLabels(admit),
            hide_labels: parseLabels(hide),
          },
        },
      },
      { onSuccess: () => setSaved(true) },
    );
  };

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <p className="text-sm font-medium">Titles picked by hand</p>
            <SavedIndicator show={saved} />
          </div>
          <p className="text-sm text-muted-foreground">
            For an account you restrict by rating in Plex. A rating says a
            title is safe; a label says it is for them. Label the titles in
            Plex, name the labels here, and Shortlist writes them into this
            account&rsquo;s restriction &mdash; so you never edit it in Plex,
            where saving drops everything Shortlist has hidden from this
            account until its next privacy pass.
          </p>
        </div>
        <div className="space-y-1">
          <Label htmlFor="user-admit-labels">
            Also show titles labelled
          </Label>
          <Input
            id="user-admit-labels"
            value={admit}
            disabled={cannot !== null}
            placeholder="For Kids"
            onChange={(e) => {
              setAdmit(e.target.value);
              setSaved(false);
            }}
          />
          <p className="text-xs text-muted-foreground">
            Shown even though their rating is outside what this account is
            allowed. Does nothing for an account with no allow list &mdash; it
            already sees everything.
          </p>
        </div>
        <div className="space-y-1">
          <Label htmlFor="user-hide-labels">Never show titles labelled</Label>
          <Input
            id="user-hide-labels"
            value={hide}
            disabled={cannot !== null}
            placeholder="Not For Kids"
            onChange={(e) => {
              setHide(e.target.value);
              setSaved(false);
            }}
          />
          <p className="text-xs text-muted-foreground">
            Hidden even though their rating is allowed. Separate several
            labels with commas.
          </p>
        </div>
        {cannot && <p className="text-sm text-muted-foreground">{cannot}</p>}
        <div className="flex items-center gap-3">
          <Button
            size="sm"
            onClick={save}
            disabled={cannot !== null || unchanged || patchUser.isPending}
          >
            Save labels
          </Button>
          {saved && (
            <span className="text-xs text-muted-foreground">
              Shortlist is updating this account&rsquo;s Plex restriction now.
              Their rows follow on the next run: on a child&rsquo;s own account a
              title you admitted counts as a children&rsquo;s title, and one you hid
              never does.
            </span>
          )}
        </div>
        {patchUser.isError && (
          <p role="alert" className="text-sm text-destructive-text">
            {apiErrorMessage(patchUser.error, "Couldn’t save this. Try again.")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
