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
import { MeshBVH, MeshBVHHelper, StaticGeometryGenerator } from "three-mesh-bvh";

import type { ModelIndex } from "@/hooks/useRoofDetection";
import { MOVEMENT, subStepCount } from "@/lib/viewer/movement";

// ---------------------------------------------------------------------------
// Tuning
// ---------------------------------------------------------------------------

export const CAPSULE = {
  /**
   * Half the camera's body width. A shade under a door's half-width so the
   * viewer fits through a 0.8 m doorway without brushing both jambs, and wide
   * enough that a wall of any realistic thickness cannot be crossed in one
   * sub-step.
   */
  radius: 0.28,
  /** Highest step the camera walks over rather than into — a stair tread. */
  stepHeight: 0.35,
} as const;

export interface ResolveOptions {
  /** Eye height above the feet, metres. */
  readonly height: number;
  /** Whether gravity and ground contact apply. */
  readonly gravity: boolean;
}

export interface ResolveResult {
  readonly grounded: boolean;
  /** True when the solver actually pushed the camera out of something. */
  readonly collided: boolean;
}

// ---------------------------------------------------------------------------
// Collider
// ---------------------------------------------------------------------------

/**
 * A merged, BVH-accelerated collision body for one model.
 *
 * All scratch objects are instance fields rather than locals: `resolve` runs up
 * to six times per frame at 144 Hz, and allocating a dozen vectors each time
 * hands the garbage collector ~10,000 objects a second, which shows up as
 * periodic stutter — precisely the "camera jitter" this has to avoid.
 */
export class Collider {
  readonly bvh: MeshBVH;
  readonly geometry: THREE.BufferGeometry;
  readonly triangleCount: number;

  private readonly segment = new THREE.Line3();
  private readonly startBefore = new THREE.Vector3();
  private readonly box = new THREE.Box3();
  private readonly triPoint = new THREE.Vector3();
  private readonly capsulePoint = new THREE.Vector3();
  private readonly direction = new THREE.Vector3();
  private readonly delta = new THREE.Vector3();

  constructor(geometry: THREE.BufferGeometry) {
    this.geometry = geometry;
    this.bvh = new MeshBVH(geometry);
    const position = geometry.getAttribute("position");
    this.triangleCount = geometry.index
      ? geometry.index.count / 3
      : position
        ? position.count / 3
        : 0;
  }

  dispose(): void {
    this.geometry.dispose();
  }

  /**
   * Advance the camera by one frame, resolving collisions.
   *
   * Mutates `eye` and `velocity` in place. Long moves are split into sub-steps
   * so a running camera cannot tunnel through a wall between two frames.
   */
  resolve(
    eye: THREE.Vector3,
    velocity: THREE.Vector3,
    dt: number,
    options: ResolveOptions,
  ): ResolveResult {
    const steps = subStepCount(dt, velocity.length());
    const stepDt = dt / steps;

    let grounded = false;
    let collided = false;

    for (let i = 0; i < steps; i += 1) {
      const result = this.step(eye, velocity, stepDt, options);
      grounded = grounded || result.grounded;
      collided = collided || result.collided;
    }

    return { grounded, collided };
  }

  private step(
    eye: THREE.Vector3,
    velocity: THREE.Vector3,
    dt: number,
    options: ResolveOptions,
  ): ResolveResult {
    const radius = CAPSULE.radius;
    const height = Math.max(options.height, radius * 2 + 0.05);

    eye.addScaledVector(velocity, dt);

    this.segment.start.set(eye.x, eye.y - radius, eye.z);
    this.segment.end.set(eye.x, eye.y - height + radius, eye.z);
    this.startBefore.copy(this.segment.start);

    // The capsule's world bounds, used to reject whole BVH subtrees at once.
    this.box.makeEmpty();
    this.box.expandByPoint(this.segment.start);
    this.box.expandByPoint(this.segment.end);
    this.box.min.addScalar(-radius);
    this.box.max.addScalar(radius);

    let hits = 0;

    this.bvh.shapecast({
      intersectsBounds: (box) => box.intersectsBox(this.box),
      intersectsTriangle: (triangle) => {
        const distance = triangle.closestPointToSegment(
          this.segment,
          this.triPoint,
          this.capsulePoint,
        );

        if (distance < radius) {
          const depth = radius - distance;
          this.direction.copy(this.capsulePoint).sub(this.triPoint);

          // A capsule centre lying exactly on a face gives a zero-length
          // direction, and normalising it yields NaN — which propagates into
          // the camera matrix and blanks the screen. Skip: the next sub-step
          // has moved off the degenerate point.
          if (this.direction.lengthSq() < 1e-12) return;

          this.direction.normalize();
          this.segment.start.addScaledVector(this.direction, depth);
          this.segment.end.addScaledVector(this.direction, depth);
          hits += 1;
        }
      },
    });

    this.delta.subVectors(this.segment.start, this.startBefore);

    // Being pushed up by more than gravity could have pulled us down this frame
    // means we are standing on something rather than brushing past it.
    const grounded =
      options.gravity && this.delta.y > Math.abs(dt * velocity.y * 0.25);

    eye.set(
      this.segment.start.x,
      this.segment.start.y + radius,
      this.segment.start.z,
    );

    if (grounded) {
      velocity.y = 0;
    } else if (hits > 0 && this.delta.lengthSq() > 1e-12) {
      // Cancel only the velocity heading into the surface. Keeping the
      // tangential part is what lets the camera slide along a wall instead of
      // stopping dead against it.
      this.delta.normalize();
      velocity.addScaledVector(this.delta, -this.delta.dot(velocity));
    }

    return { grounded, collided: hits > 0 };
  }

  /**
   * Nearest solid point below a plan position, or `null` over a void.
   *
   * Used to drop the camera onto the floor when entering walk mode, so a saved
   * eye height from a different model does not leave you inside a slab.
   */
  groundBelow(x: number, z: number, from: number, maxDrop = 40): number | null {
    const raycaster = new THREE.Raycaster(
      new THREE.Vector3(x, from, z),
      new THREE.Vector3(0, -1, 0),
      0,
      maxDrop,
    );
    const hit = this.bvh.raycastFirst(raycaster.ray, THREE.FrontSide);
    return hit ? hit.point.y : null;
  }
}

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

/**
 * Merge the model's collidable meshes into one BVH.
 *
 * `StaticGeometryGenerator` bakes each mesh's world transform into the merged
 * geometry, so the result is in world space and needs no matrix at test time.
 * Only positions are kept — normals, UVs and colours are several times the data
 * and collision never reads them.
 *
 * Returns `null` when there is nothing to collide with, which is a real case:
 * a model in Furniture-only view, or a GLB whose meshes all failed to classify
 * as structure. The caller falls back to free movement rather than trapping the
 * user at the origin.
 */
export function buildCollider(meshes: readonly THREE.Mesh[]): Collider | null {
  const usable = meshes.filter(
    (mesh) => mesh.geometry && mesh.geometry.getAttribute("position"),
  );
  if (usable.length === 0) return null;

  try {
    const generator = new StaticGeometryGenerator(usable as THREE.Mesh[]);
    generator.attributes = ["position"];
    generator.useGroups = false;

    const merged = generator.generate();
    if (!merged.getAttribute("position")) return null;

    return new Collider(merged);
  } catch {
    // A malformed mesh — mismatched attributes, a zero-length index — must not
    // cost the user the whole viewer. Walk mode falls back to no collision.
    return null;
  }
}

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
