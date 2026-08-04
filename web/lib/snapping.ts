/**
 * ArchX3D — Snapping and alignment
 * ================================
 * Pure geometry for the plan editor. No React, no DOM — every function here
 * takes plan-metre inputs and returns plan-metre outputs, which keeps it
 * testable and keeps the SVG layer free of maths.
 *
 * What snapping is for
 * --------------------
 * Furniture in real rooms is not placed at arbitrary offsets: it sits against
 * a wall, centred on another piece, or in line with its neighbours. Free
 * dragging can express those arrangements but only by accident, to within a
 * pixel. Snapping makes the arrangement the user obviously intends the one
 * that is easy to hit.
 *
 * Candidates are gathered from every source, each is scored by how far the
 * pointer would have to move to satisfy it, and the nearest within tolerance
 * wins per axis. X and Y resolve independently so an object can snap to a wall
 * on one axis while staying free on the other.
 */

import type { ReviewObject, ReviewRoom } from "./wizard";

export interface SnapSettings {
  enabled: boolean;
  /** How near a candidate must be to take effect, in metres. */
  tolerance: number;
  /** Grid pitch in metres; 0 disables grid snapping. */
  grid: number;
  toWalls: boolean;
  toCorners: boolean;
  toRoomCentre: boolean;
  toObjects: boolean;
  toAlignmentGuides: boolean;
}

export const DEFAULT_SNAP: SnapSettings = {
  enabled: true,
  tolerance: 0.12,
  grid: 0.05,
  toWalls: true,
  toCorners: true,
  toRoomCentre: true,
  toObjects: true,
  toAlignmentGuides: true,
};

export type SnapKind =
  | "wall"
  | "corner"
  | "room-centre"
  | "object-edge"
  | "object-centre"
  | "guide"
  | "grid";

/** A line the object snapped to, drawn as feedback while dragging. */
export interface SnapIndicator {
  kind: SnapKind;
  axis: "x" | "y";
  /** Constant coordinate of the guide line, in plan metres. */
  at: number;
  /** Extent to draw the guide over, in plan metres. */
  from: number;
  to: number;
  label: string;
}

export interface SnapResult {
  position: { x: number; y: number };
  indicators: SnapIndicator[];
}

interface Candidate {
  /** Where the object's centre would end up if this candidate won. */
  centre: number;
  kind: SnapKind;
  label: string;
  /** Span of the guide along the other axis. */
  from: number;
  to: number;
  /** Lower sorts first when two candidates are equally near. */
  priority: number;
}

/**
 * Snap a dragged object's centre.
 *
 * `moving` carries the object's own dimensions and rotation, because snapping
 * an *edge* to a wall means placing the centre half a footprint away from it —
 * the user is aligning the sofa's back, not its midpoint.
 */
export function snapPosition(
  desired: { x: number; y: number },
  moving: Pick<ReviewObject, "dimensions" | "rotation_z" | "id">,
  room: ReviewRoom,
  neighbours: ReviewObject[],
  settings: SnapSettings,
): SnapResult {
  if (!settings.enabled) return { position: desired, indicators: [] };

  const half = axisAlignedHalfExtents(moving);
  const xs = collectCandidates("x", desired, half, room, neighbours, settings);
  const ys = collectCandidates("y", desired, half, room, neighbours, settings);

  const bestX = pick(xs, desired.x, settings.tolerance);
  const bestY = pick(ys, desired.y, settings.tolerance);

  const indicators: SnapIndicator[] = [];
  if (bestX) {
    indicators.push({
      kind: bestX.kind, axis: "x", at: bestX.centre,
      from: bestX.from, to: bestX.to, label: bestX.label,
    });
  }
  if (bestY) {
    indicators.push({
      kind: bestY.kind, axis: "y", at: bestY.centre,
      from: bestY.from, to: bestY.to, label: bestY.label,
    });
  }

  return {
    position: { x: bestX?.centre ?? desired.x, y: bestY?.centre ?? desired.y },
    indicators,
  };
}

