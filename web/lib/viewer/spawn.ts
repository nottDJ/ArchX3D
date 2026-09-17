/**
 * ArchX3D — Where walk mode puts you
 * ==================================
 * Choosing a place to stand, and refusing a place that has no floor.
 *
 * The bug this exists to prevent (G5)
 * -----------------------------------
 * Walk mode is the viewer's default, and on a real two-storey drawing it opened
 * to a black screen. The renderer was fine — tens of thousands of draw calls a
 * second, no NaN, no lost context. The camera was 107 m underground and still
 * falling. Three things had to go wrong together:
 *
 * 1. The spawn took the largest room in the manifest. That room was on the
 *    first floor, but the camera was placed at the model's *lowest* height —
 *    and under that room the ground-floor slab had a void.
 * 2. Nothing asked whether there was a floor there. The one check that could
 *    have (`groundedSpawn`) ran before the collider had been built, and a
 *    downward probe over a void finds nothing anyway, so the position stood.
 * 3. Nothing noticed the fall. Gravity is correct physics; with no floor it
 *    simply runs forever, and every frame after the first second draws empty
 *    space.
 *
 * So the rules here are: prefer the storey you would walk in on; stand on a
 * point that is actually inside a room, not the centre of its bounding box
 * (which for an L-shaped room is outside it); only accept a point with a floor
 * under it; and treat falling out of the model as a reason to respawn, never as
 * a state to render.
 *
 * Pure — the floor test is behind `GroundProbe`, which the real BVH collider
 * satisfies — so every rule is tested in Node against real geometry.
 */

import type { RoomInfo } from "../../types/viewer";
import { boxCenter, planToViewer, type Box, type Vec3 } from "./bounds";

/** Anything that can say where the floor is below a point. `Collider` does. */
export interface GroundProbe {
  groundBelow(x: number, z: number, from: number, maxDrop?: number): number | null;
}

/**
 * How far above a storey's expected floor a spawn probe starts.
 *
 * High enough to clear a slab whose top sits above the model's lowest point
 * (the bounds start at the *bottom* of the ground slab, 0.1-0.3 m below the
 * surface you stand on), low enough to stay under any ceiling a person can
 * stand beneath — so the probe meets this storey's floor, not the one above.
 */
export const SPAWN_PROBE_CLEARANCE = 1.6;

/**
 * How far below the probe a floor may be and still count as this storey's.
 *
 * A floor further down than this is the storey *below* seen through a hole,
 * and standing on it would put the user somewhere they did not ask to be.
 */
export const SPAWN_MAX_DROP = SPAWN_PROBE_CLEARANCE + 0.8;

/**
 * Probe start above the feet, and reach below them, when asking whether a
 * position someone is already standing at is still supported.
 */
export const SUPPORT_PROBE_MARGIN = 0.3;
export const SUPPORT_MAX_DROP = 0.9;

/**
 * Metres below the model's lowest geometry at which the camera has fallen out.
 *
 * About a storey: deep enough that stepping off a mezzanine onto the floor
 * below is not mistaken for leaving the building, shallow enough that a user
 * who does fall out sees well under a second of darkness before recovery.
 */
export const FALL_OUT_DEPTH = 3;

export interface SpawnCandidate {
  /** Eye position, on the storey's expected floor height. Not yet grounded. */
  readonly position: Vec3;
  readonly roomId: string | null;
}

export interface WalkSpawn {
  readonly position: Vec3;
  readonly roomId: string | null;
  /**
   * True when a probe confirmed a floor under `position`. False either because
   * no probe was available yet — the collider is built after the model is
   * indexed — or because no candidate had a floor at all. The caller must not
   * run gravity from an unsupported spawn.
   */
  readonly supported: boolean;
}

// ---------------------------------------------------------------------------
// A point inside a room
// ---------------------------------------------------------------------------

type Point = readonly [number, number];

/** Even-odd containment. Points on an edge may go either way; that is fine. */
export function pointInPolygon(point: Point, polygon: ReadonlyArray<Point>): boolean {
  const [x, y] = point;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

function areaCentroid(polygon: ReadonlyArray<Point>): Point | null {
  let area = 0;
  let cx = 0;
  let cy = 0;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const cross = polygon[j][0] * polygon[i][1] - polygon[i][0] * polygon[j][1];
    area += cross;
    cx += (polygon[j][0] + polygon[i][0]) * cross;
    cy += (polygon[j][1] + polygon[i][1]) * cross;
  }
  if (Math.abs(area) < 1e-9) return null;
  return [cx / (3 * area), cy / (3 * area)];
}

/**
 * The middle of the widest interior span along a horizontal line.
 *
 * Always inside the polygon when the line crosses it, which the centroid of an
 * L- or U-shaped room is not.
 */
