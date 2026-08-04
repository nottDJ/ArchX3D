/**
 * ArchX3D — Mesh classification
 * =============================
 * Decides what each mesh in a loaded GLB *is*: roof, wall, furniture, and so on.
 *
 * Why this is a pure module
 * -------------------------
 * It imports no `three`. The input is a plain description of a node — name,
 * `extras`, ancestor names, bounding box — and the output is a verdict. That
 * keeps it testable in milliseconds without a WebGL context, and it mirrors the
 * split the Python side already uses, where `blender/colour.py` decides and
 * `blender/materials.py` builds.
 *
 * The ladder
 * ----------
 * Five rungs, most reliable first. Every verdict records which rung it came
 * from, so a model that is being classified mostly by *guessing* is visible in
 * the statistics panel rather than quietly wrong.
 *
 *   1. metadata   `extras.archx3d_kind` — the generator told us
 *   2. category   `extras.archx3d_group` / `archx3d_category`
 *   3. name       a known object name from the generator
 *   4. hierarchy  an ancestor was classified
 *   5. geometry   a flat plate spanning the plan, near the top — a roof
 *
 * Rung 5 exists solely for roofs, and only for GLBs built before the generator
 * emitted metadata. It is the one rung that can be wrong on a valid model, so
 * it is deliberately conservative: it would rather miss a roof than hide a
 * mezzanine floor the user wanted to see.
 */

import type {
  Classification,
  ClassificationSource,
  ElementKind,
} from "../../types/viewer";

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

/** A `three` node reduced to the facts the classifier needs. */
export interface NodeDescriptor {
  readonly name: string;
  /** `object.userData`, which `GLTFLoader` populates from glTF `extras`. */
  readonly userData: Readonly<Record<string, unknown>>;
  /** Ancestor names, nearest parent first. */
  readonly ancestors?: readonly string[];
  /** Whether the node is a light rather than a mesh. */
  readonly isLight?: boolean;
  /** World-space axis-aligned bounds, Y-up. Required only for rung 5. */
  readonly bounds?: BoundsLike;
}

export interface BoundsLike {
  readonly min: readonly [number, number, number];
  readonly max: readonly [number, number, number];
}

/** Whole-model bounds, needed to judge "high up" and "spans the plan". */
export interface SceneExtent {
  readonly min: readonly [number, number, number];
  readonly max: readonly [number, number, number];
}

// ---------------------------------------------------------------------------
// Name tables — kept in step with the generator
// ---------------------------------------------------------------------------

/**
 * Exact object names the generator gives the architectural shell.
 *
 * Matched on the *whole* name, never as a substring. `ceiling_fan_fan_1` is a
 * fan; a substring test for "ceiling" would hide it with the roof, and the user
 * would be left wondering where the fan went.
 */
const SHELL_NAMES: Readonly<Record<string, ElementKind>> = {
  walls: "wall",
  wall: "wall",
  floor: "floor",
  slab: "floor",
  ceiling: "roof",
  roof: "roof",
};

/** Prefixes the scene-graph builders use, in `prefix_id` form. */
const PREFIXES: ReadonlyArray<readonly [string, ElementKind]> = [
  ["arch_", "structure"],
  ["light_", "light"],
  ["cutter_", "opening"],
  ["opening_", "opening"],
  ["door_", "opening"],
  ["window_", "opening"],
];

/** Lights created by the rigs, which carry no scene-graph id. */
const LIGHT_NAMES: ReadonlySet<string> = new Set([
  "sun_daylight",
  "key_sun",
  "fill_area",
  "keylight",
  "filllight",
  "rimlight",
]);

/** `SceneObject.group` maps straight onto a kind. */
const GROUP_TO_KIND: Readonly<Record<string, ElementKind>> = {
  furniture: "furniture",
  decor: "decor",
  appliance: "appliance",
};

/**
 * Categories that are lights even though the catalogue groups them as decor,
 * so "Lighting only" shows the fixture and not just the punctual light.
 */
const LUMINAIRE_CATEGORIES: ReadonlySet<string> = new Set([
  "ceiling_light",
  "pendant_light",
  "chandelier",
  "wall_sconce",
  "floor_lamp",
  "table_lamp",
  "spotlight",
  "led_strip",
]);

/** Strip Blender's `.001` de-duplication suffix. */
function baseName(name: string): string {
  const lower = name.trim().toLowerCase();
  const dot = lower.lastIndexOf(".");
  if (dot > 0 && /^\d{3}$/.test(lower.slice(dot + 1))) return lower.slice(0, dot);
  return lower;
}

