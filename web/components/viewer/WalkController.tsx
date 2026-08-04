"use client";

/**
 * ArchX3D — Walk mode
 * ===================
 * First-person movement through the building: WASD, mouse look under pointer
 * lock, Shift to run, gravity, and a capsule that will not go through walls.
 *
 * Division of labour
 * ------------------
 * Three pieces, each replaceable without touching the others:
 *
 * * `PointerLockControls` (drei) owns the lock lifecycle and mouse look. It
 *   handles Escape, browser quirks and the re-lock dance correctly, and there
 *   is nothing to gain from writing that again.
 * * `lib/viewer/movement.ts` owns the feel — acceleration, run multiplier,
 *   gravity, sub-stepping. Pure arithmetic, so it is unit-tested.
 * * `CollisionManager` owns the capsule and the BVH.
 *
 * This component is the loop that joins them, and holds no rules of its own.
 *
 * Why the frame loop reads settings imperatively
 * ---------------------------------------------
 * `getSettings()` is called inside `useFrame`, not taken from a hook. Reading
 * through React would re-render this component — and therefore re-register the
 * frame callback — every time the user nudged the speed slider. Movement code
 * runs 144 times a second; it should never be the reason a component renders.
 *
 * Stuck keys
 * ----------
 * Alt-tabbing while holding W means the browser never delivers the `keyup`, and
 * the camera walks into a wall forever. Every path that can lose focus — window
 * blur, pointer-lock exit, visibility change — clears the key set.
 */

import { PointerLockControls } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { PointerLockControls as PointerLockControlsImpl } from "three-stdlib";

import type { Collider } from "./CollisionManager";
import {
  clampDelta,
  dampVelocity,
  desiredVelocity,
  inputFromCodes,
  integrateVertical,
} from "@/lib/viewer/movement";
import { useViewerSettings } from "@/hooks/useViewerSettings";
import { getSettings } from "@/lib/viewer/settings";

/**
 * `pointerSpeed` of 1.0 in three's `PointerLockControls` is 0.002 radians per
 * pixel. Dividing by it converts the viewer's sensitivity — which is stated in
 * radians per pixel, because that is a unit with meaning — into their scale.
 */
const RADIANS_PER_PIXEL_AT_UNIT_SPEED = 0.002;

/** Keys the viewer consumes, so the page does not also scroll or search. */
const CAPTURED_CODES = new Set([
  "KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE",
  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
  "Space", "PageUp", "PageDown",
  "ShiftLeft", "ShiftRight",
]);

export interface WalkControllerProps {
  readonly enabled: boolean;
  readonly collider: Collider | null;
  /** Called when pointer lock is gained or lost, to drive the UI hint. */
  readonly onLockChange?: (locked: boolean) => void;
  /** Reports the resting pose for persistence. */
  readonly onSettled?: (
    position: readonly [number, number, number],
    yaw: number,
    pitch: number,
  ) => void;
  readonly controlsRef?: React.MutableRefObject<PointerLockControlsImpl | null>;
}

