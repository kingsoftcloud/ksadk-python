export type StudioThemePreference = "system" | "light" | "dark";
export type ResolvedStudioTheme = "light" | "dark";

export const STUDIO_THEME_STORAGE_KEY = "agentkit-studio-theme";
export const STUDIO_DARK_MEDIA_QUERY = "(prefers-color-scheme: dark)";

export function normalizeThemePreference(value: unknown): StudioThemePreference {
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

export function resolveStudioTheme(
  preference: StudioThemePreference,
  systemDark: boolean,
): ResolvedStudioTheme {
  return preference === "system" ? (systemDark ? "dark" : "light") : preference;
}

export function readStudioThemePreference(): StudioThemePreference {
  try {
    return normalizeThemePreference(window.localStorage.getItem(STUDIO_THEME_STORAGE_KEY));
  } catch {
    return "system";
  }
}

export function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia(STUDIO_DARK_MEDIA_QUERY).matches;
}

export function applyStudioTheme(theme: ResolvedStudioTheme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
}

export function initializeStudioTheme(): StudioThemePreference {
  const preference = readStudioThemePreference();
  applyStudioTheme(resolveStudioTheme(preference, systemPrefersDark()));
  return preference;
}
