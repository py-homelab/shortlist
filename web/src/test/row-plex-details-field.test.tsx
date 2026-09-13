import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RowSortPrefixField } from "@/components/rows/row-plex-details-field";
import type { CollectionInput } from "@/lib/types";

function renderField(rowName: string, media: CollectionInput["media"]) {
  render(
    <RowSortPrefixField
      value="!010_"
      rowName={rowName}
      media={media}
      onChange={() => {}}
    />,
  );
}

describe("RowSortPrefixField — the sorts-as example", () => {
  it("fills {library_name} with a movie library when the row builds only in movie libraries", () => {
    renderField("More {library_name} to watch", "movie");

    expect(screen.getByText("!010_More Movies to watch")).toBeInTheDocument();
    expect(screen.getByText(/for Sarah, browsing Movies/)).toBeInTheDocument();
  });

  it("fills {library_name} with a TV library when the row builds only in TV libraries", () => {
    // A TV-only row can never be named after a movie library, so previewing one shows a sort title
    // the row will never have.
    renderField("More {library_name} to watch", "show");

    expect(screen.getByText("!010_More TV Shows to watch")).toBeInTheDocument();
    expect(screen.getByText(/for Sarah, browsing TV Shows/)).toBeInTheDocument();
    expect(screen.queryByText(/Movies/)).not.toBeInTheDocument();
  });

  it("uses the movie library for a row that builds in both, as the Plex card beside it does", () => {
    renderField("More {library_name} to watch", "both");

    expect(screen.getByText("!010_More Movies to watch")).toBeInTheDocument();
    expect(screen.getByText(/for Sarah, browsing Movies/)).toBeInTheDocument();
  });

  it("names no sample person or library when the name has no placeholders", () => {
    renderField("Hidden Gems", "show");

    expect(screen.getByText("!010_Hidden Gems")).toBeInTheDocument();
    expect(screen.queryByText(/for Sarah/)).not.toBeInTheDocument();
  });
});
