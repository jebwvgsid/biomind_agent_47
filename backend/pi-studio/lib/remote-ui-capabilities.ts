/** Unknown/failed profiles never expose ordinary Studio control surfaces. */
export function studioControlsAllowed(value: unknown): boolean {
  return !!value && typeof value === "object" && "profile" in value && value.profile === "pi";
}
