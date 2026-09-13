import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { renderRowName } from "@/lib/format";

/**
 * A row's Plex description (issue #120). Optional: empty means Shortlist leaves the description on
 * Plex alone, which is what keeps a value set in another tool.
 */
export function RowDescriptionField({
  value,
  onChange,
}: {
  value: string;
  onChange: (description: string) => void;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor="row-description">Description</Label>
      <Textarea
        id="row-description"
        value={value}
        maxLength={2000}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. Picked for {user} from what they've watched"
      />
      {/* The placeholders are named in running text rather than with <TemplateVarsHint />: the
          name field already lists them, and a second list reads as a second set. */}
      <p className="text-sm text-muted-foreground">
        Shown when someone opens the row, with the same placeholders as the
        name, from the row&rsquo;s next run. Empty leaves Plex&rsquo;s own
        alone; clearing it later hands it back, unless it was changed in Plex
        since.
      </p>
    </div>
  );
}

/**
 * A row's Plex sort-title prefix (issue #120). Optional, like the description. It lives with the
 * row's placement rather than its look because that is what it decides: where the row sits.
 */
export function RowSortPrefixField({
  value,
  rowName,
  onChange,
}: {
  value: string;
  /** The row's name as typed, for the "sorts as" example. */
  rowName: string;
  onChange: (sortTitlePrefix: string) => void;
}) {
  const hasPrefix = value.trim() !== "";
  return (
    <div className="space-y-2 border-t pt-4">
      <Label htmlFor="row-sort-title-prefix">Sort title prefix</Label>
      <Input
        id="row-sort-title-prefix"
        value={value}
        maxLength={64}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. !010_"
        className="w-48"
      />
      <p className="text-sm text-muted-foreground">
        Goes in front of the row&rsquo;s name to decide where it sorts in the
        library&rsquo;s Collections tab, from the row&rsquo;s next run. It
        doesn&rsquo;t move the row on Home &mdash; the shelf position above
        does that. Leave it empty to leave Plex&rsquo;s sort order alone.
      </p>
      {hasPrefix && rowName.trim() !== "" && (
        <p className="text-sm">
          Sorts as{" "}
          <span className="font-mono">
            {value}
            {renderRowName(rowName)}
          </span>
          {rowName !== renderRowName(rowName) && (
            <span className="text-muted-foreground">
              {" "}
              for Sarah, browsing Movies
            </span>
          )}
        </p>
      )}
    </div>
  );
}
