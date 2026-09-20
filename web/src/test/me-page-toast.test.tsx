/**
 * The picks page's confirmations, with the REAL sonner.
 *
 * `me-page.test.tsx` mocks `toast` and asserts it was called — which it was, for months, while the
 * page rendered no toaster at all (it lives outside the admin shell, which owns the app's only one).
 * So every confirmation on this page was dropped: a request filed from the grid said nothing, and
 * the deck only looked like it worked because the card flies away. A mocked toast cannot catch that;
 * this file renders the page for real and asks whether the words reach the screen.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { PickItem } from "@/lib/types";
import MePage from "@/pages/me";

const { getMe, getMySuggestions, getMyDismissed, act, seen, getSession } = vi.hoisted(() => ({
  getMe: vi.fn(),
  getMySuggestions: vi.fn(),
  getMyDismissed: vi.fn(),
  act: vi.fn(),
  seen: vi.fn(),
  getSession: vi.fn(),
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

const PICK: PickItem = {
  tmdb_id: 10,
  media_type: "movie",
  title: "Ten",
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

describe("MePage confirmations", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // Wide screen: the grid, where a tap gives no animation and the toast is the only feedback.
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (q: string) => ({ matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
    getSession.mockResolvedValue({ authenticated: true, role: "person", username: "sarah", admin: false });
    getMe.mockResolvedValue({
      state: "ok",
      name: "Sarah",
      account_id: 100,
      role: "person",
      built_at: null,
      rows: [],
      seerr: {
        linked: true,
        user_id: 7,
        quota: { movie: { limit: 0, remaining: null, restricted: false, days: null }, tv: { limit: 0, remaining: null, restricted: false, days: null } },
        error: null,
        configured: true,
      },
      counts: { items: 1, queued: 0, hidden_available: 0, family: 0 },
    });
    getMySuggestions.mockResolvedValue({
      state: "ok",
      items: [PICK],
      queued: [],
      hidden_available: 0,
      family: "exclude",
      has_family: false,
      genres: [{ name: "Drama", count: 1 }],
      built_at: null,
      seerr: null,
    });
    getMyDismissed.mockResolvedValue({ never: [], later: [] });
    seen.mockResolvedValue({ ok: true });
  });

  it("tells the person their request went in, from the grid", async () => {
    const user = userEvent.setup();
    act.mockResolvedValue({ ok: true, status: "pending", request_id: 5 });
    renderPage();
    await screen.findByText("Ten (2020)");

    await user.click(screen.getByRole("button", { name: "Request: Ten" }));

    expect(await screen.findByText("Requested — waiting for approval")).toBeInTheDocument();
  });

  it("says so when a request is approved outright", async () => {
    const user = userEvent.setup();
    act.mockResolvedValue({ ok: true, status: "approved", request_id: 5 });
    renderPage();
    await screen.findByText("Ten (2020)");

    await user.click(screen.getByRole("button", { name: "Request: Ten" }));

    expect(await screen.findByText("Requested and approved — on its way")).toBeInTheDocument();
  });

  it("shows the reason a request could not be filed", async () => {
    const user = userEvent.setup();
    act.mockResolvedValue({ ok: false, message: "quota reached — 0 left this week" });
    renderPage();
    await screen.findByText("Ten (2020)");

    await user.click(screen.getByRole("button", { name: "Request: Ten" }));

    expect(await screen.findByText("quota reached — 0 left this week")).toBeInTheDocument();
    // The title stays put: nothing was filed, so nothing should look as though it was.
    await waitFor(() => expect(screen.getByText("Ten (2020)")).toBeInTheDocument());
  });

  it("confirms a reject from the grid", async () => {
    const user = userEvent.setup();
    act.mockResolvedValue({ ok: true, dismissed: { kind: "never", until: null } });
    renderPage();
    await screen.findByText("Ten (2020)");

    await user.click(screen.getByRole("button", { name: "Reject: Ten" }));

    expect(await screen.findByText("Rejected — hidden from your picks")).toBeInTheDocument();
  });

  it("confirms a later from the grid, with when it comes back", async () => {
    const user = userEvent.setup();
    act.mockResolvedValue({ ok: true, dismissed: { kind: "later", until: "2026-10-20" } });
    renderPage();
    await screen.findByText("Ten (2020)");

    await user.click(screen.getByRole("button", { name: "Later: Ten" }));

    expect(await screen.findByText(/Watch later — back in \d+ days/)).toBeInTheDocument();
  });
});
