import type { ReactNode } from "react";

import { ImdbGlyph, TmdbGlyph, TraktGlyph } from "@/components/brand-glyphs";
import { type TitleLink, titleLinks } from "@/lib/title-links";
import { cn } from "@/lib/utils";

/** Each look-up link's brand mark. */
const GLYPH: Record<TitleLink["label"], (props: { className?: string }) => ReactNode> = {
  TMDB: TmdbGlyph,
  IMDb: ImdbGlyph,
  Trakt: TraktGlyph,
};

/**
 * TMDB, IMDb and Trakt links for one title, each marked with its service's icon.
 *
 * `labelled` shows the service name beside the icon, for a line with room for it (the run report).
 * Without it the links are icon-only tiles for tight spaces — a poster shelf, a feed line — and carry
 * "<title> on TMDB" as their accessible name, since a bare icon says nothing to a screen reader.
 * Renders nothing for a title with no TMDB id: a link to `/movie/undefined` is worse than none.
 */
export function TitleLinkIcons({
  title,
  labelled = false,
  className,
}: {
  title: Parameters<typeof titleLinks>[0];
  labelled?: boolean;
  className?: string;
}) {
  const links = titleLinks(title);
  if (links.length === 0) return null;
  return (
    <span className={cn("inline-flex items-center", labelled ? "gap-2" : "gap-1.5", className)}>
      {links.map((link) => {
        const Glyph = GLYPH[link.label];
        return labelled ? (
          <a
            key={link.label}
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 self-center hover:text-foreground hover:underline focus-visible:text-foreground focus-visible:underline"
          >
            <Glyph className="h-3.5 w-3.5 shrink-0 rounded-[2px]" />
            {link.label}
          </a>
        ) : (
          <a
            key={link.label}
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`${title.title ?? "This title"} on ${link.label}`}
            title={`Open on ${link.label}`}
            className="inline-grid h-5 w-6 place-items-center rounded border border-border bg-elevated transition-colors hover:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Glyph className="h-3.5 w-3.5 rounded-[2px]" />
          </a>
        );
      })}
    </span>
  );
}
