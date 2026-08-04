/**
 * ArchX3D — First-person movement
 * ===============================
 * The maths behind walk mode: what velocity the keys ask for, how it eases in
 * and out, how mouse motion becomes a look direction, and how far the collision
 * solver is allowed to advance in one step.
 *
 * Pure, and deliberately separate from `CollisionManager`, which owns the BVH.
 * Everything here is arithmetic on numbers, so the feel of the controls —
 * acceleration, run multiplier, pitch limits, tunnelling prevention — can be
 * tested without a browser, a GPU or a model.
 *
 * Frame-rate independence
 * -----------------------
 * Every easing here uses `1 - exp(-k·dt)` rather than a fixed per-frame lerp
 * factor. A fixed factor makes the camera accelerate faster on a 144 Hz display
 * than on a 60 Hz one, which is the most common cause of movement that "feels
 * different on my machine".
 */

export type Vec3 = readonly [number, number, number];

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

/** Which movement keys are currently down. */
export interface MovementInput {
  readonly forward: boolean;
  readonly back: boolean;
  readonly left: boolean;
  readonly right: boolean;
  readonly run: boolean;
  readonly jump: boolean;
  /** Free-fly only — walk mode gets its height from gravity and the floor. */
  readonly up?: boolean;
  readonly down?: boolean;
}

export const NO_INPUT: MovementInput = {
  forward: false,
  back: false,
  left: false,
  right: false,
  run: false,
  jump: false,
};

/**
 * Keyboard layout.
 *
 * Both WASD and the arrow keys, because a viewer embedded in a page is often
 * driven one-handed while the other hand is on the mouse. Matched on
 * `KeyboardEvent.code`, so it is layout-independent: `KeyW` is the same
 * physical key on AZERTY, where `event.key` would report "z".
 */
export function inputFromCodes(codes: ReadonlySet<string>): MovementInput {
  return {
    forward: codes.has("KeyW") || codes.has("ArrowUp"),
    back: codes.has("KeyS") || codes.has("ArrowDown"),
    left: codes.has("KeyA") || codes.has("ArrowLeft"),
    right: codes.has("KeyD") || codes.has("ArrowRight"),
    run: codes.has("ShiftLeft") || codes.has("ShiftRight"),
    jump: codes.has("Space"),
    up: codes.has("KeyE") || codes.has("PageUp"),
    down: codes.has("KeyQ") || codes.has("PageDown"),
  };
}

// ---------------------------------------------------------------------------
// Tuning
// ---------------------------------------------------------------------------

export const MOVEMENT = {
  /** Easing rate toward the requested velocity. Higher is snappier. */
  acceleration: 12,
  /** Easing rate back to a stop. Faster than acceleration so stops feel crisp. */
  deceleration: 16,
  /** Metres per second squared. Earth, because the scene is in metres. */
  gravity: 9.81,
  /** Initial upward speed of a jump, when jumping is enabled. */
  jumpSpeed: 4.2,
  /** Terminal downward speed, so a fall through a gap cannot outrun collision. */
  maxFallSpeed: 30,
  /** Pitch stops just short of vertical; at exactly ±90° yaw becomes ambiguous. */
  maxPitch: Math.PI / 2 - 0.02,
  /**
   * Longest distance the solver may advance in one collision step. A capsule
   * moved further than roughly its own radius can pass clean through a wall
   * before anything tests it — the classic tunnelling bug — so fast movement
   * is split into several shorter steps instead.
   */
  maxStepDistance: 0.2,
  /** Ceiling on sub-steps, so a huge `dt` after a tab switch cannot stall. */
  maxSubSteps: 6,
} as const;

// ---------------------------------------------------------------------------
// Velocity
// ---------------------------------------------------------------------------

/**
 * The velocity the keys are asking for, in world space.
 *
 * Diagonals are normalised: without it, holding W and D moves you √2 times
 * faster than W alone, which players notice immediately even if they cannot
 * say why.
 */
export function desiredVelocity(
  input: MovementInput,
  yaw: number,
  walkSpeed: number,
  runMultiplier: number,
): Vec3 {
  const forward = (input.forward ? 1 : 0) - (input.back ? 1 : 0);
  const strafe = (input.right ? 1 : 0) - (input.left ? 1 : 0);
  const lift = (input.up ? 1 : 0) - (input.down ? 1 : 0);

  if (forward === 0 && strafe === 0 && lift === 0) return [0, 0, 0];

  const length = Math.hypot(forward, strafe) || 1;
  const speed = walkSpeed * (input.run ? runMultiplier : 1);

  // Yaw 0 looks down −Z, matching three.js's default camera orientation.
  const sin = Math.sin(yaw);
  const cos = Math.cos(yaw);

  const fx = -sin * (forward / length);
  const fz = -cos * (forward / length);
  const sx = cos * (strafe / length);
  const sz = -sin * (strafe / length);

  return [(fx + sx) * speed, lift * speed, (fz + sz) * speed];
}

