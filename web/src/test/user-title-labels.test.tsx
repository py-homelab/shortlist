/** The owner's own admit / hide labels on a person's page. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UserTitleLabels } from "@/components/user-detail/user-title-labels";
import type * as ApiModule from "@/lib/api";
import { parseLabels } from "@/lib/title-labels";
import type { User } from "@/lib/types";

const { patchUser } = vi.hoisted(() => ({ patchUser: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { ...actual.api, patchUser: (id: number, patch: unknown) => patchUser(id, patch) },
  };
});

function person(over: Partial<User> = {}): User {
  return {
    id: 17,
    username: "Kids",
    slug: "kids",
    manage_sharing: true,
    restricted: true,
    restriction_profile: "",
    prefs: {},
    ...over,
  } as unknown as User;
}

function renderCard(user: User) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <UserTitleLabels user={user} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  patchUser.mockResolvedValue(person());
});

describe("parseLabels", () => {
  it("splits on commas and drops the blanks", () => {
    expect(parseLabels(" For Kids , ,Nature,")).toEqual(["For Kids", "Nature"]);
    expect(parseLabels("")).toEqual([]);
  });
});

describe("UserTitleLabels", () => {
  it("sends both lists as the person's prefs", async () => {
    renderCard(person());

    await userEvent.type(screen.getByLabelText(/also show titles labelled/i), "For Kids, Nature");
    await userEvent.type(screen.getByLabelText(/never show titles labelled/i), "Not For Kids");
    await userEvent.click(screen.getByRole("button", { name: /save labels/i }));

    expect(patchUser).toHaveBeenCalledWith(17, {
      prefs: { admit_labels: ["For Kids", "Nature"], hide_labels: ["Not For Kids"] },
    });
    expect(
      await screen.findByText(/updating this account.s plex restriction now/i),
    ).toBeInTheDocument();
  });

  it("opens with what is already set, and has nothing to save until it changes", () => {
    renderCard(person({ prefs: { admit_labels: ["For Kids"], hide_labels: ["Scary"] } } as Partial<User>));

    expect(screen.getByLabelText(/also show titles labelled/i)).toHaveValue("For Kids");
    expect(screen.getByLabelText(/never show titles labelled/i)).toHaveValue("Scary");
    expect(screen.getByRole("button", { name: /save labels/i })).toBeDisabled();
  });

  it("clearing a field sends an empty list, which is what takes the label back out", async () => {
    renderCard(person({ prefs: { admit_labels: ["For Kids"] } } as Partial<User>));

    await userEvent.clear(screen.getByLabelText(/also show titles labelled/i));
    await userEvent.click(screen.getByRole("button", { name: /save labels/i }));

    expect(patchUser).toHaveBeenCalledWith(17, { prefs: { admit_labels: [], hide_labels: [] } });
  });

  it("shows the server's reason when a label can't be used", async () => {
    patchUser.mockRejectedValue(
      Object.assign(new Error("422"), { status: 422, detail: "'Rock & Roll' can't be used as a label here" }),
    );
    renderCard(person());

    await userEvent.type(screen.getByLabelText(/also show titles labelled/i), "Rock & Roll");
    await userEvent.click(screen.getByRole("button", { name: /save labels/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("says why it is off for an account whose sharing Shortlist leaves alone", () => {
    renderCard(person({ manage_sharing: false }));

    expect(screen.getByText(/leave this account's plex sharing alone/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/also show titles labelled/i)).toBeDisabled();
  });

  it("says why it is off for an account on one of Plex's restriction presets", () => {
    renderCard(person({ restriction_profile: "older_kid" }));

    expect(screen.getByText(/built-in restriction presets/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save labels/i })).toBeDisabled();
  });

  it("is off on the owner's own page, where Plex restricts nothing", () => {
    renderCard(person({ user_type: "owner" } as Partial<User>));

    expect(screen.getByText(/never restricts the server owner/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save labels/i })).toBeDisabled();
  });
});
