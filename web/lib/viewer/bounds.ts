/**
 * ArchX3D — Framing and coordinate maths
 * ======================================
 * Where to put the camera so a building fills the frame, where to stand when
 * entering walk mode, and the one conversion between plan metres and viewer
 * space.
 *
 * Pure number maths, no `three` import — see `classify.ts` for why.
 *
 * The conversion, stated once
 * ---------------------------
 * The scene graph, `geometry.json` and the room manifest are all in Blender's
 * plan frame: metres, +Z up, X/Y in plan. The glTF exporter rotates the scene
 * to glTF's +Y up on the way out, which maps
 *
 *     Blender (x, y, z)  ->  glTF (x, z, -y)
 *
 * so a plan point `(x, y)` at height `h` is `(x, h, -y)` in the viewer. Getting
 * this backwards mirrors the building, which is subtle enough to pass a casual
 * look — so it lives in `planToViewer` and nowhere else.
 */

export type Vec3 = readonly [number, number, number];

export interface Box {
  readonly min: Vec3;
  readonly max: Vec3;
}

// ---------------------------------------------------------------------------
// Coordinate conversion
// ---------------------------------------------------------------------------

/** Plan metres (+Z up, X/Y in plan) to viewer space (+Y up). */
export function planToViewer(x: number, y: number, height = 0): Vec3 {
  return [x, height, -y];
}

/** The inverse, for reading a viewer position back onto the plan. */
export function viewerToPlan(position: Vec3): readonly [number, number] {
  return [position[0], -position[2]];
}

// ---------------------------------------------------------------------------
// Boxes
// ---------------------------------------------------------------------------

export function boxCenter(box: Box): Vec3 {
  return [
    (box.min[0] + box.max[0]) / 2,
    (box.min[1] + box.max[1]) / 2,
    (box.min[2] + box.max[2]) / 2,
  ];
}

export function boxSize(box: Box): Vec3 {
  return [
    box.max[0] - box.min[0],
    box.max[1] - box.min[1],
    box.max[2] - box.min[2],
  ];
}

/** Radius of the sphere enclosing the box. */
export function boxRadius(box: Box): number {
  const [w, h, d] = boxSize(box);
  return Math.sqrt(w * w + h * h + d * d) / 2;
}

export function isFiniteBox(box: Box): boolean {
  return [...box.min, ...box.max].every(Number.isFinite);
}

/**
 * Is this box degenerate enough that framing it would produce nonsense?
 *
 * An empty GLB, or one whose meshes all failed to load, yields a zero-size box;
 * dividing by it puts the camera at infinity and the user sees black.
 */
export function isDegenerateBox(box: Box): boolean {
  if (!isFiniteBox(box)) return true;
  const [w, h, d] = boxSize(box);
  return Math.max(w, h, d) < 1e-4;
}

// ---------------------------------------------------------------------------
// Framing
// ---------------------------------------------------------------------------

export interface FitResult {
  readonly position: Vec3;
  readonly target: Vec3;
  readonly near: number;
  readonly far: number;
  /** Distance from target to camera — handy for setting orbit limits. */
  readonly distance: number;
}

export interface FitOptions {
  /** Vertical field of view, degrees. */
  readonly fov?: number;
  /** Viewport aspect ratio (width / height). */
  readonly aspect?: number;
  /** Extra room around the model. 1.0 is a tight fit. */
  readonly padding?: number;
  /**
   * Unit vector from the target toward the camera. The default is a raised
   * three-quarter view: architectural drawings are conventionally shown this
   * way because it reads plan and elevation at once, and a dead-on front view
   * flattens the building into a facade.
   */
  readonly direction?: Vec3;
}

const DEFAULT_DIRECTION: Vec3 = normalise([1, 0.55, 1]);

function normalise(v: Vec3): Vec3 {
  const length = Math.hypot(v[0], v[1], v[2]);
  if (length < 1e-9) return [0, 0, 1];
  return [v[0] / length, v[1] / length, v[2] / length];
}

/**
 * Place the camera so the whole box fits the frame.
 *
 * Fits on both axes and takes the larger distance, because fitting only on
 * height crops a wide building at the sides — which is the common case for a
 * floor plan, since buildings are much wider than they are tall.
 */
