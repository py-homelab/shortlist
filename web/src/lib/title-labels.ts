/** "For Kids, Nature" <-> ["For Kids", "Nature"]. The server tidies and validates; this only splits. */
export function parseLabels(text: string): string[] {
  return text
    .split(",")
    .map((label) => label.trim())
    .filter(Boolean);
}
