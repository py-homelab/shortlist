/** `RowSpec.family` — mirrors the engine's `FAMILY_MODES`. "include" is the default and how every
 *  row behaved before the setting existed. */
export type FamilyMode = "auto" | "include" | "exclude" | "only";

export const FAMILY_MODES: readonly FamilyMode[] = ["auto", "include", "exclude", "only"];

export function asFamily(value: unknown): FamilyMode {
  return value === "exclude" || value === "only" || value === "auto" ? value : "include";
}

export const FAMILY_LABELS: Record<FamilyMode, string> = {
  auto: "Decided per person",
  include: "Mixed in with everything else",
  exclude: "Left out of this row",
  only: "This row is the family row",
};

export const FAMILY_HINTS: Record<FamilyMode, string> = {
  auto: "Left out for a family sharing the account with children (they get a family row); the only thing in the row for a child’s own account; kept alongside everything else for anyone else. Who is which is decided from each person’s own viewing, or set by you — see their user page.",
  include: "Children’s titles rank alongside everything else, as they always have.",
  exclude:
    "Nothing tagged as children’s or family viewing reaches this row — for the grown-ups’ rows on an account the whole household watches under. Not built for a child’s own account.",
  only: "Only children’s and family titles, ranked for this person — built only for people who are a family sharing their account with children. A child’s own account doesn’t get it: its other rows already hold only children’s titles.",
};