export function fitCameraToBox(box: Box, options: FitOptions = {}): FitResult {
  const fov = options.fov ?? 50;
  const aspect = options.aspect ?? 16 / 9;
  const padding = options.padding ?? 1.25;
  const direction = options.direction ? normalise(options.direction) : DEFAULT_DIRECTION;

  const safe: Box = isDegenerateBox(box)
    ? { min: [-1, -1, -1], max: [1, 1, 1] }
    : box;

  const target = boxCenter(safe);
  const [width, height, depth] = boxSize(safe);

  const vFov = (fov * Math.PI) / 180;
  const hFov = 2 * Math.atan(Math.tan(vFov / 2) * aspect);

  // The horizontal extent depends on where the camera is looking from, so use
  // the diagonal — an upper bound that is correct for any azimuth.
  const planSpan = Math.hypot(width, depth);

  const distanceForHeight = height / 2 / Math.tan(vFov / 2);
  const distanceForWidth = planSpan / 2 / Math.tan(hFov / 2);
  const distance = Math.max(distanceForHeight, distanceForWidth, 0.1) * padding;

  const position: Vec3 = [
    target[0] + direction[0] * distance,
    target[1] + direction[1] * distance,
    target[2] + direction[2] * distance,
  ];

  const radius = boxRadius(safe);
  return {
    position,
    target,
    // Clamped so a large building does not push `near` out far enough to clip
    // the wall you are standing next to after switching into walk mode.
    near: Math.max(0.01, Math.min(0.1, radius / 1000)),
    far: Math.max(100, (distance + radius) * 4),
    distance,
  };
}

// ---------------------------------------------------------------------------
// Walk mode entry
// ---------------------------------------------------------------------------

export interface SpawnOptions {
  readonly eyeHeight?: number;
  /** Prefer this plan position, e.g. the centre of the largest room. */
  readonly preferred?: readonly [number, number];
}

/**
 * A sensible place to stand when entering walk mode.
 *
 * Defaults to the centre of the plan at eye height, which for a single
 * building is reliably inside it. A caller with room metadata should pass the
 * largest room's centre, which is better still — the plan centre of an L-shaped
 * building can land in the notch, outside the walls.
 */
export function interiorSpawn(box: Box, options: SpawnOptions = {}): Vec3 {
  const eyeHeight = options.eyeHeight ?? 1.65;
  const centre = boxCenter(box);
  const floor = Number.isFinite(box.min[1]) ? box.min[1] : 0;

  if (options.preferred) {
    const [x, , z] = planToViewer(options.preferred[0], options.preferred[1]);
    return [x, floor + eyeHeight, z];
  }
  return [centre[0], floor + eyeHeight, centre[2]];
}

/**
 * Where to stand to view a room, and what to look at.
 *
 * Stands back from the room centre along its shorter axis so the whole room is
 * in frame rather than the camera being buried in the middle of the furniture.
 */
export function roomViewpoint(
  boundsMin: readonly [number, number],
  boundsMax: readonly [number, number],
  eyeHeight: number,
  floorY = 0,
): { position: Vec3; target: Vec3 } {
  const cx = (boundsMin[0] + boundsMax[0]) / 2;
  const cy = (boundsMin[1] + boundsMax[1]) / 2;
  const width = Math.abs(boundsMax[0] - boundsMin[0]);
  const depth = Math.abs(boundsMax[1] - boundsMin[1]);

  const target = planToViewer(cx, cy, floorY + eyeHeight * 0.85);

  // Back off along whichever axis is longer, so the camera ends up at the
  // short end of a long room looking down it.
  const backoff = Math.max(0.9, Math.min(width, depth) * 0.34);
  const position: Vec3 =
    width >= depth
      ? planToViewer(cx - width / 2 + backoff, cy, floorY + eyeHeight)
      : planToViewer(cx, cy - depth / 2 + backoff, floorY + eyeHeight);

  return { position, target };
}

// ---------------------------------------------------------------------------
// Look direction
// ---------------------------------------------------------------------------

/**
 * Yaw and pitch that point from `position` at `target`.
 *
 * Yaw is measured the way `WalkController` applies it: rotation about +Y, with
 * 0 looking down −Z, which is the direction a default three.js camera faces.
 */
export function lookAngles(
  position: Vec3,
  target: Vec3,
): { yaw: number; pitch: number } {
  const dx = target[0] - position[0];
  const dy = target[1] - position[1];
  const dz = target[2] - position[2];

  const horizontal = Math.hypot(dx, dz);
  return {
    yaw: Math.atan2(-dx, -dz),
    pitch: Math.atan2(dy, horizontal || 1e-9),
  };
}