export function WalkController({
  enabled,
  collider,
  onLockChange,
  onSettled,
  controlsRef,
}: WalkControllerProps) {
  const camera = useThree((state) => state.camera);
  const internal = useRef<PointerLockControlsImpl | null>(null);
  const controls = controlsRef ?? internal;

  // `pointerSpeed` is a render-time prop, so this one value is read through the
  // hook rather than the store — otherwise changing sensitivity in the settings
  // panel would not take effect until something else re-rendered. Everything
  // the frame loop reads still goes through `getSettings()`.
  const { settings } = useViewerSettings();

  const pressed = useRef<Set<string>>(new Set());
  const velocity = useRef(new THREE.Vector3());
  const grounded = useRef(false);
  const locked = useRef(false);

  // Scratch, reused every frame — see the allocation note in CollisionManager.
  const scratch = useMemo(
    () => ({ euler: new THREE.Euler(0, 0, 0, "YXZ"), eye: new THREE.Vector3() }),
    [],
  );

  const clearKeys = useCallback(() => pressed.current.clear(), []);

  // -- Keyboard ----------------------------------------------------------
  useEffect(() => {
    if (!enabled) {
      clearKeys();
      return;
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat) return;
      if (!CAPTURED_CODES.has(event.code)) return;
      // Space scrolls the page and arrows move the caret; both are wrong here.
      // Only claim them while the pointer is actually locked, so a user tabbing
      // through the toolbar keeps normal keyboard behaviour.
      if (locked.current) event.preventDefault();
      pressed.current.add(event.code);
    };

    const onKeyUp = (event: KeyboardEvent) => {
      pressed.current.delete(event.code);
    };

    const onVisibility = () => {
      if (document.visibilityState === "hidden") clearKeys();
    };

    window.addEventListener("keydown", onKeyDown, { passive: false });
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", clearKeys);
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", clearKeys);
      document.removeEventListener("visibilitychange", onVisibility);
      clearKeys();
    };
  }, [enabled, clearKeys]);

  // Leaving walk mode must not leave momentum behind for the next entry.
  useEffect(() => {
    if (!enabled) {
      velocity.current.set(0, 0, 0);
      grounded.current = false;
    }
  }, [enabled]);

  // -- Persistence -------------------------------------------------------
  const report = useCallback(() => {
    if (!onSettled) return;
    scratch.euler.setFromQuaternion(camera.quaternion, "YXZ");
    onSettled(
      [camera.position.x, camera.position.y, camera.position.z],
      scratch.euler.y,
      scratch.euler.x,
    );
  }, [camera, onSettled, scratch]);

  const handleLock = useCallback(() => {
    locked.current = true;
    onLockChange?.(true);
  }, [onLockChange]);

  const handleUnlock = useCallback(() => {
    locked.current = false;
    clearKeys();
    velocity.current.set(0, 0, 0);
    onLockChange?.(false);
    // Unlocking is the natural "the user has stopped" moment.
    report();
  }, [clearKeys, onLockChange, report]);

  // -- The loop ----------------------------------------------------------
  useFrame((_, rawDelta) => {
    if (!enabled) return;

    const dt = clampDelta(rawDelta);
    if (dt === 0) return;

    const settings = getSettings();
    const input = inputFromCodes(pressed.current);

    // Yaw comes from the camera rather than being tracked separately: the
    // pointer-lock controller owns the look direction, and keeping a second
    // copy of it here is how the two drift apart.
    scratch.euler.setFromQuaternion(camera.quaternion, "YXZ");
    const yaw = scratch.euler.y;

    const gravityEnabled = settings.collisionEnabled && collider !== null;

    const desired = desiredVelocity(
      // Without collision there is nothing to stand on, so vertical keys become
      // a free-fly. With it, height is decided by the floor.
      gravityEnabled ? { ...input, up: false, down: false } : input,
      yaw,
      settings.walkSpeed,
      settings.runMultiplier,
    );

    const damped = dampVelocity(
      [velocity.current.x, gravityEnabled ? 0 : velocity.current.y, velocity.current.z],
      desired,
      dt,
    );

    velocity.current.x = damped[0];
    velocity.current.z = damped[2];
    velocity.current.y = gravityEnabled
      ? integrateVertical(velocity.current.y, dt, {
          grounded: grounded.current,
          jumpRequested: input.jump,
          jumpEnabled: settings.jumpEnabled,
          gravityEnabled: true,
        })
      : damped[1];

    if (gravityEnabled && collider) {
      scratch.eye.copy(camera.position);
      const result = collider.resolve(scratch.eye, velocity.current, dt, {
        height: settings.eyeHeight,
        gravity: true,
      });
      grounded.current = result.grounded;
      camera.position.copy(scratch.eye);
    } else {
      camera.position.addScaledVector(velocity.current, dt);
      grounded.current = false;
    }
  });

  return (
    <PointerLockControls
      ref={controls}
      enabled={enabled}
      makeDefault={enabled}
      pointerSpeed={settings.lookSensitivity / RADIANS_PER_PIXEL_AT_UNIT_SPEED}
      onLock={handleLock}
      onUnlock={handleUnlock}
    />
  );
}
