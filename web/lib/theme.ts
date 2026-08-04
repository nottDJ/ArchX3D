/**
 * ArchX3D — theme
 * ===============
 * Light, dark, or follow the system.
 *
 * The product was dark-only, and dark-only by the worst method: every page
 * hard-coded `data-theme="dark"` on its own root and every colour was a
 * literal. Architects work in daylight studios as often as at night, and the
 * one thing a professional tool must not do is force a preference.
 *
 * Avoiding the flash
 * ------------------
 * Theme lives in `localStorage`, which the server cannot read, so a
 * server-rendered page cannot know which to send. Rendering the wrong one and
 * correcting on hydration produces a white flash on every navigation for dark
 * users — the single most-complained-about bug in themed apps.
 *
 * `THEME_SCRIPT` is injected into `<head>` and runs *before first paint*,
 * setting the attribute from storage. It is deliberately tiny and dependency
 * free, because it blocks rendering.
 */

export type Theme = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "archx3d.theme";

/**
 * Runs before paint. Kept as a string because it must be inlined, not
 * imported — a module would be fetched after the first paint it exists to
 * prevent.
 *
 * Wrapped in try/catch: `localStorage` throws in some privacy modes, and a
 * theme preference is never worth a blank page.
 */
export const THEME_SCRIPT = `
(function(){try{
  var stored = localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});
  var theme = stored === 'light' || stored === 'dark' ? stored
    : (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.setAttribute('data-theme', theme);
}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();
`.trim();

export function readStoredTheme(): Theme {
  if (typeof window === "undefined") return "system";
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function storeTheme(theme: Theme): void {
  if (typeof window === "undefined") return;
  try {
    if (theme === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Preference not persisted. The session still honours it.
  }
}

export function systemTheme(): ResolvedTheme {
  if (typeof window === "undefined") return "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function resolveTheme(theme: Theme): ResolvedTheme {
  return theme === "system" ? systemTheme() : theme;
}

/**
 * Apply a theme to the document.
 *
 * Also sets `color-scheme`, which is what tells the browser to render native
 * form controls, scrollbars and the `<html>` background in the right mode.
 * Without it a dark page gets light scrollbars, which looks like a bug.
 */
export function applyTheme(theme: Theme): ResolvedTheme {
  const resolved = resolveTheme(theme);
  const root = document.documentElement;
  root.setAttribute("data-theme", resolved);
  root.style.colorScheme = resolved;
  return resolved;
}
