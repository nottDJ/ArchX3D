/**
 * ArchX3D — Viewer domain types
 * =============================
 * The vocabulary shared by every part of the viewer: what a mesh *is*, what the
 * camera is doing, and what the user has configured.
 *
 * `ElementKind` mirrors `modules/blender/metadata.py` exactly. The generator
 * writes these strings into the GLB's `extras`; the viewer switches on them.
 * Adding a value to one side without the other produces an object nobody can
 * hide, so the two lists are kept in step deliberately.
 *
 * Coordinate frames
 * -----------------
 * The GLB is **Y-up** — the glTF exporter converts Blender's +Z up on the way
 * out. Room metadata, however, is emitted in Blender **plan metres** (+Z up),
 * because that is the frame the scene graph and `geometry.json` use.
 *
 * One conversion bridges them, and it lives in exactly one place
 * (`lib/viewer/bounds.ts#planToViewer`):
 *
 *     plan (x, y)  ->  viewer (x, height, -y)
 */

// ---------------------------------------------------------------------------
// Semantics
// ---------------------------------------------------------------------------

/** What a mesh is, architecturally. Mirrors `blender/metadata.py#KINDS`. */
export type ElementKind =
  | "roof"
  | "wall"
  | "floor"
  | "opening"
  | "structure"
  | "furniture"
  | "decor"
  | "appliance"
  | "light"
  | "unknown";

export const ELEMENT_KINDS: readonly ElementKind[] = [
  "roof",
  "wall",
  "floor",
  "opening",
  "structure",
  "furniture",
  "decor",
  "appliance",
  "light",
  "unknown",
] as const;

/** How confident the classifier is about a `kind`, and why. */
export type ClassificationSource =
  /** Read straight from `extras.archx3d_kind` — the generator said so. */
  | "metadata"
  /** Derived from `extras.archx3d_group` / `archx3d_category`. */
  | "category"
  /** Matched a known object name from the generator. */
  | "name"
  /** Inherited from an ancestor node's classification. */
  | "hierarchy"
  /** Inferred from size and position — a flat plate high up is a roof. */
  | "geometry"
  /** Nothing matched. */
  | "fallback";

/** The classifier's verdict for one node. */
export interface Classification {
  readonly kind: ElementKind;
  readonly source: ClassificationSource;
  /** Scene-graph object id, when the generator supplied one. */
  readonly objectId?: string;
  /** Catalogue category, e.g. `"sofa"`. */
  readonly category?: string;
  /** Room this object belongs to, when known. */
  readonly roomId?: string;
}

// ---------------------------------------------------------------------------
// Camera
// ---------------------------------------------------------------------------

export type CameraMode = "orbit" | "walk";

/** A camera pose, persisted between sessions so a reload resumes where you were. */
export interface CameraPose {
  readonly position: readonly [number, number, number];
  /** Orbit only — the point being orbited. */
  readonly target?: readonly [number, number, number];
  /** Walk only — look direction in radians. */
  readonly yaw?: number;
  readonly pitch?: number;
}

/** Everything the viewer remembers about where you were looking. */
export interface SavedCamera {
  readonly orbit?: CameraPose;
  readonly walk?: CameraPose;
  readonly mode?: CameraMode;
}

// ---------------------------------------------------------------------------
// View modes
// ---------------------------------------------------------------------------

/**
 * Architectural view modes. These change *visibility only* — nothing is
 * reloaded, re-parsed or regenerated, so switching is a single traversal.
 */
export type ViewMode =
  | "full"
  | "interior"
  | "structure"
  | "furniture"
  | "lighting"
  | "wireframe";

export interface ViewModeMeta {
  readonly id: ViewMode;
  readonly label: string;
  readonly hint: string;
  /** Kinds this mode shows. `null` means "everything". */
  readonly shows: readonly ElementKind[] | null;
  /** Forces the roof hidden regardless of the roof toggle. */
  readonly hidesRoof: boolean;
  /** Renders every visible material as wireframe. */
  readonly wireframe: boolean;
}

/**
 * Order is the order they appear in the toolbar.
 *
 * `furniture` deliberately keeps the floor: furniture floating in a void is
 * disorienting and tells you nothing about where it sits. `lighting` keeps the
 * floor for the same reason — a light with no surface to fall on reads as a
 * bug rather than a light.
 */
