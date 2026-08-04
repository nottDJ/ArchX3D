"use client";

/**
 * ArchX3D — The model in the scene
 * ================================
 * Puts the loaded GLB into the scene and prepares it for interactive viewing.
 *
 * The model is added at identity
 * ------------------------------
 * No centring, no scaling, no rotation. It is tempting to translate a model to
 * the origin — most viewers do — but the room manifest, the minimap and room
 * navigation are all expressed in the building's own plan coordinates, and any
 * transform here would have to be applied to every one of them, correctly,
 * forever. Framing is the camera's job instead, which is where it belongs and
 * costs nothing.
 *
 * Preparation
 * -----------
 * Four passes over the scene graph, all one-time:
 *
 * * **Shadows** — glTF does not carry cast/receive flags, so every mesh gets
 *   both. Doing this per mesh rather than on the root is necessary because
 *   three.js reads the flags on the object being rendered, not on an ancestor.
 * * **Frustum culling** — on for everything, so a mesh outside the view costs
 *   nothing. It is on by default, but a loader or an extension can clear it.
 * * **Bounding volumes** — computed once so culling and raycasting do not have
 *   to derive them lazily during the first frames, which is a visible hitch on
 *   a large model.
 * * **Vertex colours** — Blender exports them on procedural furniture, and a
 *   material with `vertexColors` unset ignores them and renders flat white.
 */

import { useEffect } from "react";
import * as THREE from "three";

export interface ModelProps {
  readonly scene: THREE.Group;
  readonly shadows: boolean;
}

export function Model({ scene, shadows }: ModelProps) {
  useEffect(() => {
    scene.traverse((object) => {
      const mesh = object as THREE.Mesh;
      if (!mesh.isMesh) return;

      mesh.castShadow = shadows;
      mesh.receiveShadow = shadows;
      mesh.frustumCulled = true;

      if (mesh.geometry) {
        if (!mesh.geometry.boundingBox) mesh.geometry.computeBoundingBox();
        if (!mesh.geometry.boundingSphere) mesh.geometry.computeBoundingSphere();
      }

      const hasVertexColours = Boolean(mesh.geometry?.getAttribute("color"));
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const material of materials) {
        if (!material) continue;
        const standard = material as THREE.MeshStandardMaterial;
        if (hasVertexColours && !standard.vertexColors) {
          standard.vertexColors = true;
          standard.needsUpdate = true;
        }
      }
    });
  }, [scene, shadows]);

  return <primitive object={scene} />;
}
