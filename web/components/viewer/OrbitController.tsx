"use client";

/**
 * ArchX3D — Orbit mode
 * ====================
 * Rotate, pan and zoom around the building. The mode a user lands in, because
 * seeing the whole thing is the first question and standing inside it is the
 * second.
 *
 * Thin by design: drei's `OrbitControls` already does the interaction well, so
 * this adds only the parts that are specific to inspecting a building —
 * distance limits derived from the model's size, an inverted-pitch guard so you
 * cannot roll under the floor and lose your bearings, and reporting the pose so
 * it can be restored next session.
 */

import { OrbitControls } from "@react-three/drei";
import { useEffect, useRef } from "react";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";

import type { Box } from "@/lib/viewer/bounds";
import { boxRadius } from "@/lib/viewer/bounds";

export interface OrbitControllerProps {
  readonly enabled: boolean;
  /** Model bounds, used to scale the zoom limits to the building. */
  readonly bounds: Box | null;
  /** Fires when the user stops moving the camera, for persistence. */
  readonly onSettled?: (
    position: readonly [number, number, number],
    target: readonly [number, number, number],
  ) => void;
  readonly controlsRef?: React.MutableRefObject<OrbitControlsImpl | null>;
}

/**
 * Stop just short of the poles.
 *
 * At exactly vertical the azimuth becomes undefined and the model appears to
 * spin on its own as the controller resolves the singularity. Two degrees of
 * clearance is invisible and removes it.
 */
const POLAR_EPSILON = 0.035;

export function OrbitController({
  enabled,
  bounds,
  onSettled,
  controlsRef,
}: OrbitControllerProps) {
  const internal = useRef<OrbitControlsImpl | null>(null);
  const controls = controlsRef ?? internal;

  const radius = bounds ? boxRadius(bounds) : 10;

  // Report the resting pose rather than every frame of a drag: persistence
  // only cares where the user stopped, and writing to localStorage during an
  // orbit would be hundreds of writes a second.
  useEffect(() => {
    const instance = controls.current;
    if (!instance || !onSettled) return;

    const handle = () => {
      const { object, target } = instance;
      onSettled(
        [object.position.x, object.position.y, object.position.z],
        [target.x, target.y, target.z],
      );
    };

    instance.addEventListener("end", handle);
    return () => instance.removeEventListener("end", handle);
  }, [controls, onSettled]);

  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enabled={enabled}
      // Damping is what makes an architectural model feel heavy rather than
      // twitchy; it also smooths out low-resolution trackpad deltas.
      enableDamping
      dampingFactor={0.08}
      rotateSpeed={0.7}
      zoomSpeed={0.9}
      panSpeed={0.8}
      // Screen-space panning drags the model with the cursor, which is what
      // every CAD tool does. The alternative pans along the ground plane and
      // feels like the model is sliding away from you.
      screenSpacePanning
      minDistance={Math.max(0.35, radius * 0.03)}
      maxDistance={radius * 12}
      minPolarAngle={POLAR_EPSILON}
      maxPolarAngle={Math.PI - POLAR_EPSILON}
    />
  );
}
