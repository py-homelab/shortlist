import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FamilyHouseholdsCard } from "@/components/settings/family-households-card";
import { householdSummary } from "@/components/user-detail/user-household";
import type { Settings, User } from "@/lib/types";

const { putSettings } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));
vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, fallback: string) => fallback,
  api: { putSettings },
}));

function renderCard(settings: Settings) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <FamilyHouseholdsCard settings={settings} />
    </QueryClientProvider>,
  );
}

describe("FamilyHouseholdsCard", () => {
  beforeEach(() => putSettings.mockClear());

  it("shows the defaults as whole numbers and percentages", () => {
    renderCard({});
    expect(screen.getByLabelText("A family at")).toHaveValue(15);
    expect(screen.getByLabelText("…from at least")).toHaveValue(4);
    expect(screen.getByLabelText("A child’s own account above")).toHaveValue(80);
    expect(screen.getByLabelText("Decide only after")).toHaveValue(10);
  });

  it("saves a changed threshold as a fraction", async () => {
    renderCard({ "family.min_share": 0.15 });
    fireEvent.change(screen.getByLabelText("A family at"), { target: { value: "25" } });
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith(expect.objectContaining({ "family.min_share": 0.25 })),
    );
  });
});

describe("householdSummary", () => {
  it("says what the last run decided and from what", () => {
    const hh = { label: "family", source: "engine", kids_titles: 66, window_titles: 187, window_days: 365 };
    expect(householdSummary(hh as User["household"])).toBe(
      "Last run: a family sharing the account with children — 66 children’s titles of 187 watched in the last 12 months.",
    );
    expect(householdSummary({ label: "adult", source: "override" } as User["household"])).toBe(
      "Set by you: grown-up viewing.",
    );
    expect(householdSummary(null)).toMatch(/Not decided yet/);
  });
});
