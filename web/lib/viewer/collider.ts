/**
 * ArchX3D — Collision body
 * ========================
 * The capsule-versus-BVH solver the walk camera uses to stand on floors and stop
 * at walls. See `components/viewer/CollisionManager.tsx` for why a BVH and a
 * capsule rather than raycasts.
 *
 * Lives here, apart from the React hook that owns its lifetime, for the same
 * reason the rest of `lib/viewer` does: it is plain three.js with no React, so
 * it can be exercised in Node against real geometry. The walk-mode black screen
 * (G5) was a camera falling through a void the solver was never asked about;
 * the regression tests for it drive this class directly.
 */

import * as THREE from "three";
import { MeshBVH, StaticGeometryGenerator } from "three-mesh-bvh";

import { subStepCount } from "./movement";

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
    const ray = new THREE.Ray(new THREE.Vector3(x, from, z), new THREE.Vector3(0, -1, 0));
    // `near`/`far` must be passed to `raycastFirst` itself. This used to build
    // a `Raycaster` with them and hand over only its `.ray`, so `maxDrop` was
    // silently infinite and "the floor below" could be any surface under the
    // building — which is how a spawn over a void looked supported.
    const hit = this.bvh.raycastFirst(ray, THREE.FrontSide, 0, maxDrop);
    return hit ? hit.point.y : null;
  }

  /**
   * Open distance from a point along a horizontal direction, capped at `reach`.
   *
   * Both sides count: a wall is in the way whichever way its faces were wound,
   * and a single-sided panel seen from behind is still a panel.
   */
  clearance(x: number, y: number, z: number, dx: number, dz: number, reach: number): number {
    const direction = new THREE.Vector3(dx, 0, dz);
    if (direction.lengthSq() < 1e-12 || !(reach > 0)) return 0;
    const ray = new THREE.Ray(new THREE.Vector3(x, y, z), direction.normalize());
    const hit = this.bvh.raycastFirst(ray, THREE.DoubleSide, 0, reach);
    return hit ? hit.distance : reach;
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
