"use client";

/**
 * ArchX3D — Model indexing and roof detection
 * ===========================================
 * Walks a loaded model once and builds everything the rest of the viewer needs
 * to answer questions about it: what each mesh is, which meshes form the roof,
 * what belongs to which room, how big the building is, and what it costs to
 * draw.
 *
 * Why one pass and one index
 * --------------------------
 * Hiding the roof, switching to Structure, flying to a room and computing
 * statistics are four features that would each traverse the scene. On a
 * 100,000-object building that is four traversals per interaction. Indexing
 * once at load time turns every one of them into an array lookup, and the index
 * is rebuilt only when the model itself changes.
 *
 * Two passes, not one
 * -------------------
 * Scene bounds have to be known before the geometric roof test can ask "is this
 * plate near the top of the building?", so bounds are measured first. The
 * measurement is cheap — one `Box3.setFromObject` on the root — and the second
 * pass only computes per-mesh bounds for the handful of meshes that reach the
 * geometric rung at all.
 */

import { useMemo } from "react";
import * as THREE from "three";

import { classifyNode, type SceneExtent } from "@/lib/viewer/classify";
import type { Box } from "@/lib/viewer/bounds";
import type {
  Classification,
  ClassificationSource,
  ElementKind,
  ModelStats,
} from "@/types/viewer";
import { ELEMENT_KINDS } from "@/types/viewer";

export interface ModelIndex {
  /** Every mesh and light, with the verdict that classified it. */
  readonly entries: ReadonlyArray<ModelEntry>;
  /** Meshes grouped by kind — the lookup the visibility manager walks. */
  readonly byKind: ReadonlyMap<ElementKind, ReadonlyArray<THREE.Object3D>>;
  /** Meshes grouped by scene-graph room id. Empty without metadata. */
  readonly byRoom: ReadonlyMap<string, ReadonlyArray<THREE.Object3D>>;
  /** Everything classified as roof — what the roof toggle hides. */
  readonly roofs: ReadonlyArray<THREE.Object3D>;
  /** Solid geometry the walk camera must not pass through. */
  readonly colliders: ReadonlyArray<THREE.Mesh>;
  readonly bounds: Box;
  readonly stats: ModelStats;
}

export interface ModelEntry {
  readonly object: THREE.Object3D;
  readonly classification: Classification;
}

/**
 * Kinds the camera collides with.
 *
 * Walls, floors, closed doors, columns, stairs and the roof — the building's
 * fabric. Furniture is deliberately excluded: an architectural walkthrough that
 * snags on a rug or cannot round a coffee table feels broken, and users
 * consistently expect to walk *through* the contents of a room and *around* its
 * structure. `unknown` is included because an unclassified mesh in a building
 * is far more likely to be part of it than not, and a false collider is a
 * smaller failure than falling out of the world.
 */
const COLLIDING_KINDS: ReadonlySet<ElementKind> = new Set<ElementKind>([
  "wall",
  "floor",
  "roof",
  "structure",
  "opening",
  "unknown",
]);

const EMPTY_BOUNDS: Box = { min: [-1, 0, -1], max: [1, 2, 1] };

// ---------------------------------------------------------------------------

function toBox(box3: THREE.Box3): Box {
  return {
    min: [box3.min.x, box3.min.y, box3.min.z],
    max: [box3.max.x, box3.max.y, box3.max.z],
  };
}

function ancestorNames(object: THREE.Object3D): string[] {
  const names: string[] = [];
  let parent = object.parent;
  // Four levels is deeper than any hierarchy the generator produces, and stops
  // an unexpectedly deep import from walking to the root on every mesh.
  while (parent && names.length < 4) {
    if (parent.name) names.push(parent.name);
    parent = parent.parent;
  }
  return names;
}

function countTriangles(geometry: THREE.BufferGeometry): number {
  if (geometry.index) return geometry.index.count / 3;
  const position = geometry.getAttribute("position");
  return position ? position.count / 3 : 0;
}

// ---------------------------------------------------------------------------

/**
 * Index a loaded model.
 *
 * Returns a stable object for a given scene, so consumers can depend on it
 * directly without re-running on every render.
 */
export function useModelIndex(scene: THREE.Group | null): ModelIndex | null {
  return useMemo(() => (scene ? buildIndex(scene) : null), [scene]);
}