function pick(
  candidates: Candidate[],
  desired: number,
  tolerance: number,
): Candidate | null {
  let best: Candidate | null = null;
  let bestDistance = tolerance;

  for (const candidate of candidates) {
    const distance = Math.abs(candidate.centre - desired);
    if (distance > tolerance) continue;
    // Strictly nearer wins; equal distance is broken by priority so a wall
    // beats the grid line that happens to sit on it.
    if (
      distance < bestDistance - 1e-9 ||
      (best !== null && Math.abs(distance - bestDistance) < 1e-9 &&
        candidate.priority < best.priority)
    ) {
      best = candidate;
      bestDistance = Math.min(bestDistance, distance);
    }
  }
  return best;
}

/**
 * Half-extents of the object's *axis-aligned* bounding box.
 *
 * A rotated object's edge is not parallel to its width, so snapping its
 * bounding box is the honest approximation: it is what the user sees as the
 * object's extent in the plan.
 */
function axisAlignedHalfExtents(
  moving: Pick<ReviewObject, "dimensions" | "rotation_z">,
): { x: number; y: number } {
  const theta = (moving.rotation_z * Math.PI) / 180;
  const cos = Math.abs(Math.cos(theta));
  const sin = Math.abs(Math.sin(theta));
  const { width, depth } = moving.dimensions;
  return {
    x: (width * cos + depth * sin) / 2,
    y: (width * sin + depth * cos) / 2,
  };
}

function collectCandidates(
  axis: "x" | "y",
  desired: { x: number; y: number },
  half: { x: number; y: number },
  room: ReviewRoom,
  neighbours: ReviewObject[],
  settings: SnapSettings,
): Candidate[] {
  const out: Candidate[] = [];
  const index = axis === "x" ? 0 : 1;
  const other = axis === "x" ? 1 : 0;
  const halfThis = axis === "x" ? half.x : half.y;

  const min = room.bounds_min[index];
  const max = room.bounds_max[index];
  const spanFrom = room.bounds_min[other];
  const spanTo = room.bounds_max[other];

  // --- Walls: align the object's near edge with the room boundary ---------
  if (settings.toWalls) {
    for (const [edge, label] of [
      [min, axis === "x" ? "left wall" : "bottom wall"],
      [max, axis === "x" ? "right wall" : "top wall"],
    ] as const) {
      const inward = edge === min ? 1 : -1;
      out.push({
        centre: edge + inward * halfThis,
        kind: "wall",
        label: `against ${label}`,
        from: spanFrom,
        to: spanTo,
        priority: 1,
      });
    }
  }

  // --- Corners: the polygon's own vertices, for non-rectangular rooms -----
  if (settings.toCorners) {
    for (const vertex of room.polygon) {
      const at = vertex[index];
      // Both sides, since which way is "inward" depends on the corner.
      for (const sign of [1, -1]) {
        out.push({
          centre: at + sign * halfThis,
          kind: "corner",
          label: "corner",
          from: spanFrom,
          to: spanTo,
          priority: 2,
        });
      }
    }
  }

  if (settings.toRoomCentre) {
    out.push({
      centre: (min + max) / 2,
      kind: "room-centre",
      label: "room centre",
      from: spanFrom,
      to: spanTo,
      priority: 3,
    });
  }

  // --- Neighbours: centre-to-centre, and edge-to-edge --------------------
  if (settings.toObjects || settings.toAlignmentGuides) {
    for (const neighbour of neighbours) {
      const theirHalf = axisAlignedHalfExtents(neighbour);
      const theirCentre = axis === "x" ? neighbour.position.x : neighbour.position.y;
      const theirHalfThis = axis === "x" ? theirHalf.x : theirHalf.y;
      const theirOther = axis === "x" ? neighbour.position.y : neighbour.position.x;
      const theirHalfOther = axis === "x" ? theirHalf.y : theirHalf.x;

      if (settings.toAlignmentGuides) {
        out.push({
          centre: theirCentre,
          kind: "object-centre",
          label: `aligned with ${neighbour.category.replace(/_/g, " ")}`,
          from: Math.min(theirOther - theirHalfOther, desired[axis === "x" ? "y" : "x"]),
          to: Math.max(theirOther + theirHalfOther, desired[axis === "x" ? "y" : "x"]),
          priority: 4,
        });
      }

      if (settings.toObjects) {
        // Flush against either side of the neighbour.
        for (const sign of [1, -1]) {
          out.push({
            centre: theirCentre + sign * (theirHalfThis + halfThis),
            kind: "object-edge",
            label: `beside ${neighbour.category.replace(/_/g, " ")}`,
            from: theirOther - theirHalfOther,
            to: theirOther + theirHalfOther,
            priority: 5,
          });
        }
        // Edges flush — two objects ending on the same line.
        for (const sign of [1, -1]) {
          out.push({
            centre: theirCentre + sign * (theirHalfThis - halfThis),
            kind: "guide",
            label: `edge with ${neighbour.category.replace(/_/g, " ")}`,
            from: theirOther - theirHalfOther,
            to: theirOther + theirHalfOther,
            priority: 6,
          });
        }
      }
    }
  }

  // --- Grid: the fallback, and deliberately lowest priority --------------
  if (settings.grid > 0) {
    const snapped = Math.round(desired[axis] / settings.grid) * settings.grid;
    out.push({
      centre: snapped,
      kind: "grid",
      label: `${Math.round(settings.grid * 100)} cm grid`,
      from: spanFrom,
      to: spanTo,
      priority: 9,
    });
  }

  return out;
}

