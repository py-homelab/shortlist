import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UserAvatar } from "@/components/user-avatar";

describe("UserAvatar initials", () => {
  it("takes initials from the name's words, not from bracketed notes or punctuation", () => {
    // Plex display names carry notes: "Joe - Richard's Mate (P)" rendered "J(", and "Katey (Sarah/Eric
    // Friend) (P)" rendered "K(" — a bracket where a letter belongs.
    render(<UserAvatar name="Joe - Richard's Mate (P)" labelled />);
    expect(screen.getByRole("img", { name: "Joe - Richard's Mate (P)" })).toHaveTextContent(/^JM$/);
  });

  it("uses a name that is only a first name plus notes as one word", () => {
    render(<UserAvatar name="Katey (Sarah/Eric Friend) (P)" labelled />);
    expect(screen.getByRole("img", { name: "Katey (Sarah/Eric Friend) (P)" })).toHaveTextContent(/^KA$/);
  });

  it("keeps a one-word name's first two letters", () => {
    render(<UserAvatar name="moohouse" labelled />);
    expect(screen.getByRole("img", { name: "moohouse" })).toHaveTextContent(/^MO$/);
  });

  it("falls back to a question mark for a name with no letters at all", () => {
    render(<UserAvatar name="(!)" labelled />);
    expect(screen.getByRole("img", { name: "(!)" })).toHaveTextContent(/^\?$/);
  });
});
