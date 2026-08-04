"use client";

/**
 * ArchX3D — Viewer lighting
 * =========================
 * Image-based lighting, tone mapping and shadows for the loaded model.
 *
 * The relationship with the generator's lighting
 * ---------------------------------------------
 * The Blender scene already carries a lighting rig recovered from the reference
 * photographs — luminaires with real power and colour temperature, a daylight
 * direction, an ambient level. Those lights are exported in the GLB via
 * `KHR_lights_punctual` and three.js instantiates them, so the model arrives
 * already lit the way the reconstruction intended.
 *
 * What this component adds is what a punctual light cannot give: an environment
 * to reflect. Without an environment map every metal reads as flat grey and
 * every gloss surface as matte, because there is nothing in the world for them
 * to mirror. The HDRI here is therefore a *reflection* source first and a fill
 * light second, which is why its intensity is modest by default — turning it up
 * would wash out the lighting the pipeline worked to recover.
 *
 * Tone mapping
 * ------------
 * ACES Filmic, matching what Blender's Filmic/AgX view transform does to the
 * renders the evaluation engine scores. A viewer using linear tone mapping
 * shows visibly different colour from the preview images, and users reasonably
 * conclude one of them is wrong.
 *
 * Shadows
 * -------
 * One shadow-casting directional light, not several. Shadow maps are the single
 * most expensive thing a WebGL scene can do, and a building has large flat
 * surfaces where a second map buys almost nothing and costs a full extra pass.
 */

import { Environment } from "@react-three/drei";
import { useThree } from "@react-three/fiber";
import { useEffect, useMemo } from "react";
import * as THREE from "three";

import type { Box } from "@/lib/viewer/bounds";
import { boxCenter, boxRadius } from "@/lib/viewer/bounds";
import type { EnvironmentPreset } from "@/types/viewer";

export interface LightingProps {
  readonly bounds: Box | null;
  readonly preset: EnvironmentPreset;
  readonly exposure: number;
  readonly ambientIntensity: number;
  readonly shadows: boolean;
}

export function Lighting({
  bounds,
  preset,
  exposure,
  ambientIntensity,
  shadows,
}: LightingProps) {
  const gl = useThree((state) => state.gl);

  // Tone mapping and exposure live on the renderer, not on a material, so they
  // are set imperatively rather than declared.
  useEffect(() => {
    gl.toneMapping = THREE.ACESFilmicToneMapping;
    gl.toneMappingExposure = exposure;
  }, [gl, exposure]);

  /**
   * Scale the sun to the building.
   *
   * A shadow camera sized for a 6 m flat will not cover a 60 m office, and one
   * sized for the office wastes almost all of its resolution on a flat — the
   * shadows go soft and blocky. Deriving the frustum from the model's radius
   * keeps texel density roughly constant whatever the building.
   */
  const sun = useMemo(() => {
    if (!bounds) {
      return { position: [12, 18, 10] as const, extent: 20, far: 80, bias: -0.0005 };
    }

    const centre = boxCenter(bounds);
    const radius = Math.max(2, boxRadius(bounds));

    return {
      position: [
        centre[0] + radius * 1.1,
        bounds.max[1] + radius * 1.3,
        centre[2] + radius * 0.9,
      ] as const,
      extent: radius * 1.35,
      far: radius * 6,
      // Bias scales with the scene: a value tuned for a small model produces
      // shadow acne on a large one, and peter-panning on a smaller one.
      bias: -Math.max(0.00005, radius * 0.00004),
    };
  }, [bounds]);

  /**
   * The sun's aim point, as a real scene object.
   *
   * `DirectionalLight.target` defaults to an `Object3D` at the world origin,
   * which points the light at the corner of any building that does not happen
   * to sit there. It must be an object *in the scene* — parenting it to the
   * light instead would make its position relative to the light, so the aim
   * would drift with the sun rather than staying on the model.
   */
  const target = useMemo(() => {
    const centre = bounds ? boxCenter(bounds) : ([0, 0, 0] as const);
    const object = new THREE.Object3D();
    object.position.set(centre[0], centre[1], centre[2]);
    return object;
  }, [bounds]);

  return (
    <>
      {/*
        `background={false}` keeps the HDRI as reflections only. Showing it
        would put a photographic sky behind an architectural model, which reads
        as a render of a render.
      */}
      <Environment preset={preset} background={false} environmentIntensity={ambientIntensity} />

      {/* A little omnidirectional fill so interiors are never pitch black when
          the model carries no luminaires — a DXF-only build has none. */}
      <ambientLight intensity={ambientIntensity * 0.4} />

      <primitive object={target} />

      <directionalLight
        position={sun.position}
        target={target}
        intensity={1.15}
        castShadow={shadows}
        shadow-mapSize-width={2048}
        shadow-mapSize-height={2048}
        shadow-camera-near={0.5}
        shadow-camera-far={sun.far}
        shadow-camera-left={-sun.extent}
        shadow-camera-right={sun.extent}
        shadow-camera-top={sun.extent}
        shadow-camera-bottom={-sun.extent}
        shadow-bias={sun.bias}
        // Softens the contact edge without the cost of a larger map.
        shadow-normalBias={0.02}
      />
    </>
  );
}