/** Exported for tests and for callers outside React. */
export function buildIndex(scene: THREE.Group): ModelIndex {
  // -- Pass 1: how big is the building? ----------------------------------
  const rootBox = new THREE.Box3().setFromObject(scene);
  const bounds: Box = rootBox.isEmpty() ? EMPTY_BOUNDS : toBox(rootBox);
  const extent: SceneExtent = { min: bounds.min, max: bounds.max };

  // -- Pass 2: classify and index ----------------------------------------
  const entries: ModelEntry[] = [];
  const byKind = new Map<ElementKind, THREE.Object3D[]>();
  const byRoom = new Map<string, THREE.Object3D[]>();
  const colliders: THREE.Mesh[] = [];
  const materials = new Set<THREE.Material>();
  const textures = new Set<THREE.Texture>();
  const bySource: Partial<Record<ClassificationSource, number>> = {};

  let meshes = 0;
  let triangles = 0;

  const scratch = new THREE.Box3();

  scene.traverse((object) => {
    const isMesh = (object as THREE.Mesh).isMesh === true;
    const isLight = (object as THREE.Light).isLight === true;
    if (!isMesh && !isLight) return;

    const classification = classifyNode(
      {
        name: object.name,
        userData: object.userData ?? {},
        ancestors: ancestorNames(object),
        isLight,
        // Only meshes can reach the geometric rung, and computing world bounds
        // for every mesh up front would cost more than the test saves.
        bounds: isMesh ? toBox(scratch.setFromObject(object)) : undefined,
      },
      extent,
    );

    entries.push({ object, classification });

    const kind = classification.kind;
    const kindBucket = byKind.get(kind);
    if (kindBucket) kindBucket.push(object);
    else byKind.set(kind, [object]);

    bySource[classification.source] = (bySource[classification.source] ?? 0) + 1;

    if (classification.roomId) {
      const roomBucket = byRoom.get(classification.roomId);
      if (roomBucket) roomBucket.push(object);
      else byRoom.set(classification.roomId, [object]);
    }

    if (!isMesh) return;

    const mesh = object as THREE.Mesh;
    meshes += 1;
    if (mesh.geometry) triangles += countTriangles(mesh.geometry);

    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      if (!material) continue;
      materials.add(material);
      for (const value of Object.values(material)) {
        if (value instanceof THREE.Texture) textures.add(value);
      }
    }

    if (COLLIDING_KINDS.has(kind)) colliders.push(mesh);
  });

  const byKindCounts = Object.fromEntries(
    ELEMENT_KINDS.map((kind) => [kind, byKind.get(kind)?.length ?? 0]),
  ) as Record<ElementKind, number>;

  const stats: ModelStats = {
    meshes,
    triangles: Math.round(triangles),
    materials: materials.size,
    textures: textures.size,
    bytes: null,
    size: [
      bounds.max[0] - bounds.min[0],
      bounds.max[1] - bounds.min[1],
      bounds.max[2] - bounds.min[2],
    ],
    byKind: byKindCounts,
    bySource,
  };

  return {
    entries,
    byKind,
    byRoom,
    roofs: byKind.get("roof") ?? [],
    colliders,
    bounds,
    stats,
  };
}

/**
 * Whether the model told us what its parts are, or we had to guess.
 *
 * Surfaced in the statistics panel because it changes what the user should
 * expect: a model classified by inference may hide the wrong thing, and knowing
 * that beats wondering why the roof toggle did nothing.
 */
export function detectionQuality(index: ModelIndex | null): {
  declared: number;
  inferred: number;
  ratio: number;
} {
  if (!index || index.entries.length === 0) {
    return { declared: 0, inferred: 0, ratio: 0 };
  }

  const declared =
    (index.stats.bySource.metadata ?? 0) + (index.stats.bySource.category ?? 0);
  const inferred = index.entries.length - declared;

  return { declared, inferred, ratio: declared / index.entries.length };
}

/** True when at least one mesh looks like a roof, so the toggle is meaningful. */
export function useRoofDetection(index: ModelIndex | null): {
  readonly roofs: ReadonlyArray<THREE.Object3D>;
  readonly hasRoof: boolean;
  /** True when the roof was inferred rather than declared. */
  readonly inferred: boolean;
} {
  return useMemo(() => {
    const roofs = index?.roofs ?? [];
    const inferred =
      roofs.length > 0 &&
      (index?.entries ?? []).some(
        (entry) =>
          entry.classification.kind === "roof" &&
          entry.classification.source === "geometry",
      );
    return { roofs, hasRoof: roofs.length > 0, inferred };
  }, [index]);
}
