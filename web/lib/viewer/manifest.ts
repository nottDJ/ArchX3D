/**
 * ArchX3D — Scene manifest parsing
 * ================================
 * Reads the `archx3d` block the generator writes onto the glTF scene, and
 * survives every version of the file that does not have one.
 *
 * The generator stamps `bpy.context.scene["archx3d"]` with a JSON string, which
 * `export_extras=True` carries into the glTF scene's `extras`. `GLTFLoader`
 * copies scene extras onto `gltf.scene.userData`.
 *
 * Everything here is defensive. The manifest is optional by design: a GLB built
 * before the metadata pass, or by a third-party tool, must still open — it just
 * loses room navigation and the minimap, which is exactly what "gracefully
 * disable" means. So a missing block, a malformed one, a truncated room list
 * and a room with no polygon all produce the same outcome: as much as can be
 * read, and no exception.
 */

import type { RoomInfo, SceneManifest } from "../../types/viewer";

const EMPTY: SceneManifest = {
  version: "0",
  generator: "unknown",
  up_axis: "Y",
  units: "metre",
  rooms: [],
};

// ---------------------------------------------------------------------------
// Coercion helpers
// ---------------------------------------------------------------------------

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function pair(value: unknown): readonly [number, number] | null {
  if (!Array.isArray(value) || value.length < 2) return null;
  const x = value[0];
  const y = value[1];
  if (typeof x !== "number" || typeof y !== "number") return null;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return [x, y];
}

function polygon(value: unknown): ReadonlyArray<readonly [number, number]> {
  if (!Array.isArray(value)) return [];
  const points: Array<readonly [number, number]> = [];
  for (const entry of value) {
    const point = pair(entry);
    if (point) points.push(point);
  }
  return points;
}

/** Turn `living_room` into `Living Room` for a room with no name. */
function titleCase(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

// ---------------------------------------------------------------------------
// Rooms
// ---------------------------------------------------------------------------

/**
 * Parse one room, or `null` if it has no usable footprint.
 *
 * A room without bounds cannot be flown to, drawn on a minimap, or highlighted,
 * so admitting it would put a dead entry in the room list. Dropping it is the
 * honest outcome — the same reasoning the pipeline applies to a detection it
 * cannot place.
 */
export function parseRoom(raw: unknown): RoomInfo | null {
  if (typeof raw !== "object" || raw === null) return null;
  const input = raw as Record<string, unknown>;

  const id = str(input.id);
  if (!id) return null;

  const boundsMin = pair(input.bounds_min);
  const boundsMax = pair(input.bounds_max);
  if (!boundsMin || !boundsMax) return null;

  // A zero-area room is a segmentation artefact, not a place you can stand.
  if (
    Math.abs(boundsMax[0] - boundsMin[0]) < 1e-3 ||
    Math.abs(boundsMax[1] - boundsMin[1]) < 1e-3
  ) {
    return null;
  }

  const roomType = str(input.room_type, "room");

  return {
    id,
    name: str(input.name) || titleCase(roomType) || id,
    room_type: roomType,
    style: str(input.style) || undefined,
    area_m2: num(input.area_m2),
    // A room with no stated height still needs one to stand in.
    ceiling_height: num(input.ceiling_height, 3),
    bounds_min: boundsMin,
    bounds_max: boundsMax,
    polygon: polygon(input.polygon),
    connected_to: Array.isArray(input.connected_to)
      ? input.connected_to.filter((v): v is string => typeof v === "string")
      : [],
    object_count: Math.max(0, Math.round(num(input.object_count))),
  };
}

// ---------------------------------------------------------------------------
// Manifest
// ---------------------------------------------------------------------------

/**
 * Parse the manifest from a scene's `userData`.
 *
 * Accepts the value as either a JSON string (how Blender custom properties
 * travel) or an already-parsed object (how a hand-authored glTF might carry
 * it), because both are legitimate and the difference is not the caller's
 * problem.
 */
export function parseManifest(userData: unknown): SceneManifest {
  if (typeof userData !== "object" || userData === null) return EMPTY;

  const block = (userData as Record<string, unknown>).archx3d;
  if (block === undefined || block === null) return EMPTY;

  let payload: unknown = block;
  if (typeof block === "string") {
    try {
      payload = JSON.parse(block);
    } catch {
      return EMPTY;
    }
  }

  if (typeof payload !== "object" || payload === null) return EMPTY;
  const input = payload as Record<string, unknown>;

  const rooms: RoomInfo[] = [];
  if (Array.isArray(input.rooms)) {
    for (const entry of input.rooms) {
      const room = parseRoom(entry);
      if (room) rooms.push(room);
    }
  }

  return {
    version: str(input.version, "0"),
    generator: str(input.generator, "unknown"),
    // Anything other than an explicit Z means Y, which is what glTF mandates
    // and what every exporter produces.
    up_axis: input.up_axis === "Z" ? "Z" : "Y",
    units: str(input.units, "metre"),
    // Largest first, so the room list opens on the space that matters most and
    // `interiorSpawn` has an obvious room to prefer.
    rooms: rooms.sort((a, b) => b.area_m2 - a.area_m2),
  };
}

/** True when there is enough metadata for room navigation and the minimap. */
export function hasRoomMetadata(manifest: SceneManifest): boolean {
  return manifest.rooms.length > 0;
}

/** Plan-space bounding box of every room, for the minimap's viewBox. */
export function roomsExtent(
  rooms: readonly RoomInfo[],
): { min: readonly [number, number]; max: readonly [number, number] } | null {
  if (rooms.length === 0) return null;

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const room of rooms) {
    minX = Math.min(minX, room.bounds_min[0]);
    minY = Math.min(minY, room.bounds_min[1]);
    maxX = Math.max(maxX, room.bounds_max[0]);
    maxY = Math.max(maxY, room.bounds_max[1]);
  }

  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null;
  return { min: [minX, minY], max: [maxX, maxY] };
}

/**
 * Which room contains a plan point, or `null` outside every room.
 *
 * Bounding-box test rather than point-in-polygon: room boxes rarely overlap in
 * a segmented plan, the boxes are always present where polygons may not be, and
 * being briefly wrong about which room you are in costs a minimap highlight
 * rather than anything structural.
 */
export function roomAt(
  rooms: readonly RoomInfo[],
  x: number,
  y: number,
): RoomInfo | null {
  for (const room of rooms) {
    if (
      x >= room.bounds_min[0] &&
      x <= room.bounds_max[0] &&
      y >= room.bounds_min[1] &&
      y <= room.bounds_max[1]
    ) {
      return room;
    }
  }
  return null;
}
