import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { PickItem } from "@/lib/types";
import MePage from "@/pages/me";

const { toastFn } = vi.hoisted(() => ({ toastFn: vi.fn() }));
vi.mock("sonner", () => ({ toast: toastFn }));

const { getMe, getMySuggestions, getMyDismissed, act, seen, getSession } =
  vi.hoisted(() => ({
    getMe: vi.fn(),
    getMySuggestions: vi.fn(),
    getMyDismissed: vi.fn(),
    act: vi.fn(),
    seen: vi.fn((_body: unknown) => Promise.resolve({ ok: true, logged: 0 })),
    getSession: vi.fn(() =>
      Promise.resolve({
        authenticated: true,
        login_required: true,
        account_id: 100,
        username: "sarah",
        role: "person",
      }),
    ),
  }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getMe: () => getMe(),
      getMySuggestions: (family: string) => getMySuggestions(family),
      getMyDismissed: () => getMyDismissed(),
      act: (body: unknown) => act(body),
      seen: (body: unknown) => seen(body),
      getSession: () => getSession(),
      logout: vi.fn(() => Promise.resolve()),
    },
  };
});

function item(over: Partial<PickItem>): PickItem {
  return {
    tmdb_id: 1,
    media_type: "movie",
    title: "Title",
    year: 2020,
    rating: 7.5,
    vote_count: 1000,
    overview: "A film.",
    language: "en",
    poster_path: null,
    genres: ["Drama"],
    reason: "Because you watched Fargo",
    seed_title: "Fargo",
    kids: false,
    rank: 1,
    source: "engine:e",
    seerr: { status: null, request_id: null },
    requestable: true,
    reason_not_requestable: null,
    ...over,
  };
}

