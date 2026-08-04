/**
 * ArchX3D — Viewer settings and persistence
 * =========================================
 * Defaults, bounds, validation, and a tiny external store.
 *
 * Why an external store rather than React state
 * --------------------------------------------
 * The walk controller and the collision solver read settings every frame, at
 * 60–144 Hz. Holding them in React state would mean either passing them through
 * props — re-rendering the whole canvas tree whenever the user nudges a slider —
 * or reading a ref that has to be manually kept in sync.
 *
 * A module-level store solves both: frame code calls `getSettings()` and never
 * re-renders, while the toolbar subscribes through `useSyncExternalStore` and
 * updates normally. It is about forty lines and removes a dependency.
 *
 * Why everything is validated on read
 * -----------------------------------
 * These values come out of `localStorage`, which is user-writable and survives
 * across versions. A `walkSpeed` of `null` from an older build, or `1e9` from a
 * curious user, must not put the camera in orbit around Jupiter. Every field is
 * clamped to a range that produces a usable viewer.
 */

import type {
  CameraMode,
  EnvironmentPreset,
  SavedCamera,
  ViewerSettings,
  ViewerTheme,
  ViewMode,
} from "../../types/viewer";
import { ENVIRONMENT_PRESETS, VIEW_MODES } from "../../types/viewer";

// ---------------------------------------------------------------------------
// Defaults and bounds
// ---------------------------------------------------------------------------

export const DEFAULT_SETTINGS: ViewerSettings = {
  // Walk, not orbit. The building is reconstructed at true scale — 3 m
  // ceilings, doorways sized from the drawing — and orbiting it from outside
  // presents all of that as a tabletop model, which is the one reading the
  // reconstruction is not for. Entering at eye height is what makes a wrong
  // ceiling height or a mis-sized doorway obvious, and those are the errors a
  // walkthrough exists to expose. Orbit remains one keypress away.
  cameraMode: "walk",
  viewMode: "full",
  showRoof: true,
  // A relaxed indoor walking pace. Faster reads as a game; slower is tedious
  // crossing a large plan.
  walkSpeed: 2.6,
  runMultiplier: 2.4,
  // Average adult eye height. The camera is a person, not a drone.
  eyeHeight: 1.65,
  lookSensitivity: 0.0022,
  jumpEnabled: false,
  collisionEnabled: true,
  shadows: true,
  environment: "studio",
  exposure: 1,
  ambientIntensity: 0.35,
  theme: "dark",
  showGrid: true,
  showStats: false,
  showMinimap: true,
};

/** `[min, max]` for every numeric setting, and the slider step. */
export const SETTING_BOUNDS = {
  walkSpeed: { min: 0.5, max: 12, step: 0.1 },
  runMultiplier: { min: 1, max: 5, step: 0.1 },
  eyeHeight: { min: 0.5, max: 3, step: 0.05 },
  lookSensitivity: { min: 0.0005, max: 0.008, step: 0.0001 },
  exposure: { min: 0.2, max: 3, step: 0.05 },
  ambientIntensity: { min: 0, max: 2, step: 0.05 },
} as const;

export type NumericSetting = keyof typeof SETTING_BOUNDS;

