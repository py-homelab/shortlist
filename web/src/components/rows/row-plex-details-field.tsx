import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { renderRowName } from "@/lib/format";

/**
 * A row's Plex description and sort-title prefix (issue #120). Both are optional, and empty means
 * Shortlist leaves that field on Plex alone — which is what keeps a value set in another tool.
 */
export function RowPlexDetailsField({
  description,
  sortTitlePrefix,
  rowName,
  onChange,
}: {
  description: string;
  sortTitlePrefix: string;
  /** The row's name as typed, for the "sorts as" example. */
  rowName: string;
  onChange: (patch: {
    description?: string;
    sort_title_prefix?: string;
  }) => void;
}) {
  const hasPrefix = sortTitlePrefix.trim() !== "";
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="row-description">Description</Label>
        <Textarea
          id="row-description"
          value={description}
          maxLength={2000}
          onChange={(e) => onChange({ description: e.target.value })}
          placeholder="e.g. Picked for {user} from what they've watched"
        />
        {/* The placeholders are named in running text rather than with <TemplateVarsHint />: the
            name field already lists them, and a second list reads as a second set. */}
        <p className="text-sm text-muted-foreground">
          Plex shows this when someone opens the row. It fills in the same
          placeholders as the row&rsquo;s name. Leave it empty to leave
          Plex&rsquo;s description alone.
        </p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="row-sort-title-prefix">Sort title prefix</Label>
        <Input
          id="row-sort-title-prefix"
          value={sortTitlePrefix}
          maxLength={64}
          onChange={(e) => onChange({ sort_title_prefix: e.target.value })}
          placeholder="e.g. !010_"
          className="w-48"
        />
        <p className="text-sm text-muted-foreground">
          Goes in front of the row&rsquo;s name to decide where it sorts in the
          library&rsquo;s Collections tab. It doesn&rsquo;t move the row on Home
          &mdash; that is set under &ldquo;Where it appears&rdquo;. Leave it
          empty to leave Plex&rsquo;s sort order alone.
        </p>
        {hasPrefix && rowName.trim() !== "" && (
          <p className="text-sm">
            Sorts as{" "}
            <span className="font-mono">
              {sortTitlePrefix}
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

      <p className="text-sm text-muted-foreground">
        Changes reach Plex the next time this row runs, and replace whatever
        that field held. Clearing a field hands it back to Plex, which then
        shows no description and sorts the row by its name. Anything someone
        has changed there since Shortlist set it is left alone.
      </p>
    </div>
  );
}
