import { describe, expect, it } from "vitest";

import {
  isNightly,
  seasonDate,
  seasonStatusLine,
  seasonWindowLabel,
} from "@/lib/seasons";

const HALLOWEEN = { month: 10, day: 31 };
const CHRISTMAS = { month: 12, day: 25 };
const VALENTINES = { month: 2, day: 14 };

describe("seasonWindowLabel", () => {
  it("runs from its lead through its day", () => {
    expect(seasonWindowLabel(HALLOWEEN, 30, 0)).toBe(
      `${seasonDate("2026-10-01")} – ${seasonDate("2026-10-31")}`,
    );
  });

  it("stays up for the days after", () => {
    expect(seasonWindowLabel(CHRISTMAS, 0, 1)).toBe(
      `${seasonDate("2026-12-25")} – ${seasonDate("2026-12-26")}`,
    );
  });

  it("starts in December when a long lead crosses New Year", () => {
    expect(seasonWindowLabel(VALENTINES, 60, 0)).toBe(
      `${seasonDate("2026-12-16")} – ${seasonDate("2027-02-14")}`,
    );
  });
});

describe("seasonStatusLine", () => {
  const halloween = {
    slug: "halloween",
    name: "Halloween",
    emoji: "🎃",
    starts: "2026-10-01",
    ends: "2026-10-31",
  };
  const christmas = {
    slug: "christmas",
    name: "Christmas",
    emoji: "🎄",
    starts: "2026-11-25",
    ends: "2026-12-25",
  };

  it("names the season on screen and when it comes down", () => {
    expect(seasonStatusLine({ showing: halloween, next: christmas })).toBe(
      `Showing 🎃 Halloween until ${seasonDate("2026-10-31")}`,
    );
  });

  it("names what a hidden row is waiting for", () => {
    expect(seasonStatusLine({ showing: null, next: christmas })).toBe(
      `Hidden until 🎄 Christmas starts on ${seasonDate("2026-11-25")}`,
    );
  });

  it("says nothing for a row that follows no season", () => {
    expect(seasonStatusLine(null)).toBe("");
  });
});


describe("isNightly", () => {
  it("is true only for a schedule that runs every day", () => {
    expect(isNightly("30 3 * * *")).toBe(true);
    expect(isNightly("30 3 * * 0")).toBe(false);
    expect(isNightly("")).toBe(false);
  });
});
