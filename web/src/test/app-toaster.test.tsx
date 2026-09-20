/**
 * The toast area, on both surfaces, with the REAL sonner.
 *
 * The admin shell and the picks page each mount their own — the page lives outside the shell — and a
 * missing mount is invisible: `toast()` still resolves, the suite stays green, and nothing reaches
 * the screen. That is exactly how the picks page went without confirmations. Rendering the shared
 * component and firing a real toast is the only check that can tell the two apart.
 */
import { render, screen } from "@testing-library/react";
import { toast } from "sonner";
import { describe, expect, it } from "vitest";

import { AppToaster } from "@/components/app-toaster";

describe("AppToaster", () => {
  it("puts a toast on the screen", async () => {
    render(<AppToaster />);

    toast("Saved your settings");

    expect(await screen.findByText("Saved your settings")).toBeInTheDocument();
  });

  it("does the same where the picks page mounts it, from the top", async () => {
    // The deck's action row owns the bottom of that viewport, so its toasts come from the top.
    render(<AppToaster position="top-center" />);

    toast("Requested — waiting for approval");

    expect(await screen.findByText("Requested — waiting for approval")).toBeInTheDocument();
  });
});