function widestSpanMidpoint(polygon: ReadonlyArray<Point>, y: number): Point | null {
  const xs: number[] = [];
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if (yi > y !== yj > y) xs.push(((xj - xi) * (y - yi)) / (yj - yi) + xi);
  }
  xs.sort((a, b) => a - b);
  let best: Point | null = null;
  let width = 0;
  for (let k = 0; k + 1 < xs.length; k += 2) {
    if (xs[k + 1] - xs[k] > width) {
      width = xs[k + 1] - xs[k];
      best = [(xs[k] + xs[k + 1]) / 2, y];
    }
  }
  return best;
}

/**
 * A plan point that lies inside the room, in plan metres.
 *
 * The bounding-box centre is what the viewer used to use, and for any room
 * that is not convex it can be outside the room entirely — in a courtyard, in
 * the neighbouring room, or over nothing.
 */
export function interiorPoint(room: Pick<RoomInfo, "polygon" | "bounds_min" | "bounds_max">): Point {
  const boxMid: Point = [
    (room.bounds_min[0] + room.bounds_max[0]) / 2,
    (room.bounds_min[1] + room.bounds_max[1]) / 2,
  ];
  const polygon = room.polygon;
  if (polygon.length < 3) return boxMid;

  const centroid = areaCentroid(polygon);
  if (centroid && pointInPolygon(centroid, polygon)) return centroid;

  // Scan a few heights, preferring the middle, and keep the first that works.
  for (const t of [0.5, 0.35, 0.65, 0.2, 0.8]) {
    const y = room.bounds_min[1] + (room.bounds_max[1] - room.bounds_min[1]) * t;
    const mid = widestSpanMidpoint(polygon, y);
    if (mid && pointInPolygon(mid, polygon)) return mid;
  }
  return boxMid;
}

// ---------------------------------------------------------------------------
// Candidates and choice
// ---------------------------------------------------------------------------

/**
 * Places to try, best first.
 *
 * The lowest storey first, because that is the one a person walks into; within
 * a storey the largest room first, because that is the most legible first view
 * and the least likely to put the camera nose-first against a wall. Every room
 * follows, so a void under the favourite is answered by the next room rather
 * than by a fall. The plan centre is the last resort.
 */
export function spawnCandidates(
  rooms: readonly RoomInfo[],
  box: Box,
  eyeHeight: number,
): SpawnCandidate[] {
  const base = Number.isFinite(box.min[1]) ? box.min[1] : 0;

  const ordered = [...rooms].sort((a, b) => {
    const byStorey = (a.elevation ?? 0) - (b.elevation ?? 0);
    if (Math.abs(byStorey) > 1e-6) return byStorey;
    return b.area_m2 - a.area_m2;
  });

  const out: SpawnCandidate[] = ordered.map((room) => {
    const [px, py] = interiorPoint(room);
    const floor = base + (room.elevation ?? 0);
    const [x, , z] = planToViewer(px, py);
    return { position: [x, floor + eyeHeight, z], roomId: room.id };
  });

  const centre = boxCenter(box);
  out.push({ position: [centre[0], base + eyeHeight, centre[2]], roomId: null });
  return out;
}

/**
 * The first candidate with a floor under it, standing on that floor.
 *
 * With no probe yet, the best candidate is returned unconfirmed; the caller
 * re-asks once the collider exists. With a probe and no supported candidate at
 * all, the best candidate is returned with `supported: false`, and the caller
 * must fly rather than fall.
 */
export function chooseWalkSpawn(
  candidates: readonly SpawnCandidate[],
  probe: GroundProbe | null,
  eyeHeight: number,
): WalkSpawn {
  const first = candidates[0] ?? { position: [0, eyeHeight, 0] as Vec3, roomId: null };
  if (!probe) return { ...first, supported: false };

  for (const candidate of candidates) {
    const [x, y, z] = candidate.position;
    const from = y - eyeHeight + SPAWN_PROBE_CLEARANCE;
    const floor = probe.groundBelow(x, z, from, SPAWN_MAX_DROP);
    if (floor !== null && Number.isFinite(floor)) {
      return { position: [x, floor + eyeHeight, z], roomId: candidate.roomId, supported: true };
    }
  }
  return { ...first, supported: false };
}

/**
 * The floor height under someone already standing at `position`, or `null`.
 *
 * Used to decide whether a remembered pose, or the pose the camera was placed
 * at before the collider existed, is still somewhere to stand.
 */
export function supportAt(probe: GroundProbe, position: Vec3, eyeHeight: number): number | null {
  const [x, y, z] = position;
  if (![x, y, z].every(Number.isFinite)) return null;
  const feet = y - eyeHeight;
  return probe.groundBelow(x, z, feet + SUPPORT_PROBE_MARGIN, SUPPORT_MAX_DROP);
}

