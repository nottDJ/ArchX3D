"use client";

/**
 * ArchX3D — View modes
 * ====================
 * Turns "show me the structure only" into `object.visible = false` on the right
 * meshes, and nothing else.
 *
 * These modes change **visibility only**. Nothing is refetched, re-parsed,
 * rebuilt or re-uploaded to the GPU. Switching from Full Building to Furniture
 * and back is two traversals of a pre-built index and costs well under a
 * millisecond on a large model, which is why the modes can be flipped freely
 * rather than being a decision the user commits to.
 *
 * Why this is a component that renders nothing
 * --------------------------------------------
 * Visibility is a side effect on objects that already exist in the scene, not
 * something React can express as output. Modelling it as a component means the
 * effect runs at the right point in the tree, is cleaned up when the viewer
 * unmounts, and — crucially — restores what it changed, so a mode toggle can
 * never leave a mesh permanently hidden.
 *
 * Wireframe and shared materials
 * ------------------------------
 * The generator shares one material across every wall and every object of a
 * given species, so a mesh-by-mesh wireframe flag would toggle far more than
 * the mesh it was set on. Wireframe is therefore applied per *material*, over a
 * de-duplicated set, and every original value is recorded so exiting the mode
 * restores exactly what was there — including materials that were already
 * wireframe for some other reason.
 */

import { useEffect } from "react";
import * as THREE from "three";

import type { ModelIndex } from "@/hooks/useRoofDetection";
import { isVisibleInMode } from "@/lib/viewer/classify";
import type { ElementKind, ViewMode } from "@/types/viewer";
import { VIEW_MODES } from "@/types/viewer";

export interface VisibilityManagerProps {
  readonly index: ModelIndex | null;
  readonly viewMode: ViewMode;
  /** When set, everything outside this room is dimmed rather than hidden. */
  readonly highlightRoom?: string | null;
}

/**
 * Should the roof be drawn, given the mode and the user's preference?
 *
 * Lives here so the rule sits next to the other visibility rules, but is
 * *applied* by `RoofManager` — which owns the fade and therefore has to own the
 * final `visible` flag too.
 */
export function roofVisibleInMode(viewMode: ViewMode, showRoof: boolean): boolean {
  const meta = VIEW_MODES.find((mode) => mode.id === viewMode) ?? VIEW_MODES[0];
  return isVisibleInMode("roof", meta.shows, meta.hidesRoof || !showRoof);
}

/** Opacity applied to everything outside a highlighted room. */
const DIMMED_OPACITY = 0.12;

export function VisibilityManager({
  index,
  viewMode,
  highlightRoom,
}: VisibilityManagerProps) {
  const meta = VIEW_MODES.find((mode) => mode.id === viewMode) ?? VIEW_MODES[0];

  // -- Visibility --------------------------------------------------------
  useEffect(() => {
    if (!index) return;

    const touched: THREE.Object3D[] = [];

    for (const [kind, objects] of index.byKind) {
      // The roof is `RoofManager`'s alone. Two effects writing `visible` on the
      // same objects race on cleanup order, and the loser wins — which shows up
      // as a roof that reappears when an unrelated setting changes.
      if (kind === "roof") continue;

      const visible = isVisibleInMode(kind as ElementKind, meta.shows, false);
      for (const object of objects) {
        if (object.visible !== visible) {
          object.visible = visible;
          touched.push(object);
        }
      }
    }

    return () => {
      // Restoring on cleanup means an unmount, a model swap or a fast sequence
      // of mode changes can never strand a mesh in the hidden state.
      for (const object of touched) object.visible = true;
    };
  }, [index, meta]);

  // -- Wireframe ---------------------------------------------------------
  useEffect(() => {
    if (!index || !meta.wireframe) return;

    const previous = new Map<THREE.Material, boolean>();

    for (const entry of index.entries) {
      const mesh = entry.object as THREE.Mesh;
      if (!mesh.isMesh) continue;

      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const material of materials) {
        if (!material || previous.has(material)) continue;
        // `wireframe` exists on every material that can render a surface, but
        // not on the base `Material` type, so this is the one place a narrow
        // cast is warranted.
        const target = material as THREE.Material & { wireframe?: boolean };
        if (typeof target.wireframe !== "boolean") continue;

        previous.set(material, target.wireframe);
        target.wireframe = true;
      }
    }

    return () => {
      for (const [material, value] of previous) {
        (material as THREE.Material & { wireframe?: boolean }).wireframe = value;
      }
    };
  }, [index, meta.wireframe]);

  // -- Room highlight ----------------------------------------------------
  useEffect(() => {
    if (!index || !highlightRoom) return;

    const inRoom = new Set(index.byRoom.get(highlightRoom) ?? []);
    if (inRoom.size === 0) return;

    const previous = new Map<
      THREE.Material,
      { opacity: number; transparent: boolean; depthWrite: boolean }
    >();

    for (const entry of index.entries) {
      const mesh = entry.object as THREE.Mesh;
      if (!mesh.isMesh || inRoom.has(mesh)) continue;
      // The shell stays solid: dimming the walls of the room you are looking
      // *into* removes the very context the highlight is meant to give.
      if (entry.classification.kind === "wall" || entry.classification.kind === "floor") {
        continue;
      }

      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const material of materials) {
        if (!material || previous.has(material)) continue;
        previous.set(material, {
          opacity: material.opacity,
          transparent: material.transparent,
          depthWrite: material.depthWrite,
        });
        material.transparent = true;
        material.opacity = DIMMED_OPACITY;
        // Without this, dimmed geometry still occludes what is behind it and
        // the highlighted room stays hidden behind a ghost.
        material.depthWrite = false;
      }
    }

    return () => {
      for (const [material, state] of previous) {
        material.opacity = state.opacity;
        material.transparent = state.transparent;
        material.depthWrite = state.depthWrite;
      }
    };
  }, [index, highlightRoom]);

  return null;
}
