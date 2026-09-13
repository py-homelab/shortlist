import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WhatsNewDialog } from "@/components/layout/whats-new-dialog";
import type { WhatsNew } from "@/lib/types";

const { getWhatsNew, markWhatsNewSeen } = vi.hoisted(() => ({
  getWhatsNew: vi.fn(),
  markWhatsNewSeen: vi.fn((_version: string) => Promise.resolve({ ok: true })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getWhatsNew: () => getWhatsNew(),
    markWhatsNewSeen: (version: string) => markWhatsNewSeen(version),
  },
}));

function renderDialog() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <WhatsNewDialog />
    </QueryClientProvider>,
  );
}

// The shape of a real release body (`tests/fixtures/github_releases.json`): `###` sections, bold
// lead-ins, bullets, inline code, and an issue number in plain text.
const NOTES_180 = [
  "### Added",
  "",
  "- **A row can now take days off.** Set `When it appears` on a row. (#102)",
  "",
  "### Fixed",
  "",
  "- Read the [docs](https://example.com/docs) for more.",
].join("\n");

const RELEASE_180: WhatsNew["releases"][number] = {
  version: "1.8.0",
  url: "https://github.com/stevezau/shortlist/releases/tag/v1.8.0",
  published_at: "2026-08-26T09:57:25Z",
  notes: NOTES_180,
};

const ONE_RELEASE: WhatsNew = { version: "1.8.0", releases: [RELEASE_180] };

const TWO_RELEASES: WhatsNew = {
  version: "1.8.0",
  releases: [
    RELEASE_180,
    {
      version: "1.7.0",
      url: "https://github.com/stevezau/shortlist/releases/tag/v1.7.0",
      published_at: "2026-08-18T11:28:47Z",
      notes: "### Changed\n\n- Something older.",
    },
  ],
};

describe("WhatsNewDialog", () => {
  beforeEach(() => {
    getWhatsNew.mockReset();
    markWhatsNewSeen.mockClear();
  });

  it("opens with the newest release's notes rendered as formatted text", async () => {
    getWhatsNew.mockResolvedValue(ONE_RELEASE);
    renderDialog();

    const dialog = await screen.findByRole("dialog", {
      name: "What’s new in Shortlist 1.8.0",
    });
    expect(
      await within(dialog).findByRole("heading", { name: "Added" }),
    ).toBeTruthy();
    expect(
      within(dialog).getByText("A row can now take days off.").tagName,
    ).toBe("STRONG");
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(2);
    expect(within(dialog).getByText("When it appears").tagName).toBe("CODE");
  });

  it("shows nothing when there is nothing unread", async () => {
    getWhatsNew.mockResolvedValue({ version: "1.8.0", releases: [] });
    renderDialog();

    await waitFor(() => expect(getWhatsNew).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows nothing when the notes cannot be loaded", async () => {
    // A pop-up has no business announcing its own failure: the notes are a courtesy, and GitHub
    // being down is not something the owner needs to act on.
    getWhatsNew.mockRejectedValue(new Error("boom"));
    renderDialog();

    await waitFor(() => expect(getWhatsNew).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("lists every release since the owner last looked, newest first", async () => {
    getWhatsNew.mockResolvedValue(TWO_RELEASES);
    renderDialog();

    const dialog = await screen.findByRole("dialog");
    const versions = within(dialog)
      .getAllByRole("heading", { name: /^Shortlist \d/ })
      .map((heading) => heading.textContent);
    expect(versions).toEqual(["Shortlist 1.8.0", "Shortlist 1.7.0"]);
    expect(await within(dialog).findByText("Something older.")).toBeTruthy();
  });

  it("records the newest release it showed when closed with the button", async () => {
    getWhatsNew.mockResolvedValue(TWO_RELEASES);
    renderDialog();

    await userEvent.click(
      await screen.findByRole("button", { name: "Got it" }),
    );

    expect(markWhatsNewSeen).toHaveBeenCalledWith("1.8.0");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("records the release shown, not the running build, when the notes lag behind it", async () => {
    // The image publishes before CI creates its GitHub release. Recording the running 1.9.0 here
    // would mark 1.9.0's notes read before they exist.
    getWhatsNew.mockResolvedValue({ ...ONE_RELEASE, version: "1.9.0" });
    renderDialog();

    await userEvent.click(
      await screen.findByRole("button", { name: "Got it" }),
    );

    expect(markWhatsNewSeen).toHaveBeenCalledWith("1.8.0");
  });

  it("counts Escape as closing it for good", async () => {
    getWhatsNew.mockResolvedValue(ONE_RELEASE);
    renderDialog();
    await screen.findByRole("dialog");

    await userEvent.keyboard("{Escape}");

    expect(markWhatsNewSeen).toHaveBeenCalledWith("1.8.0");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("opens links in a new tab, away from the app", async () => {
    getWhatsNew.mockResolvedValue(ONE_RELEASE);
    renderDialog();

    const link = await screen.findByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com/docs");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(
      screen.getByRole("link", { name: "View on GitHub" }),
    ).toHaveAttribute("href", RELEASE_180.url);
  });

  it("never renders raw HTML or a script link from the notes", async () => {
    // A release body is editable on GitHub by anyone with write access to the repo; it is rendered
    // inside the owner's session, so it gets no more trust than markdown.
    getWhatsNew.mockResolvedValue({
      ...ONE_RELEASE,
      releases: [
        {
          ...RELEASE_180,
          notes:
            '<img src="x" onerror="alert(1)">\n\n[click](javascript:alert(1))',
        },
      ],
    });
    renderDialog();

    const dialog = await screen.findByRole("dialog");
    // Wait for the notes to render first: asserting "no <img>" before they load would pass on nothing.
    const link = await within(dialog).findByText("click");
    expect(link.closest("a")?.getAttribute("href")).toBe("");
    expect(dialog.querySelector("img")).toBeNull();
  });
});
