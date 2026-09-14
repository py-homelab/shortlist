import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RowRenamePage } from "@/pages/row-rename";

const { listCollections, updateCollection } = vi.hoisted(() => ({
  listCollections: vi.fn(),
  updateCollection: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, listCollections, updateCollection } };
});

function streamOf(events: object[]) {
  const body = new TextEncoder().encode(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(""));
  let sent = false;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => (sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: body })),
      }),
    },
  };
}

function renderRename() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[{ pathname: "/rows/7/rename", state: { proposedName: "New Name" } }]}>
        <Routes>
          <Route path="/rows/:id/rename" element={<RowRenamePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RowRenamePage — what each person's rename came to", () => {
  beforeEach(() => {
    Element.prototype.scrollTo = vi.fn(); // jsdom has no layout, so no scrolling
    listCollections.mockResolvedValue([{ id: 7, slug: "comedy", name: "Old Name", name_template: "Old Name" }]);
    updateCollection.mockResolvedValue({});
  });
  afterEach(() => vi.unstubAllGlobals());

  it("keeps going past one person Plex refused, and says why in plain words", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamOf([
          {
            user: "sarah",
            display_name: "Sarah",
            library: "Movies",
            error: "Plex refused 'New Name' in Movies: something in that library already has that name.",
          },
          { user: "mike", display_name: "Mike", old: "Old Name", new: "New Name", libraries: ["Movies"] },
          { done: true, total: 1 },
        ]),
      ),
    );

    renderRename();

    const log = await screen.findByRole("list", { name: /rename results/i });
    expect(within(log).getByText(/Mike/)).toBeInTheDocument();
    expect(within(log).getByText(/Sarah: Plex refused/)).toBeInTheDocument();
    expect(await screen.findByText(/renamed 1 collection on Plex/i)).toBeInTheDocument();
    expect(screen.getByText(/1 could not be renamed/i)).toBeInTheDocument();
  });

  it("says a row that takes its name at the next run is not renamed yet", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamOf([
          {
            user: "sarah",
            display_name: "Sarah",
            old: "Old Name",
            new: "New Name",
            libraries: ["Movies"],
            next_run: true,
          },
          { done: true, total: 0 },
        ]),
      ),
    );

    renderRename();

    const log = await screen.findByRole("list", { name: /rename results/i });
    expect(within(log).getByText(/at this row's next run/i)).toBeInTheDocument();
    expect(screen.queryByText(/Nothing to rename/i)).not.toBeInTheDocument();
  });

  it("still stops on an error that is not about one person", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(streamOf([{ error: "Plex isn't reachable." }, { done: true, total: 0 }])),
    );

    renderRename();

    expect(await screen.findByText("Plex isn't reachable.")).toBeInTheDocument();
  });
});