const ME = {
  state: "ok",
  name: "Sarah",
  account_id: 100,
  role: "person",
  built_at: null,
  rows: [],
  seerr: { linked: true, user_id: 7, quota: { movie: { limit: 0, remaining: null, restricted: false, days: null }, tv: { limit: 5, remaining: 4, restricted: false, days: 7 } }, error: null, configured: true },
  counts: { items: 2, queued: 0, hidden_available: 0, family: 0 },
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/me"]}>
        <MePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("MePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // Wide screen: the grid, where every card carries its buttons.
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (q: string) => ({ matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
    getMe.mockResolvedValue(ME);
    getMySuggestions.mockResolvedValue({
      state: "ok",
      items: [
        item({ tmdb_id: 10, title: "Ten", genres: ["Drama"], rank: 1 }),
        item({ tmdb_id: 20, title: "Twenty", media_type: "show", genres: ["Comedy"], rank: 2, rating: 8.9 }),
      ],
      queued: [item({ tmdb_id: 30, title: "Thirty", requestable: false, reason_not_requestable: "requested" })],
      hidden_available: 1,
      family: "exclude",
      has_family: true,
      genres: [{ name: "Drama", count: 1 }, { name: "Comedy", count: 1 }],
      built_at: null,
      seerr: ME.seerr,
    });
    getMyDismissed.mockResolvedValue({ never: [], later: [] });
  });

  it("shows the person's list in the engine's order, their quota and the queued titles", async () => {
    renderPage();
    expect(await screen.findByText("Ten (2020)")).toBeInTheDocument();
    const titles = screen.getAllByText(/\(2020\)$/).map((el) => el.textContent);
    expect(titles.slice(0, 2)).toEqual(["Ten (2020)", "Twenty (2020)"]);
    expect(screen.getByText(/📺 4\/5/)).toBeInTheDocument();
    expect(screen.getByText("1 already requested or on the way")).toBeInTheDocument();
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
    expect(getMySuggestions).toHaveBeenCalledWith("auto");
  });

  it("filters by type and genre, and sorts", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Ten (2020)");
    await user.click(screen.getByRole("button", { name: "TV" }));
    expect(screen.queryByText("Ten (2020)")).not.toBeInTheDocument();
    expect(screen.getByText("Twenty (2020)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "All" }));
    await user.selectOptions(screen.getByLabelText("Genre"), "Comedy");
    expect(screen.queryByText("Ten (2020)")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Genre"), "");
    await user.selectOptions(screen.getByLabelText("Sort"), "rating");
    const sorted = screen.getAllByText(/\(2020\)$/).map((el) => el.textContent);
    expect(sorted.slice(0, 2)).toEqual(["Twenty (2020)", "Ten (2020)"]);
  });

  it("never hides the title, records the position it held, and undo brings it back", async () => {
    const user = userEvent.setup();
    act.mockResolvedValue({ ok: true, dismissed: { kind: "never", until: null } });
    renderPage();
    await screen.findByText("Ten (2020)");
    await user.click(screen.getByRole("button", { name: "Reject: Twenty" }));
    await waitFor(() => expect(screen.queryByText("Twenty (2020)")).not.toBeInTheDocument());
    expect(act).toHaveBeenCalledWith({ action: "never", tmdb_id: 20, media_type: "show", surface: "grid", position: 1 });
    expect(toastFn).toHaveBeenCalledWith("Rejected — hidden from your picks");
  });

  it("a request goes out as the person and moves the title to queued", async () => {
    const user = userEvent.setup();
    act.mockResolvedValue({ ok: true, status: "pending", request_id: 5 });
    renderPage();
    await screen.findByText("Ten (2020)");
    await user.click(screen.getByRole("button", { name: "Request: Ten" }));
    await waitFor(() => expect(act).toHaveBeenCalledWith(expect.objectContaining({ action: "request", tmdb_id: 10 })));
    expect(toastFn).toHaveBeenCalledWith("Requested — waiting for approval");
    expect(screen.getByText("2 already requested or on the way")).toBeInTheDocument();
  });

  it("a refused action puts the title back", async () => {
    const user = userEvent.setup();
    act.mockRejectedValue(new Error("nope"));
    renderPage();
    await screen.findByText("Ten (2020)");
    await user.click(screen.getByRole("button", { name: "Later: Ten" }));
    await waitFor(() => expect(toastFn).toHaveBeenCalled());
    expect(screen.getByText("Ten (2020)")).toBeInTheDocument();
  });

  it("the family lane is a separate fetch", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Ten (2020)");
    await user.click(screen.getByRole("button", { name: /Family/ }));
    await waitFor(() => expect(getMySuggestions).toHaveBeenCalledWith("only"));
  });

  it("the hidden panel lists dismissals and restores one", async () => {
    const user = userEvent.setup();
    getMyDismissed.mockResolvedValue({
      never: [{ tmdb_id: 5, media_type: "movie", title: "Gone", year: 2001, until: null, created_at: "2026-09-18T00:00:00Z" }],
      later: [],
    });
    act.mockResolvedValue({ ok: true, restored: { tmdb_id: 5, media_type: "movie", kind: "never", title: "Gone" } });
    renderPage();
    await screen.findByText("Ten (2020)");
    await user.click(screen.getByRole("button", { name: "hidden" }));
    const panel = await screen.findByText("Hidden titles");
    expect(within(panel.parentElement!.parentElement!).getByText(/Gone \(2001\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "restore" }));
    await waitFor(() => expect(act).toHaveBeenCalledWith({ action: "undo", tmdb_id: 5, media_type: "movie" }));
  });

  it("says so when there are no picks yet", async () => {
    getMe.mockResolvedValue({ ...ME, state: "no_picks", counts: { items: 0, queued: 0, hidden_available: 0, family: 0 } });
    getMySuggestions.mockResolvedValue({ state: "no_picks", items: [], queued: [], hidden_available: 0, family: "exclude", has_family: false, genres: [], built_at: null, seerr: ME.seerr });
    renderPage();
    expect(await screen.findByText("No picks yet")).toBeInTheDocument();
  });
});
