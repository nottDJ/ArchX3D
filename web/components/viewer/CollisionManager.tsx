"use client";

/**
 * ArchX3D — Collision
 * ===================
 * Stops the walk camera passing through walls, doors, columns, stairs and the
 * roof, at a cost that does not depend on how big the building is.
 *
 * Why a BVH and not raycasts
 * --------------------------
 * The obvious implementation casts a few rays from the camera and stops when
 * one hits something. It fails in three ways that all show up immediately in a
 * real building: rays miss thin geometry at glancing angles, so you slip
 * through a wall you approached diagonally; a handful of rays cannot describe a
 * body, so you clip corners; and per-triangle testing against a 400,000-triangle
 * model is O(n) per frame, which is a slideshow.
 *
 * Instead the collidable meshes are merged once into a single geometry with a
 * bounding volume hierarchy over it, and each frame a *capsule* — the camera's
 * body — is swept against that tree. The BVH turns "which triangles are near
 * this capsule?" into a logarithmic descent, so the per-frame cost is set by how
 * much geometry is *within arm's reach*, not by how much exists.
 *
 * The capsule
 * -----------
 * A vertical segment with a radius: a cylinder with hemispherical caps. It is
 * the right shape because it cannot catch on a corner — there is no edge for a
 * corner to snag — so walking along a wall slides instead of stuttering, which
 * is the difference between a viewer that feels solid and one that feels stuck.
 *
 *     eye ──────●  ┐  segment.start = eye − radius
 *               │  │
 *               │  ├─ height
 *               │  │
 *     feet ─────●  ┘  segment.end   = eye − height + radius
 *
 * Resolution is iterative: find every triangle the capsule overlaps, push the
 * capsule out of each by its penetration depth, then move the camera to wherever
 * the capsule ended up. Velocity along the push-out direction is cancelled so
 * that walking into a wall at an angle keeps the sideways component — the
 * sliding that makes movement feel smooth rather than sticky.
 */

import { useEffect, useMemo, useState } from "react";
import * as THREE from "three";
import { MeshBVHHelper } from "three-mesh-bvh";

import type { ModelIndex } from "@/hooks/useRoofDetection";
import { buildCollider, type Collider } from "@/lib/viewer/collider";
import { MOVEMENT } from "@/lib/viewer/movement";

export { CAPSULE, Collider, buildCollider } from "@/lib/viewer/collider";
export type { ResolveOptions, ResolveResult } from "@/lib/viewer/collider";

/**
 * Build a collider for a model, and rebuild it when the model changes.
 *
 * Deliberately keyed on the *model*, not on the current view mode: hiding the
 * roof must not let you walk out through the ceiling, and a user in Furniture
 * view still expects walls to be solid. Visibility and collision are separate
 * questions and are answered separately.
 */
export function useCollider(index: ModelIndex | null): Collider | null {
  const [collider, setCollider] = useState<Collider | null>(null);

  const colliders = useMemo(() => index?.colliders ?? [], [index]);

  useEffect(() => {
    if (colliders.length === 0) {
      setCollider(null);
      return;
    }

    const built = buildCollider(colliders);
    setCollider(built);

    return () => {
      built?.dispose();
      setCollider(null);
    };
  }, [colliders]);

  return collider;
}

// ---------------------------------------------------------------------------
// Debug view
// ---------------------------------------------------------------------------

/**
 * Draws the BVH's bounding boxes. Development aid, never shipped enabled —
 * seeing where the tree splits is the fastest way to understand why a
 * particular wall is not stopping the camera.
 */
export function CollisionDebug({
  collider,
  depth = 12,
}: {
  collider: Collider | null;
  depth?: number;
}) {
  const helper = useMemo(() => {
    if (!collider) return null;
    const mesh = new THREE.Mesh(collider.geometry);
    const created = new MeshBVHHelper(mesh, depth);
    created.displayParents = false;
    return created;
  }, [collider, depth]);

  useEffect(() => () => helper?.dispose(), [helper]);

  if (!helper) return null;
  return <primitive object={helper} />;
}

export { MOVEMENT };