// ---------------------------------------------------------------------------
// Rotation
// ---------------------------------------------------------------------------

/**
 * Rotate an object to face squarely onto the wall nearest its back.
 *
 * "Rotate to wall" is the single most common furniture operation and the most
 * annoying to do by hand, because being 2° off is visible in a render and
 * invisible in a plan.
 */
export function rotationToNearestWall(
  object: Pick<ReviewObject, "position">,
  room: ReviewRoom,
): number | null {
  if (room.polygon.length < 2) return null;

  let best: { angle: number; distance: number } | null = null;

  for (let i = 0; i < room.polygon.length; i += 1) {
    const a = room.polygon[i];
    const b = room.polygon[(i + 1) % room.polygon.length];
    const { distance } = closestPointOnSegment(
      { x: object.position.x, y: object.position.y }, a, b,
    );
    // The object's front (local +Y) should point away from the wall, so the
    // rotation is the wall's inward normal expressed as a heading.
    const wallAngle = Math.atan2(b[1] - a[1], b[0] - a[0]);
    const inward = interiorSide(a, b, room) * (Math.PI / 2);
    const angle = ((wallAngle + inward) * 180) / Math.PI - 90;

    if (!best || distance < best.distance) {
      best = { angle: normaliseDegrees(angle), distance };
    }
  }

  return best ? best.angle : null;
}

/** +1 or -1: which perpendicular of segment a→b points into the room. */
function interiorSide(a: number[], b: number[], room: ReviewRoom): number {
  const midpoint = { x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2 };
  const centre = polygonCentroid(room.polygon);
  const normal = { x: -(b[1] - a[1]), y: b[0] - a[0] };
  const toward = { x: centre.x - midpoint.x, y: centre.y - midpoint.y };
  return normal.x * toward.x + normal.y * toward.y >= 0 ? 1 : -1;
}

export function normaliseDegrees(value: number): number {
  return ((value % 360) + 360) % 360;
}

// ---------------------------------------------------------------------------
// Alignment and distribution
// ---------------------------------------------------------------------------

export type AlignMode =
  | "left"
  | "right"
  | "top"
  | "bottom"
  | "centre-x"
  | "centre-y";

