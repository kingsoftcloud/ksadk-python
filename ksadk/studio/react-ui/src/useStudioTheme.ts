import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import {
  applyStudioTheme,
  readStudioThemePreference,
  resolveStudioTheme,
  STUDIO_DARK_MEDIA_QUERY,
  STUDIO_THEME_STORAGE_KEY,
  systemPrefersDark,
  type StudioThemePreference,
} from "./studioTheme";

export function useStudioTheme() {
  const [preference, setPreference] = useState<StudioThemePreference>(readStudioThemePreference);
  const [systemDark, setSystemDark] = useState(systemPrefersDark);
  const resolvedTheme = resolveStudioTheme(preference, systemDark);

  useEffect(() => {
    const media = window.matchMedia(STUDIO_DARK_MEDIA_QUERY);
    const handleChange = () => setSystemDark(media.matches);
    handleChange();
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, []);

  useLayoutEffect(() => {
    applyStudioTheme(resolvedTheme);
  }, [resolvedTheme]);

  const updatePreference = useCallback((next: StudioThemePreference) => {
    setPreference(next);
    try {
      window.localStorage.setItem(STUDIO_THEME_STORAGE_KEY, next);
    } catch {
      // Storage can be unavailable in privacy-restricted browser contexts.
    }
  }, []);

  return { preference, resolvedTheme, setPreference: updatePreference };
}
