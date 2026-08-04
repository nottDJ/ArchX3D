"use client";

/**
 * ArchX3D — Roof visibility
 * =========================
 * The single most-used control in the viewer, and the reason it is a first
 *-class feature rather than a line in the visibility manager.
 *
 * The problem it solves
 * ---------------------
 * Every generated building has a ceiling, because a room without one renders
 * with light pouring in from above and looks nothing like the photograph it was
 * built from. That same ceiling makes the interior invisible from anywhere
 * outside, so the default view of a finished model is a box. Users reach for
 * "hide the roof" within seconds of the model appearing.
 *
 * Detection
 * ---------
 * Three sources, in order of trust, all resolved in `lib/viewer/classify.ts`:
 *
 * 1. **Metadata** — `extras.archx3d_kind === "roof"`, written by the generator.
 *    Exact, and what any model built by a current ArchX3D produces.
 * 2. **Name** — the mesh is called `Ceiling` or `Roof`. Matched on the whole
 *    name so `ceiling_fan_1` is not swept up with it.
 * 3. **Geometry** — a thin plate, in the top quarter of the building, covering
 *    most of the plan. The fallback for models built before the metadata pass.
 *
 * The geometric rung is deliberately reluctant: it would rather miss a roof
 * than hide a mezzanine floor. A missed roof is visible and the user can switch
 * to Interior view; a wrongly hidden floor is a piece of the building that has
 * silently vanished.
 *
 * Fade, not blink
 * ---------------
 * The roof fades over ~180 ms rather than disappearing. A hard cut through a
 * large surface reads as a glitch — the eye cannot tell whether geometry was
 * removed or the camera moved — while a short fade reads unmistakably as *that
 * thing was taken away*.
 */

import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import * as THREE from "three";

import type { ModelIndex } from "@/hooks/useRoofDetection";

export interface RoofManagerProps {
  readonly index: ModelIndex | null;
  /** False hides the roof. Driven by the toggle and by the active view mode. */
  readonly visible: boolean;
  /** Skip the fade — used when the model first appears. */
  readonly immediate?: boolean;
}

/** Seconds for a full fade. Short enough not to feel sluggish. */
const FADE_SECONDS = 0.18;

interface RoofMaterialState {
  readonly material: THREE.Material;
  readonly opacity: number;
  readonly transparent: boolean;
  readonly depthWrite: boolean;
}

export function RoofManager({ index, visible, immediate = false }: RoofManagerProps) {
  const invalidate = useThree((state) => state.invalidate);
  const progress = useRef(visible ? 1 : 0);
  const target = useRef(visible ? 1 : 0);
  const states = useRef<RoofMaterialState[]>([]);

  // Capture the roof materials' original state once per model, so the fade has
  // something to return to and a restore can never invent values.
  useEffect(() => {
    if (!index) {
      states.current = [];
      return;
    }

    const seen = new Set<THREE.Material>();
    const captured: RoofMaterialState[] = [];

    for (const object of index.roofs) {
      const mesh = object as THREE.Mesh;
      if (!mesh.isMesh) continue;

      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const material of materials) {
        if (!material || seen.has(material)) continue;
        seen.add(material);
        captured.push({
          material,
          opacity: material.opacity,
          transparent: material.transparent,
          depthWrite: material.depthWrite,
        });
      }
    }

    states.current = captured;

    return () => {
      for (const state of captured) {
        state.material.opacity = state.opacity;
        state.material.transparent = state.transparent;
        state.material.depthWrite = state.depthWrite;
      }
      states.current = [];
    };
  }, [index]);

  useEffect(() => {
    target.current = visible ? 1 : 0;
    if (immediate) progress.current = target.current;
    // Orbit mode renders on demand, so a toggle has to ask for the frame that
    // starts the fade — otherwise nothing happens until the user moves the
    // camera, and the button looks broken.
    invalidate();
  }, [visible, immediate, invalidate]);

  useFrame((_, delta) => {
    if (!index || states.current.length === 0) return;

    const goal = target.current;
    if (progress.current === goal) return;

    const step = delta / FADE_SECONDS;
    progress.current =
      goal > progress.current
        ? Math.min(goal, progress.current + step)
        : Math.max(goal, progress.current - step);

    const t = progress.current;
    const hidden = t <= 0.001;

    for (const state of states.current) {
      state.material.opacity = state.opacity * t;
      // Only force transparency while actually fading; leaving it on costs a
      // sorting pass and can introduce z-fighting on a large flat ceiling.
      state.material.transparent = t < 0.999 ? true : state.transparent;
      state.material.depthWrite = t < 0.999 ? false : state.depthWrite;
    }

    // Once invisible, stop drawing it at all — a fully transparent surface is
    // still rasterised, and a ceiling covers the whole viewport.
    for (const object of index.roofs) object.visible = !hidden;

    // Keep the fade running under on-demand rendering.
    if (progress.current !== goal) invalidate();
  });

  // Apply the end state immediately when asked, without waiting for a frame.
  useEffect(() => {
    if (!immediate || !index) return;
    const hidden = !visible;
    for (const object of index.roofs) object.visible = !hidden;
    for (const state of states.current) {
      state.material.opacity = visible ? state.opacity : 0;
      state.material.transparent = visible ? state.transparent : true;
      state.material.depthWrite = visible ? state.depthWrite : false;
    }
  }, [immediate, index, visible]);

  return null;
}
