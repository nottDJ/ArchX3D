"use client";

/**
 * ArchX3D — Viewer settings hook
 * ==============================
 * React's window onto the settings store in `lib/viewer/settings.ts`.
 *
 * Two access patterns, on purpose:
 *
 * * `useViewerSettings()` subscribes and re-renders — for the toolbar, the
 *   settings panel, and anything that draws a value.
 * * `getSettings()` (imported directly from the store) does not — for the walk
 *   controller and the collision solver, which read every frame and must never
 *   trigger a render.
 *
 * Hydration
 * ---------
 * `getServerSettings` deliberately returns the defaults rather than the stored
 * values. Reading `localStorage` during render would make the server and client
 * markup disagree; instead the stored settings are applied in an effect after
 * mount, which produces one extra render on first load and no hydration
 * warning.
 */

import { useCallback, useEffect, useSyncExternalStore } from "react";

import {
  getServerSettings,
  getSettings,
  hydrateSettings,
  resetSettings,
  setSettings,
  subscribeSettings,
} from "@/lib/viewer/settings";
import type { ViewerSettings } from "@/types/viewer";

export interface UseViewerSettings {
  readonly settings: ViewerSettings;
  /** Merge a partial update. No-ops when nothing actually changes. */
  readonly update: (patch: Partial<ViewerSettings>) => void;
  /** Flip a boolean setting without reading it first. */
  readonly toggle: (key: BooleanSettingKey) => void;
  readonly reset: () => void;
}

/** Keys `toggle` accepts — every boolean field, and only those. */
export type BooleanSettingKey = {
  [K in keyof ViewerSettings]: ViewerSettings[K] extends boolean ? K : never;
}[keyof ViewerSettings];

export function useViewerSettings(): UseViewerSettings {
  const settings = useSyncExternalStore(
    subscribeSettings,
    getSettings,
    getServerSettings,
  );

  useEffect(() => {
    hydrateSettings();
  }, []);

  const update = useCallback((patch: Partial<ViewerSettings>) => {
    setSettings(patch);
  }, []);

  const toggle = useCallback((key: BooleanSettingKey) => {
    setSettings({ [key]: !getSettings()[key] } as Partial<ViewerSettings>);
  }, []);

  const reset = useCallback(() => {
    resetSettings();
  }, []);

  return { settings, update, toggle, reset };
}