/**
 * New centres for an aligned group.
 *
 * Edges are aligned using axis-aligned bounding boxes, so a rotated object
 * lines up by what the user can see rather than by its untransformed width.
 */
export function align(
  objects: ReviewObject[],
  mode: AlignMode,
): Record<string, { x: number; y: number }> {
  if (objects.length < 2) return {};

  const boxes = objects.map((object) => ({ object, half: axisAlignedHalfExtents(object) }));
  const result: Record<string, { x: number; y: number }> = {};

  const lefts = boxes.map((b) => b.object.position.x - b.half.x);
  const rights = boxes.map((b) => b.object.position.x + b.half.x);
  const bottoms = boxes.map((b) => b.object.position.y - b.half.y);
  const tops = boxes.map((b) => b.object.position.y + b.half.y);

  for (const { object, half } of boxes) {
    let { x, y } = object.position;
    switch (mode) {
      case "left":
        x = Math.min(...lefts) + half.x;
        break;
      case "right":
        x = Math.max(...rights) - half.x;
        break;
      case "bottom":
        y = Math.min(...bottoms) + half.y;
        break;
      case "top":
        y = Math.max(...tops) - half.y;
        break;
      case "centre-x":
        x = average(boxes.map((b) => b.object.position.x));
        break;
      case "centre-y":
        y = average(boxes.map((b) => b.object.position.y));
        break;
    }
    result[object.id] = { x, y };
  }

  return result;
}

/**
 * Even out the gaps between three or more objects along an axis.
 *
 * The extremes stay put — they define the span the user already chose — and
 * the objects between them are redistributed so the *gaps* are equal. Equal
 * gaps rather than equal centre spacing is what reads as evenly spaced when
 * the objects differ in size.
 */
export function distribute(
  objects: ReviewObject[],
  axis: "x" | "y",
): Record<string, { x: number; y: number }> {
  if (objects.length < 3) return {};

  const boxes = objects
    .map((object) => ({ object, half: axisAlignedHalfExtents(object) }))
    .sort((a, b) => a.object.position[axis] - b.object.position[axis]);

  const first = boxes[0];
  const last = boxes[boxes.length - 1];
  const halfOf = (entry: typeof first) => (axis === "x" ? entry.half.x : entry.half.y);

  const span =
    last.object.position[axis] - halfOf(last) -
    (first.object.position[axis] + halfOf(first));
  const occupied = boxes.slice(1, -1).reduce((total, entry) => total + halfOf(entry) * 2, 0);
  const gap = (span - occupied) / (boxes.length - 1);

  const result: Record<string, { x: number; y: number }> = {};
  let cursor = first.object.position[axis] + halfOf(first) + gap;

  for (const entry of boxes.slice(1, -1)) {
    const centre = cursor + halfOf(entry);
    result[entry.object.id] = {
      x: axis === "x" ? centre : entry.object.position.x,
      y: axis === "y" ? centre : entry.object.position.y,
    };
    cursor = centre + halfOf(entry) + gap;
  }

  return result;
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function average(values: number[]): number {
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function polygonCentroid(polygon: number[][]): { x: number; y: number } {
  let x = 0;
  let y = 0;
  for (const [px, py] of polygon) {
    x += px;
    y += py;
  }
  return { x: x / polygon.length, y: y / polygon.length };
}

function closestPointOnSegment(
  point: { x: number; y: number },
  a: number[],
  b: number[],
): { point: { x: number; y: number }; distance: number } {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) {
    return { point: { x: a[0], y: a[1] }, distance: Math.hypot(point.x - a[0], point.y - a[1]) };
  }
  const t = Math.max(
    0,
    Math.min(1, ((point.x - a[0]) * dx + (point.y - a[1]) * dy) / lengthSquared),
  );
  const closest = { x: a[0] + t * dx, y: a[1] + t * dy };
  return {
    point: closest,
    distance: Math.hypot(point.x - closest.x, point.y - closest.y),
  };
}
