/** `RowSpec.family` — mirrors the engine's `FAMILY_MODES`. "include" is the default and how every
 *  row behaved before the setting existed. */
export type FamilyMode = "include" | "exclude" | "only";

export const FAMILY_MODES: readonly FamilyMode[] = ["include", "exclude", "only"];

export function asFamily(value: unknown): FamilyMode {
  return value === "exclude" || value === "only" ? value : "include";
}

export const FAMILY_LABELS: Record<FamilyMode, string> = {
  include: "Mixed in with everything else",
  exclude: "Left out of this row",
  only: "This row is the family row",
};

export const FAMILY_HINTS: Record<FamilyMode, string> = {
  include: "Children’s titles rank alongside everything else, as they always have.",
  exclude:
    "Nothing tagged as children’s or family viewing reaches this row — for the grown-ups’ rows on an account the whole household watches under.",
  only: "Only children’s and family titles, ranked for this person — one shelf for the kids on a shared account.",
};