/** True once the camera is well below everything the model contains. */
export function hasFallenOut(y: number, box: Box | null): boolean {
  if (!Number.isFinite(y)) return true;
  if (!box || !Number.isFinite(box.min[1])) return false;
  return y < box.min[1] - FALL_OUT_DEPTH;
}

/**
 * What to do the Nth time the camera falls out of the model.
 *
 * Once is a spawn that looked supported but was not quite — respawn. Twice
 * means the geometry cannot hold the walk capsule at all (a model built at the
 * wrong scale, where a "garage" is 0.8 m across and its walls push the 0.56 m
 * capsule straight through the floor), and respawning again would only repeat
 * the fall as a flicker. Fly instead: no gravity, so no fall, and the model
 * stays on screen.
 */
export function afterFallingOut(count: number): "respawn" | "fly" {
  return count <= 1 ? "respawn" : "fly";
}

// ---------------------------------------------------------------------------
// Which way to face
// ---------------------------------------------------------------------------

/** Anything that can say how far one can see horizontally. `Collider` does. */
export interface SightProbe {
  clearance(x: number, y: number, z: number, dx: number, dz: number, reach: number): number;
}

/** Directions tried around a full turn: every 15°. */
export const SIGHT_SAMPLES = 24;

/** Metres of open view beyond which a direction is no more inviting. */
export const SIGHT_REACH = 20;

/**
 * Neighbours either side a direction is judged with, in samples.
 *
 * Two at 15° is ±30°, about half a walk camera's horizontal field of view. A
 * single ray threaded through a doorway is not a view; what fills the screen is.
 */
const SIGHT_HALF_WINDOW = 2;

/**
 * The yaw with the most open view from `position`.
 *
 * Facing the building's centre, the rule this refines, is right from the middle
 * of a plan and wrong from its edge: in a corner room the centre lies beyond the
 * nearest wall, and the first thing a user saw was that wall a metre away —
 * flat colour, no floor, nothing to say where they were.
 *
 * Each direction is scored by the mean clear distance across the view it would
 * show. A sightline stops where it leaves `box`: through an exterior window the
 * ray would run on outdoors, and the view that invites is the interior. Ties go
 * to the direction nearest `preferredYaw`, so an open plan still faces the
 * centre as before.
 */
export function openestYaw(
  probe: SightProbe,
  position: Vec3,
  preferredYaw: number,
  box: Box | null = null,
): number {
  const [x, y, z] = position;
  if (![x, y, z, preferredYaw].every(Number.isFinite)) return preferredYaw;

  const yaws: number[] = [];
  const reach: number[] = [];
  for (let i = 0; i < SIGHT_SAMPLES; i += 1) {
    const yaw = (i / SIGHT_SAMPLES) * Math.PI * 2;
    // Forward for a yaw, in the convention of `lookAngles`: yaw 0 looks down -z.
    const dx = -Math.sin(yaw);
    const dz = -Math.cos(yaw);
    const limit = Math.min(SIGHT_REACH, box ? exitDistance(x, z, dx, dz, box) : SIGHT_REACH);
    yaws.push(yaw);
    reach.push(limit > 0 ? probe.clearance(x, y, z, dx, dz, limit) : 0);
  }

  let best = preferredYaw;
  let bestScore = -Infinity;
  let bestTurn = Infinity;
  for (let i = 0; i < SIGHT_SAMPLES; i += 1) {
    let sum = 0;
    for (let k = -SIGHT_HALF_WINDOW; k <= SIGHT_HALF_WINDOW; k += 1) {
      sum += reach[(i + k + SIGHT_SAMPLES) % SIGHT_SAMPLES];
    }
    const score = sum / (SIGHT_HALF_WINDOW * 2 + 1);
    const turn = angleBetween(yaws[i], preferredYaw);
    // A millimetre is noise, not a better view.
    if (score > bestScore + 1e-3 || (Math.abs(score - bestScore) <= 1e-3 && turn < bestTurn)) {
      best = yaws[i];
      bestScore = score;
      bestTurn = turn;
    }
  }
  return best;
}

/** Distance along (dx, dz) from (x, z) to the edge of `box` in plan; 0 if outside. */
function exitDistance(x: number, z: number, dx: number, dz: number, box: Box): number {
  let t = Infinity;
  if (Math.abs(dx) > 1e-9) t = Math.min(t, ((dx > 0 ? box.max[0] : box.min[0]) - x) / dx);
  if (Math.abs(dz) > 1e-9) t = Math.min(t, ((dz > 0 ? box.max[2] : box.min[2]) - z) / dz);
  return Math.max(0, t);
}

/** Unsigned smallest angle between two yaws, radians. */
function angleBetween(a: number, b: number): number {
  const d = Math.abs(a - b) % (Math.PI * 2);
  return d > Math.PI ? Math.PI * 2 - d : d;
}