export function clampSetting(key: NumericSetting, value: number): number {
  const { min, max } = SETTING_BOUNDS[key];
  if (!Number.isFinite(value)) return DEFAULT_SETTINGS[key];
  return Math.min(max, Math.max(min, value));
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

const VIEW_MODE_IDS = new Set<string>(VIEW_MODES.map((m) => m.id));
const ENVIRONMENT_IDS = new Set<string>(ENVIRONMENT_PRESETS);

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function num(key: NumericSetting, value: unknown): number {
  return typeof value === "number" ? clampSetting(key, value) : DEFAULT_SETTINGS[key];
}

/**
 * Coerce an untrusted object into valid settings.
 *
 * Never throws and never returns a partial object: an unreadable field falls
 * back to its default, so one bad key cannot cost the user the rest of their
 * preferences.
 */
export function parseSettings(raw: unknown): ViewerSettings {
  if (typeof raw !== "object" || raw === null) return DEFAULT_SETTINGS;
  const input = raw as Record<string, unknown>;

  return {
    cameraMode:
      input.cameraMode === "walk" || input.cameraMode === "orbit"
        ? (input.cameraMode as CameraMode)
        : DEFAULT_SETTINGS.cameraMode,
    viewMode:
      typeof input.viewMode === "string" && VIEW_MODE_IDS.has(input.viewMode)
        ? (input.viewMode as ViewMode)
        : DEFAULT_SETTINGS.viewMode,
    showRoof: bool(input.showRoof, DEFAULT_SETTINGS.showRoof),
    walkSpeed: num("walkSpeed", input.walkSpeed),
    runMultiplier: num("runMultiplier", input.runMultiplier),
    eyeHeight: num("eyeHeight", input.eyeHeight),
    lookSensitivity: num("lookSensitivity", input.lookSensitivity),
    jumpEnabled: bool(input.jumpEnabled, DEFAULT_SETTINGS.jumpEnabled),
    collisionEnabled: bool(input.collisionEnabled, DEFAULT_SETTINGS.collisionEnabled),
    shadows: bool(input.shadows, DEFAULT_SETTINGS.shadows),
    environment:
      typeof input.environment === "string" && ENVIRONMENT_IDS.has(input.environment)
        ? (input.environment as EnvironmentPreset)
        : DEFAULT_SETTINGS.environment,
    exposure: num("exposure", input.exposure),
    ambientIntensity: num("ambientIntensity", input.ambientIntensity),
    theme:
      input.theme === "light" || input.theme === "dark"
        ? (input.theme as ViewerTheme)
        : DEFAULT_SETTINGS.theme,
    showGrid: bool(input.showGrid, DEFAULT_SETTINGS.showGrid),
    showStats: bool(input.showStats, DEFAULT_SETTINGS.showStats),
    showMinimap: bool(input.showMinimap, DEFAULT_SETTINGS.showMinimap),
  };
}

/** Coerce an untrusted object into a saved camera, dropping anything odd. */
export function parseSavedCamera(raw: unknown): SavedCamera {
  if (typeof raw !== "object" || raw === null) return {};
  const input = raw as Record<string, unknown>;

  const pose = (value: unknown) => {
    if (typeof value !== "object" || value === null) return undefined;
    const p = value as Record<string, unknown>;
    const position = triple(p.position);
    if (!position) return undefined;
    return {
      position,
      target: triple(p.target),
      yaw: typeof p.yaw === "number" && Number.isFinite(p.yaw) ? p.yaw : undefined,
      pitch:
        typeof p.pitch === "number" && Number.isFinite(p.pitch) ? p.pitch : undefined,
    };
  };

  return {
    orbit: pose(input.orbit),
    walk: pose(input.walk),
    mode:
      input.mode === "walk" || input.mode === "orbit"
        ? (input.mode as CameraMode)
        : undefined,
  };
}

function triple(value: unknown): readonly [number, number, number] | undefined {
  if (!Array.isArray(value) || value.length !== 3) return undefined;
  const [x, y, z] = value;
  if (![x, y, z].every((n) => typeof n === "number" && Number.isFinite(n))) {
    return undefined;
  }
  return [x as number, y as number, z as number];
}

// ---------------------------------------------------------------------------
// Storage
// ---------------------------------------------------------------------------

const SETTINGS_KEY = "archx3d.viewer.settings.v1";
const CAMERA_KEY_PREFIX = "archx3d.viewer.camera.v1:";

/**
 * Camera poses are stored per model, not globally.
 *
 * Resuming the position from a *different* building would drop you inside a
 * wall, or in empty space a hundred metres away. Keying on the model URL means
 * every project remembers its own vantage point.
 */
export function cameraStorageKey(modelUrl: string): string {
  return CAMERA_KEY_PREFIX + modelUrl;
}

/** `localStorage` access that survives private mode, quota errors and SSR. */
function readJson(key: string): unknown {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? null : JSON.parse(raw);
  } catch {
    return null;
  }
}

function writeJson(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Full or blocked storage is not worth interrupting a session over — the
    // viewer works perfectly well without remembering anything.
  }
}

export function loadSettings(): ViewerSettings {
  return parseSettings(readJson(SETTINGS_KEY));
}

export function saveSettings(settings: ViewerSettings): void {
  writeJson(SETTINGS_KEY, settings);
}

export function loadCamera(modelUrl: string): SavedCamera {
  return parseSavedCamera(readJson(cameraStorageKey(modelUrl)));
}

export function saveCamera(modelUrl: string, camera: SavedCamera): void {
  writeJson(cameraStorageKey(modelUrl), camera);
}

// ---------------------------------------------------------------------------
// The store
// ---------------------------------------------------------------------------

type Listener = () => void;

let current: ViewerSettings = DEFAULT_SETTINGS;
let hydrated = false;
const listeners = new Set<Listener>();

/**
 * Read settings without subscribing. Safe to call every frame.
 *
 * Returns the same frozen object until something changes, so a frame loop can
 * cache it and compare by identity.
 */
export function getSettings(): ViewerSettings {
  return current;
}

/**
 * Server snapshot for `useSyncExternalStore`.
 *
 * Always the defaults, never the stored value: reading `localStorage` during
 * render would produce different markup on server and client and trigger a
 * hydration mismatch. `hydrate()` applies the stored settings after mount.
 */
export function getServerSettings(): ViewerSettings {
  return DEFAULT_SETTINGS;
}

export function subscribeSettings(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(): void {
  for (const listener of listeners) listener();
}

export function setSettings(patch: Partial<ViewerSettings>): ViewerSettings {
  const next = parseSettings({ ...current, ...patch });

  // Identity is the subscription signal, so an update that changes nothing must
  // not produce a new object — otherwise every slider drag re-renders the tree.
  if (shallowEqual(current, next)) return current;

  current = next;
  saveSettings(next);
  emit();
  return next;
}

export function resetSettings(): void {
  current = DEFAULT_SETTINGS;
  saveSettings(current);
  emit();
}

/** Apply persisted settings once, after mount. Idempotent. */
export function hydrateSettings(): void {
  if (hydrated) return;
  hydrated = true;
  const stored = loadSettings();
  if (!shallowEqual(current, stored)) {
    current = stored;
    emit();
  }
}

function shallowEqual(a: ViewerSettings, b: ViewerSettings): boolean {
  const keys = Object.keys(a) as Array<keyof ViewerSettings>;
  return keys.every((key) => a[key] === b[key]);
}

/** Test seam — resets module state between cases. */
export function __resetStoreForTests(): void {
  current = DEFAULT_SETTINGS;
  hydrated = false;
  listeners.clear();
}