function isElementKind(value: unknown): value is ElementKind {
  return (
    typeof value === "string" &&
    [
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
    ].includes(value)
  );
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

// ---------------------------------------------------------------------------
// Geometric roof inference (rung 5)
// ---------------------------------------------------------------------------

/** Tuning for the geometric roof test. Every value has a reason. */
export const ROOF_HEURISTIC = {
  /** A roof plate is thin: under 12% of the building's height. */
  maxThicknessRatio: 0.12,
  /** Its underside sits in the top 25% of the building. */
  minHeightRatio: 0.75,
  /** It covers at least 30% of the plan — a shelf does not. */
  minFootprintRatio: 0.3,
} as const;

/**
 * Is this node plausibly a roof, judged on size and position alone?
 *
 * Deliberately strict. A false positive hides part of the building the user
 * asked to see, which is worse than a false negative — they can still orbit
 * under an un-hidden roof, but they cannot find a floor that vanished.
 */
export function looksLikeRoof(
  bounds: BoundsLike,
  scene: SceneExtent,
): boolean {
  const sceneHeight = scene.max[1] - scene.min[1];
  const sceneFootprint =
    (scene.max[0] - scene.min[0]) * (scene.max[2] - scene.min[2]);
  if (sceneHeight <= 0 || sceneFootprint <= 0) return false;

  const thickness = bounds.max[1] - bounds.min[1];
  const footprint =
    (bounds.max[0] - bounds.min[0]) * (bounds.max[2] - bounds.min[2]);

  const isPlate = thickness / sceneHeight <= ROOF_HEURISTIC.maxThicknessRatio;
  const isHigh =
    (bounds.min[1] - scene.min[1]) / sceneHeight >= ROOF_HEURISTIC.minHeightRatio;
  const isBroad = footprint / sceneFootprint >= ROOF_HEURISTIC.minFootprintRatio;

  return isPlate && isHigh && isBroad;
}

// ---------------------------------------------------------------------------
// The classifier
// ---------------------------------------------------------------------------

function verdict(
  kind: ElementKind,
  source: ClassificationSource,
  node: NodeDescriptor,
): Classification {
  return {
    kind,
    source,
    objectId: str(node.userData.archx3d_id),
    category: str(node.userData.archx3d_category),
    roomId: str(node.userData.archx3d_room),
  };
}

/**
 * Classify one node.
 *
 * `scene` is only consulted for the geometric roof test; omit it and rung 5 is
 * skipped, which is the right behaviour while scene bounds are still unknown.
 */
export function classifyNode(
  node: NodeDescriptor,
  scene?: SceneExtent,
): Classification {
  // -- 1. The generator said so ------------------------------------------
  const declared = node.userData.archx3d_kind;
  if (isElementKind(declared)) return verdict(declared, "metadata", node);

  // -- 2. Catalogue group / category -------------------------------------
  const category = str(node.userData.archx3d_category);
  if (category && LUMINAIRE_CATEGORIES.has(category)) {
    return verdict("light", "category", node);
  }

  const group = str(node.userData.archx3d_group);
  if (group) return verdict(GROUP_TO_KIND[group] ?? "furniture", "category", node);

  // A catalogue category with no group still means it came from the furniture
  // builder, whatever it was called.
  if (category) return verdict("furniture", "category", node);

  // -- 3. Known generator names ------------------------------------------
  if (node.isLight) return verdict("light", "name", node);

  const name = baseName(node.name);
  if (name in SHELL_NAMES) return verdict(SHELL_NAMES[name], "name", node);
  if (LIGHT_NAMES.has(name)) return verdict("light", "name", node);

  for (const [prefix, kind] of PREFIXES) {
    if (name.startsWith(prefix)) return verdict(kind, "name", node);
  }

  // -- 4. Inherit from an ancestor ---------------------------------------
  for (const ancestor of node.ancestors ?? []) {
    const parent = baseName(ancestor);
    if (parent in SHELL_NAMES) {
      return verdict(SHELL_NAMES[parent], "hierarchy", node);
    }
    for (const [prefix, kind] of PREFIXES) {
      if (parent.startsWith(prefix)) return verdict(kind, "hierarchy", node);
    }
  }

  // -- 5. Geometry, for roofs only ---------------------------------------
  if (scene && node.bounds && looksLikeRoof(node.bounds, scene)) {
    return verdict("roof", "geometry", node);
  }

  return verdict("unknown", "fallback", node);
}

/**
 * Which kinds a view mode shows.
 *
 * Split out from the component so the rule — and its interaction with the roof
 * toggle — can be tested without mounting a canvas.
 */
export function isVisibleInMode(
  kind: ElementKind,
  shows: readonly ElementKind[] | null,
  roofHidden: boolean,
): boolean {
  if (kind === "roof" && roofHidden) return false;
  if (shows === null) return true;
  return shows.includes(kind);
}
