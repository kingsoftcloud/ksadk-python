export type StudioViewportMode = "compact" | "laptop" | "desktop" | "wide";

export function viewportModeForWidth(width: number): StudioViewportMode {
  if (width <= 1023) return "compact";
  if (width <= 1439) return "laptop";
  if (width <= 1919) return "desktop";
  return "wide";
}
