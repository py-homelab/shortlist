import { describe, expect, it } from "vitest";

import {
  fillPlaceholders,
  PLACEHOLDER_EXACT,
  PLACEHOLDER_SPLIT,
  PLACEHOLDERS,
  usesSeason,
} from "@/lib/placeholders";

describe("usesSeason", () => {
  it("spots either placeholder", () => {
    expect(usesSeason("{season} picks")).toBe(true);
    expect(usesSeason("{season_emoji} picks")).toBe(true);
    expect(usesSeason("{top_seed} picks")).toBe(false);
  });
});

describe("the placeholder list", () => {
  it("is the engine's list, in the engine's spelling", () => {
    // shortlist/engine/placeholders.py names exactly these; tests/unit/test_placeholders.py pins its side.
    expect(PLACEHOLDERS.map((p) => p.token)).toEqual([
      "{user}",
      "{library_name}",
      "{top_seed}",
      "{season}",
      "{season_emoji}",
    ]);
  });

  it("splits a name around exactly those tokens, and nothing that only looks like one", () => {
    const parts = "{Season} and {season_emoji} {season} for {genre}".split(PLACEHOLDER_SPLIT);
    expect(parts.filter((part) => PLACEHOLDER_EXACT.test(part))).toEqual(["{season_emoji}", "{season}"]);
  });

  it("fills every token and leaves the whitespace alone", () => {
    const values = { topSeed: "Fargo", user: "Sarah", libraryName: "", season: { name: "Christmas", emoji: "🎄" } };
    expect(fillPlaceholders("{season_emoji} {season}  for {user} {library_name}\n{top_seed}", values)).toBe(
      "🎄 Christmas  for Sarah \nFargo",
    );
  });
});
