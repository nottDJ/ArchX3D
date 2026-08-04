"use client";

/**
 * ArchX3D — theme hook
 * ====================
 * Reads and sets the theme, and follows the system when asked to.
 *
 * `mounted` exists because the server cannot know the stored theme, so the
 * first client render must match the server's markup or React logs a
 * hydration mismatch. Consumers use it to defer rendering the *indicator*
 * (which icon is highlighted) — never the content, which is themed by CSS and
 * is already correct thanks to the pre-paint script.
 */

import { useCallback, useEffect, useState } from "react";

import {
  applyTheme,
  readStoredTheme,
  resolveTheme,
  storeTheme,
  type ResolvedTheme,
  type Theme,
} from "@/lib/theme";

export function useTheme(): {
  theme: Theme;
  resolved: ResolvedTheme;
  setTheme: (theme: Theme) => void;
  mounted: boolean;
} {
  const [theme, setThemeState] = useState<Theme>("system");
  const [resolved, setResolved] = useState<ResolvedTheme>("dark");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const stored = readStoredTheme();
    setThemeState(stored);
    setResolved(applyTheme(stored));
    setMounted(true);
  }, []);

  // Following the system means following it *live* — a user whose OS switches
  // at sunset expects the app to switch with it, not on next reload.
  useEffect(() => {
    if (theme !== "system" || typeof window === "undefined") return;

    const media = window.matchMedia("(prefers-color-scheme: light)");
    const handle = () => setResolved(applyTheme("system"));

    media.addEventListener("change", handle);
    return () => media.removeEventListener("change", handle);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    storeTheme(next);
    setResolved(applyTheme(next));
  }, []);

  return { theme, resolved: mounted ? resolved : resolveTheme(theme), setTheme, mounted };
}
