import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationsSection } from "@/components/settings/notifications-section";
import type { Settings } from "@/lib/types";

const { putSettings, testConnection } = vi.hoisted(() => ({
  putSettings: vi.fn((v: Settings) => Promise.resolve(v)),
  testConnection: vi.fn(() =>
    Promise.resolve({ ok: true, message: "Sent — your webhook answered 204." }),
  ),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_e: unknown, f: string) => f,
  api: { putSettings, testConnection },
}));

function renderSection(settings: Settings) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <NotificationsSection settings={settings} />
    </QueryClientProvider>,
  );
}

describe("NotificationsSection", () => {
  beforeEach(() => {
    putSettings.mockClear();
    testConnection.mockClear();
  });

  it("hides the address field until the owner turns notifications on", () => {
    renderSection({});
    expect(screen.queryByLabelText(/Webhook address/i)).toBeNull();
    // The off state still says where a failure DOES show up, rather than leaving a bare switch.
    expect(screen.getByText(/shows up in the bell/i)).toBeTruthy();
  });

  it("saves the switch and reveals the address field", async () => {
    renderSection({});
    fireEvent.click(
      screen.getByRole("switch", { name: /Send alerts to a webhook/i }),
    );
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith({
        "notify.webhook.enabled": true,
      }),
    );
    expect(screen.getByLabelText(/Webhook address/i)).toBeTruthy();
  });

  it("puts the switch back when the save fails", async () => {
    // A switch stuck on "on" over a server that still says off is worse than a switch that refuses:
    // the owner walks away believing they'll be told when a run fails.
    putSettings.mockRejectedValueOnce(new Error("nope"));
    renderSection({});
    const toggle = screen.getByRole("switch", {
      name: /Send alerts to a webhook/i,
    });
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/Couldn’t save/i),
    );
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(screen.queryByLabelText(/Webhook address/i)).toBeNull();
  });

  it("shows a saved address as dots and never re-sends them", async () => {
    // The URL is a credential, so the server returns it redacted. Pressing Save without retyping
    // must be a no-op — sending the dots back would overwrite the real address with "•••••".
    renderSection({
      "notify.webhook.enabled": true,
      "notify.webhook.url": "•••••",
    });
    const field = screen.getByLabelText(/Webhook address/i);
    expect(field).toHaveProperty("value", "•••••");
    expect(field).toHaveProperty("type", "password");
    expect(screen.getByRole("button", { name: /^Save$/i })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("labels the button for what pressing it actually does", async () => {
    // "Test" would understate it: this one posts a real message into the owner's chat channel.
    renderSection({
      "notify.webhook.enabled": true,
      "notify.webhook.url": "•••••",
    });
    const send = screen.getByRole("button", { name: /Send a test/i });
    fireEvent.click(send);
    // WHICH service is the one thing this component picks, so assert it — "was called" would pass
    // just as happily if the button regressed to pinging the AI provider.
    await waitFor(() => expect(testConnection).toHaveBeenCalledWith("notify"));
    expect(await screen.findByText(/answered 204/i)).toBeTruthy();
  });

  describe("choosing what gets sent", () => {
    const on = { "notify.webhook.enabled": true, "notify.webhook.url": "•••••" };

    it("ticks the events the server says are switched on", () => {
      renderSection({
        ...on,
        "notify.webhook.events": ["run.failed", "privacy.exposure"],
      });
      expect(
        screen.getByRole("checkbox", { name: /A run failed/i }),
      ).toHaveProperty("checked", true);
      expect(
        screen.getByRole("checkbox", { name: /Someone can see a row/i }),
      ).toHaveProperty("checked", true);
      expect(
        screen.getByRole("checkbox", { name: /A run started/i }),
      ).toHaveProperty("checked", false);
    });

    it("hides the list until notifications are on", () => {
      renderSection({ "notify.webhook.events": ["run.failed"] });
      expect(screen.queryByRole("checkbox", { name: /A run failed/i })).toBeNull();
    });

    it("saves the whole list when one event is ticked", async () => {
      renderSection({ ...on, "notify.webhook.events": ["run.failed"] });
      fireEvent.click(screen.getByRole("checkbox", { name: /A job failed/i }));
      await waitFor(() =>
        expect(putSettings).toHaveBeenCalledWith({
          "notify.webhook.events": ["run.failed", "job.failed"],
        }),
      );
    });

    it("puts the tick back when the save fails", async () => {
      putSettings.mockRejectedValueOnce(new Error("nope"));
      renderSection({ ...on, "notify.webhook.events": ["run.failed"] });
      const box = screen.getByRole("checkbox", { name: /A run failed/i });
      fireEvent.click(box);
      await waitFor(() =>
        expect(screen.getByRole("alert").textContent).toMatch(/Couldn’t save/i),
      );
      expect(box).toHaveProperty("checked", true);
    });

    it("says routine jobs only report a failure", () => {
      renderSection(on);
      expect(screen.getByText(/privacy sync and playback credits/i)).toBeTruthy();
    });
  });

  describe("authentication", () => {
    const on = { "notify.webhook.enabled": true, "notify.webhook.url": "•••••" };

    it("stays folded away until the owner asks for it", () => {
      renderSection(on);
      expect(screen.queryByLabelText(/Header name/i)).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: /Add authentication/i }));
      expect(screen.getByLabelText(/Header name/i)).toHaveProperty(
        "value",
        "Authorization",
      );
      expect(screen.getByLabelText(/Header value/i)).toHaveProperty(
        "type",
        "password",
      );
    });

    it("is already open, with the value as dots, when one is saved", () => {
      renderSection({
        ...on,
        "notify.webhook.auth_header_name": "X-Gotify-Key",
        "notify.webhook.auth_header_value": "•••••",
      });
      expect(screen.getByLabelText(/Header name/i)).toHaveProperty(
        "value",
        "X-Gotify-Key",
      );
      expect(screen.getByLabelText(/Header value/i)).toHaveProperty(
        "value",
        "•••••",
      );
    });

    it("saves a header name as typed", async () => {
      renderSection(on);
      fireEvent.click(screen.getByRole("button", { name: /Add authentication/i }));
      const name = screen.getByLabelText(/Header name/i);
      fireEvent.change(name, { target: { value: "X-Api-Key" } });
      const group = name.closest("div.rounded-lg") as HTMLElement;
      fireEvent.click(within(group).getByRole("button", { name: /^Save$/i }));
      await waitFor(() =>
        expect(putSettings).toHaveBeenCalledWith({
          "notify.webhook.auth_header_name": "X-Api-Key",
        }),
      );
    });
  });
});