/** Frame-rate-independent easing factor for a given rate and timestep. */
export function easeFactor(rate: number, dt: number): number {
  return 1 - Math.exp(-Math.max(0, rate) * Math.max(0, dt));
}

/**
 * Ease the current horizontal velocity toward what the keys want.
 *
 * Stopping uses a higher rate than starting: a walkthrough that coasts to a
 * halt feels like ice, and in an architectural viewer the user is usually
 * stopping *because* they want to look at something in particular.
 */
export function dampVelocity(current: Vec3, desired: Vec3, dt: number): Vec3 {
  const stopping =
    desired[0] === 0 && desired[1] === 0 && desired[2] === 0;
  const factor = easeFactor(
    stopping ? MOVEMENT.deceleration : MOVEMENT.acceleration,
    dt,
  );

  return [
    current[0] + (desired[0] - current[0]) * factor,
    current[1] + (desired[1] - current[1]) * factor,
    current[2] + (desired[2] - current[2]) * factor,
  ];
}

/**
 * Advance vertical velocity by one frame of gravity, and handle a jump.
 *
 * Returns the new vertical speed. Jumping while airborne is ignored, so the
 * camera cannot climb by holding the key.
 */
export function integrateVertical(
  verticalSpeed: number,
  dt: number,
  options: {
    readonly grounded: boolean;
    readonly jumpRequested: boolean;
    readonly jumpEnabled: boolean;
    readonly gravityEnabled: boolean;
  },
): number {
  if (!options.gravityEnabled) return 0;

  if (options.grounded) {
    if (options.jumpRequested && options.jumpEnabled) return MOVEMENT.jumpSpeed;
    // A small downward bias keeps the capsule pressed against the floor, so
    // walking over a seam between two slabs does not read as leaving the ground.
    return Math.min(verticalSpeed, 0) === 0 ? -0.5 : verticalSpeed;
  }

  return Math.max(-MOVEMENT.maxFallSpeed, verticalSpeed - MOVEMENT.gravity * dt);
}

// ---------------------------------------------------------------------------
// Look
// ---------------------------------------------------------------------------

/**
 * Apply one mouse-motion event to the current look direction.
 *
 * Pitch is clamped short of vertical. Yaw is left unwrapped — it is only ever
 * fed to `sin`/`cos`, and wrapping it would make a continuous spin jump.
 */
export function applyLook(
  yaw: number,
  pitch: number,
  movementX: number,
  movementY: number,
  sensitivity: number,
): { yaw: number; pitch: number } {
  return {
    yaw: yaw - movementX * sensitivity,
    pitch: clampPitch(pitch - movementY * sensitivity),
  };
}

export function clampPitch(pitch: number): number {
  return Math.max(-MOVEMENT.maxPitch, Math.min(MOVEMENT.maxPitch, pitch));
}

/** Unit forward vector for a yaw/pitch pair. */
export function lookDirection(yaw: number, pitch: number): Vec3 {
  const cosPitch = Math.cos(pitch);
  return [-Math.sin(yaw) * cosPitch, Math.sin(pitch), -Math.cos(yaw) * cosPitch];
}

// ---------------------------------------------------------------------------
// Stepping
// ---------------------------------------------------------------------------

/**
 * How many collision sub-steps this frame's motion needs.
 *
 * At a run of 8 m/s and 60 fps the camera advances 13 cm per frame, which one
 * step handles. After a stall — a tab switch, a shader compile — `dt` can be
 * half a second, and a single step would teleport the camera through the
 * building. Splitting keeps every step under `maxStepDistance`.
 */
export function subStepCount(dt: number, speed: number): number {
  const distance = Math.abs(speed) * Math.max(0, dt);
  if (!Number.isFinite(distance) || distance <= MOVEMENT.maxStepDistance) return 1;
  return Math.min(
    MOVEMENT.maxSubSteps,
    Math.ceil(distance / MOVEMENT.maxStepDistance),
  );
}

/**
 * Clamp a frame delta to something physically sensible.
 *
 * `useFrame` reports the real elapsed time, which after a background tab can be
 * many seconds. Simulating that literally drops the camera through the floor;
 * capping it means a stall costs you a moment of movement, not your position.
 */
export function clampDelta(dt: number): number {
  if (!Number.isFinite(dt) || dt <= 0) return 0;
  return Math.min(dt, 0.1);
}
