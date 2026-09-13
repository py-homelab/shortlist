import { api } from "@/lib/api";
import { renderRowName } from "@/lib/format";
import type { CollectionInput } from "@/lib/types";

/** The sample person and library every preview on this card is filled in for. */
const SAMPLE = { topSeed: "Fargo", user: "Sarah" } as const;

/**
 * Fill a description's placeholders with the sample values. Line breaks are kept: `renderRowName`
 * collapses whitespace around `{library_name}`, which is right for a one-line title and would
 * flatten a description typed over several lines — the engine's `render_description` keeps them too.
 */
function renderDescription(template: string, libraryName: string): string {
  return template
    .replaceAll("{top_seed}", SAMPLE.topSeed)
    .replaceAll("{user}", SAMPLE.user)
    .replaceAll("{library_name}", libraryName)
    .trim();
}

/** What varies about the name, in the words the caption uses — or null when nothing does. */
function nameCaption(input: CollectionInput, template: string): string | null {
  // A per-person row renders a different name for each person, from their own viewing; a SHARED
  // row is one collection everybody sees, so only {library_name} moves — telling someone their
  // shared row is named per person is simply untrue.
  const perPerson = /\{(top_seed|user)\}/.test(template);
  const perLibrary = template.includes("{library_name}");
  if (input.build !== "shared" && perPerson) {
    return "Example only — each person gets their own name here, from their own viewing.";
  }
  return perLibrary
    ? "Example only — the real library name fills in, so each library gets its own."
    : null;
}

/**
 * The row as Plex shows it, filled in for a sample person: its poster, its name, its description.
 *
 * Beside the fields that set those three, because that is where the typing happens — it used to sit
 * at the top of the sidebar, a column away from the name and further still from the poster and
 * description, which lived in folded groups near the bottom of the page.
 *
 * The poster is only a real image when one exists (an uploaded one). A text or AI poster is rendered
 * by the server, so this shows its words on a plain tile and the Poster field's Preview button makes
 * the real one — drawing a lookalike here would promise a picture Plex will not show.
 */
export function RowPlexCard({
  input,
  collectionId,
  hasImage,
}: {
  input: CollectionInput;
  collectionId: number | null;
  hasImage: boolean;
}) {
  const template = input.name_template || input.name;
  // The sample library has to match the row's media type, or a TV-only row previews as
  // "More Movies to watch" — a name it can never produce.
  const sampleLibrary = input.media === "show" ? "TV Shows" : "Movies";
  const shown =
    renderRowName(template, SAMPLE.topSeed, SAMPLE.user, sampleLibrary) ||
    "Picked for You";
  const description = renderDescription(input.description, sampleLibrary);
  const caption = nameCaption(input, template);
  const mode = input.poster.mode;
  const posterTitle = renderRowName(
    input.poster.title,
    SAMPLE.topSeed,
    SAMPLE.user,
    sampleLibrary,
  );
  const posterSubtitle = renderRowName(
    input.poster.subtitle,
    SAMPLE.topSeed,
    SAMPLE.user,
    sampleLibrary,
  );

  return (
    <div className="space-y-2">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">
        On Plex
      </p>
      <div className="aspect-[2/3] w-full max-w-44 overflow-hidden rounded-md border bg-muted">
        {mode === "upload" && collectionId !== null && hasImage ? (
          <img
            src={api.posterImageUrl(collectionId)}
            alt="This row's poster"
            className="size-full object-cover"
          />
        ) : mode === "" || mode === "upload" ? (
          <div className="flex size-full items-center justify-center p-3 text-center text-xs text-muted-foreground">
            {mode === "upload"
              ? "No image uploaded yet"
              : "Plex’s own artwork"}
          </div>
        ) : (
          <div className="flex size-full flex-col justify-end gap-1 bg-accent p-3">
            <p className="break-words text-sm font-semibold text-accent-foreground">
              {posterTitle || shown}
            </p>
            {posterSubtitle && (
              <p className="break-words text-xs text-accent-foreground/80">
                {posterSubtitle}
              </p>
            )}
          </div>
        )}
      </div>
      <p className="break-words text-sm font-medium">“{shown}”</p>
      {description && (
        <p className="whitespace-pre-line break-words text-xs text-muted-foreground">
          {description}
        </p>
      )}
      {caption && <p className="text-xs text-muted-foreground">{caption}</p>}
    </div>
  );
}
