import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PosterField } from "@/components/rows/poster-field";
import type { PosterInput } from "@/lib/types";

const getImageProvider = vi.fn(() =>
  Promise.resolve({
    capable: false,
    provider: "anthropic",
    reason: "no images here",
  }),
);

vi.mock("@/lib/api", () => ({
  api: {
    getImageProvider: () => getImageProvider(),
    posterImageUrl: (id: number) => `/img/${id}`,
  },
  apiErrorMessage: (_e: unknown, fallback: string) => fallback,
}));

function renderField(
  poster: Partial<PosterInput>,
  collectionId: number | null = 1,
  seasonal = false,
) {
  const value: PosterInput = {
    mode: "",
    title: "",
    subtitle: "",
    style: "",
    ...poster,
  };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <PosterField
        value={value}
        onChange={() => {}}
        collectionId={collectionId}
        hasImage={false}
        seasonal={seasonal}
      />
    </QueryClientProvider>,
  );
}

describe("PosterField", () => {
  it("offers all four poster sources", () => {
    renderField({});
    expect(screen.getByText("Plex default")).toBeInTheDocument();
    expect(screen.getByText("Upload")).toBeInTheDocument();
    expect(screen.getByText("Text")).toBeInTheDocument();
    expect(screen.getByText("AI image")).toBeInTheDocument();
  });

  it("shows the text fields for the built-in text poster (no provider gate)", () => {
    renderField({ mode: "text" });
    expect(screen.getByLabelText("Title text")).toBeInTheDocument();
    expect(screen.getByLabelText("Art style")).toBeInTheDocument();
    expect(screen.queryByText("no images here")).not.toBeInTheDocument();
  });

  it("lists the season placeholders beside a seasonal row's poster text", () => {
    // The poster's text is filled with the season just as the name is, so a hint that leaves them out
    // hides the one thing a seasonal poster most wants to say.
    renderField({ mode: "text" }, 1, true);
    expect(screen.getByText("{season}")).toBeInTheDocument();
  });

  it("does not offer the season placeholders on an ordinary row's poster", () => {
    renderField({ mode: "text" });
    expect(screen.queryByText("{season}")).toBeNull();
  });

  it("tells a brand-new row to save first before uploading", () => {
    renderField({ mode: "upload" }, null);
    expect(screen.getByText(/save the row first/i)).toBeInTheDocument();
  });

  it("surfaces the provider gate only for AI when it can't make images", async () => {
    renderField({ mode: "ai" });
    expect(await screen.findByText(/no images here/)).toBeInTheDocument();
  });
});