export const VIEW_MODES: readonly ViewModeMeta[] = [
  {
    id: "full",
    label: "Full building",
    hint: "Everything, exactly as generated",
    shows: null,
    hidesRoof: false,
    wireframe: false,
  },
  {
    id: "interior",
    label: "Interior",
    hint: "Roof removed so you can look inside",
    shows: null,
    hidesRoof: true,
    wireframe: false,
  },
  {
    id: "structure",
    label: "Structure",
    hint: "Walls, slabs, columns and openings only",
    shows: ["wall", "floor", "roof", "structure", "opening"],
    hidesRoof: false,
    wireframe: false,
  },
  {
    id: "furniture",
    label: "Furniture",
    hint: "Furnishing and decor, on the floor plane",
    shows: ["furniture", "decor", "appliance", "floor"],
    hidesRoof: true,
    wireframe: false,
  },
  {
    id: "lighting",
    label: "Lighting",
    hint: "Luminaires and how they fall on the floor",
    shows: ["light", "floor"],
    hidesRoof: true,
    wireframe: false,
  },
  {
    id: "wireframe",
    label: "Wireframe",
    hint: "Every surface as edges",
    shows: null,
    hidesRoof: false,
    wireframe: true,
  },
] as const;

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export type ViewerTheme = "dark" | "light";

export interface ViewerSettings {
  readonly cameraMode: CameraMode;
  readonly viewMode: ViewMode;
  /** User's roof preference. A `hidesRoof` view mode overrides it. */
  readonly showRoof: boolean;
  /** Metres per second at a walk. */
  readonly walkSpeed: number;
  /** Multiplier applied while Shift is held. */
  readonly runMultiplier: number;
  /** Eye height above the floor, metres. */
  readonly eyeHeight: number;
  /** Mouse-look sensitivity, radians per pixel. */
  readonly lookSensitivity: number;
  /** Off by default — an architectural walkthrough is not a platformer. */
  readonly jumpEnabled: boolean;
  /** Stop the camera passing through walls. */
  readonly collisionEnabled: boolean;
  readonly shadows: boolean;
  /** Image-based lighting from a drei environment preset. */
  readonly environment: EnvironmentPreset;
  readonly exposure: number;
  readonly ambientIntensity: number;
  readonly theme: ViewerTheme;
  readonly showGrid: boolean;
  readonly showStats: boolean;
  readonly showMinimap: boolean;
}

/**
 * Presets bundled with `@react-three/drei`. Each is fetched lazily from the
 * drei CDN the first time it is selected, so `studio` — the default — is the
 * only one most sessions ever load.
 */
export type EnvironmentPreset =
  | "apartment"
  | "city"
  | "dawn"
  | "forest"
  | "lobby"
  | "night"
  | "park"
  | "studio"
  | "sunset"
  | "warehouse";

export const ENVIRONMENT_PRESETS: readonly EnvironmentPreset[] = [
  "apartment",
  "city",
  "dawn",
  "forest",
  "lobby",
  "night",
  "park",
  "studio",
  "sunset",
  "warehouse",
] as const;

// ---------------------------------------------------------------------------
// Model
// ---------------------------------------------------------------------------

/** Progress of the GLB fetch and parse. */
export type LoadPhase = "idle" | "downloading" | "parsing" | "ready" | "error";

export interface LoadState {
  readonly phase: LoadPhase;
  /** `0..1`, or `null` when the server sends no `Content-Length`. */
  readonly progress: number | null;
  readonly loadedBytes: number;
  readonly totalBytes: number | null;
  readonly error: string | null;
}

/** What the viewer measured about the model it loaded. */
export interface ModelStats {
  readonly meshes: number;
  readonly triangles: number;
  readonly materials: number;
  readonly textures: number;
  readonly bytes: number | null;
  /** Metric extents of the whole building. */
  readonly size: readonly [number, number, number];
  readonly byKind: Readonly<Record<ElementKind, number>>;
  /** How each mesh's kind was decided — exposes reliance on inference. */
  readonly bySource: Readonly<Partial<Record<ClassificationSource, number>>>;
}

// ---------------------------------------------------------------------------
// Rooms
// ---------------------------------------------------------------------------

/**
 * One room, as described by the scene manifest embedded in the GLB.
 *
 * Coordinates are **plan metres**, matching the scene graph. Convert with
 * `planToViewer` before using them in the scene.
 */
export interface RoomInfo {
  readonly id: string;
  readonly name: string;
  readonly room_type: string;
  readonly style?: string;
  readonly area_m2: number;
  readonly ceiling_height: number;
  readonly bounds_min: readonly [number, number];
  readonly bounds_max: readonly [number, number];
  readonly polygon: ReadonlyArray<readonly [number, number]>;
  readonly connected_to: readonly string[];
  readonly object_count: number;
}

/** The `extras.archx3d` block the generator writes onto the glTF scene. */
export interface SceneManifest {
  readonly version: string;
  readonly generator: string;
  readonly up_axis: "Y" | "Z";
  readonly units: string;
  readonly rooms: readonly RoomInfo[];
}

// ---------------------------------------------------------------------------
// Source
// ---------------------------------------------------------------------------

/**
 * Where a viewer session gets its model from.
 *
 * `project` is the wizard's per-project output; `job` is the one-shot
 * `/api/generate` pipeline, which writes to the shared `output/` directory.
 */
export interface ViewerSource {
  readonly url: string;
  readonly projectId?: string;
  readonly jobId?: string;
  /** Shown in the header so a user knows which build they are looking at. */
  readonly label: string;
}
