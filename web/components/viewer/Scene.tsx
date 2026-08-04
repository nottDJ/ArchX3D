"use client";

/**
 * ArchX3D — Scene contents
 * ========================
 * Everything that lives inside the `<Canvas>`: the model, the lighting, the
 * managers that decide what is visible, and the camera controllers.
 *
 * Why this is separate from `Viewer`
 * ---------------------------------
 * Components inside a `<Canvas>` render through React Three Fiber's reconciler,
 * not the DOM one. Mixing scene components and DOM components in one file makes
 * it easy to write a `<div>` that silently becomes an unknown three.js element,
 * and the failure is a blank canvas with no error. Keeping the boundary at a
 * file boundary makes the rule visible: nothing in this file emits HTML.
 *
 * Ordering
 * --------
 * The managers are mounted *after* the model. React runs effects
 * bottom-up-then-in-order, so the model's own preparation pass — shadows,
 * bounding volumes, vertex colours — has already run by the time visibility and
 * roof effects touch the same objects.
 */

import { Grid } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo } from "react";
import * as THREE from "three";

import { CameraController, type ViewerCommands } from "./CameraController";
import { useCollider } from "./CollisionManager";
import { Lighting } from "./Lighting";
import { Model } from "./Model";
import { RoofManager } from "./RoofManager";
import { VisibilityManager, roofVisibleInMode } from "./VisibilityManager";
import { useModelIndex, type ModelIndex } from "@/hooks/useRoofDetection";
import type { Box } from "@/lib/viewer/bounds";
import { boxCenter, boxRadius } from "@/lib/viewer/bounds";
import type { RoomInfo, ViewerSettings } from "@/types/viewer";

export interface SceneProps {
  readonly scene: THREE.Group | null;
  readonly settings: ViewerSettings;
  readonly rooms: readonly RoomInfo[];
  readonly modelUrl: string;
  readonly commandsRef: React.MutableRefObject<ViewerCommands | null>;
  readonly highlightRoom: string | null;
  readonly onIndexed?: (index: ModelIndex | null) => void;
  readonly onLockChange?: (locked: boolean) => void;
  readonly onRoomChange?: (roomId: string | null) => void;
  readonly onFps?: (fps: number) => void;
  /** Camera position in plan metres and heading in radians, for the minimap. */
  readonly onPose?: (position: readonly [number, number], heading: number) => void;
}

export function Scene({
  scene,
  settings,
  rooms,
  modelUrl,
  commandsRef,
  highlightRoom,
  onIndexed,
  onLockChange,
  onRoomChange,
  onFps,
  onPose,
}: SceneProps) {
  const index = useModelIndex(scene);
  const collider = useCollider(settings.collisionEnabled ? index : null);
  const invalidate = useThree((state) => state.invalidate);

  useEffect(() => {
    onIndexed?.(index);
  }, [index, onIndexed]);

  /**
   * Request a frame whenever something visual changes outside the frame loop.
   *
   * Orbit mode renders on demand, so a view-mode switch, a lighting change or a
   * newly loaded model would otherwise sit invisible until the user happened to
   * move the camera. This is the single place that guarantees a toggle is seen;
   * the two animated managers additionally invalidate while they are running.
   */
  useEffect(() => {
    invalidate();
  }, [invalidate, settings, index, highlightRoom, scene]);

  const bounds: Box | null = index?.bounds ?? null;
  const roofVisible = roofVisibleInMode(settings.viewMode, settings.showRoof);

  return (
    <>
      <Lighting
        bounds={bounds}
        preset={settings.environment}
        exposure={settings.exposure}
        ambientIntensity={settings.ambientIntensity}
        shadows={settings.shadows}
      />

      {settings.showGrid && <GroundGrid bounds={bounds} />}

      {scene && <Model scene={scene} shadows={settings.shadows} />}

      <VisibilityManager
        index={index}
        viewMode={settings.viewMode}
        highlightRoom={highlightRoom}
      />
      <RoofManager index={index} visible={roofVisible} />

      <CameraController
        mode={settings.cameraMode}
        bounds={bounds}
        collider={collider}
        rooms={rooms}
        modelUrl={modelUrl}
        ready={Boolean(scene && index)}
        eyeHeight={settings.eyeHeight}
        commandsRef={commandsRef}
        onLockChange={onLockChange}
        onRoomChange={onRoomChange}
      />

      {onFps && <FpsProbe onFps={onFps} />}
      {onPose && <PoseProbe onPose={onPose} />}
    </>
  );
}

// ---------------------------------------------------------------------------
// Pose sampling
// ---------------------------------------------------------------------------

/**
 * Reports the camera's plan position and heading, throttled to 15 Hz.
 *
 * The minimap is a 160-pixel widget; updating it every frame would mean sixty
 * React renders a second for a marker that moves a fraction of a pixel. Fifteen
 * is indistinguishable to the eye and a quarter of the work.
 *
 * Also gated on actual movement — a stationary camera reports nothing, so an
 * idle viewer does no React work at all.
 */
function PoseProbe({
  onPose,
}: {
  onPose: (position: readonly [number, number], heading: number) => void;
}) {
  const state = useMemo(
    () => ({
      elapsed: 0,
      euler: new THREE.Euler(0, 0, 0, "YXZ"),
      lastX: Number.NaN,
      lastZ: Number.NaN,
      lastYaw: Number.NaN,
    }),
    [],
  );

  useFrame(({ camera }, delta) => {
    state.elapsed += delta;
    if (state.elapsed < 1 / 15) return;
    state.elapsed = 0;

    state.euler.setFromQuaternion(camera.quaternion, "YXZ");
    const { x, z } = camera.position;
    const yaw = state.euler.y;

    if (
      Math.abs(x - state.lastX) < 0.01 &&
      Math.abs(z - state.lastZ) < 0.01 &&
      Math.abs(yaw - state.lastYaw) < 0.01
    ) {
      return;
    }

    state.lastX = x;
    state.lastZ = z;
    state.lastYaw = yaw;

    // Viewer space back to plan metres; the inverse of `planToViewer`.
    onPose([x, -z], yaw);
  });

  return null;
}

// ---------------------------------------------------------------------------
// Ground grid
// ---------------------------------------------------------------------------

/**
 * A metric reference plane under the building.
 *
 * Sized and positioned from the model so it reads as a site rather than a
 * backdrop, and faded at the edges so it does not draw a hard horizon line.
 * One-metre cells with a five-metre accent — the spacing an architect reads
 * without being told what it means.
 */
function GroundGrid({ bounds }: { bounds: Box | null }) {
  const config = useMemo(() => {
    if (!bounds) return { size: 60, position: [0, 0, 0] as const, distance: 80 };
    const centre = boxCenter(bounds);
    const radius = Math.max(4, boxRadius(bounds));
    return {
      size: Math.ceil(radius * 5),
      // A hair below the slab, or the two surfaces z-fight along every edge.
      position: [centre[0], bounds.min[1] - 0.002, centre[2]] as const,
      distance: radius * 8,
    };
  }, [bounds]);

  return (
    <Grid
      position={config.position as unknown as THREE.Vector3Tuple}
      args={[config.size, config.size]}
      cellSize={1}
      cellThickness={0.5}
      cellColor="#2a3038"
      sectionSize={5}
      sectionThickness={1}
      sectionColor="#3d4653"
      fadeDistance={config.distance}
      fadeStrength={1.2}
      followCamera={false}
      infiniteGrid={false}
    />
  );
}

// ---------------------------------------------------------------------------
// FPS
// ---------------------------------------------------------------------------

/**
 * Samples frame rate for the development overlay.
 *
 * Averaged over half a second and reported at 2 Hz. A per-frame readout is
 * unreadable, and — more importantly — a `setState` every frame would make the
 * counter itself the thing costing the frames.
 */
function FpsProbe({ onFps }: { onFps: (fps: number) => void }) {
  const state = useMemo(() => ({ frames: 0, elapsed: 0 }), []);

  useFrame((_, delta) => {
    state.frames += 1;
    state.elapsed += delta;
    if (state.elapsed < 0.5) return;
    onFps(state.frames / state.elapsed);
    state.frames = 0;
    state.elapsed = 0;
  });

  return null;
}

/**
 * Applies renderer settings that have no declarative equivalent.
 *
 * Exported for `Viewer` to mount inside the canvas; kept here so every
 * scene-side concern stays in the scene-side file.
 */
export function RendererSettings({ shadows }: { shadows: boolean }) {
  const gl = useThree((state) => state.gl);

  useEffect(() => {
    gl.shadowMap.enabled = shadows;
    // Soft shadows at no extra sample cost — the right default for
    // architectural surfaces, which are large, flat and unforgiving of the
    // stair-stepping a hard shadow map produces.
    gl.shadowMap.type = THREE.PCFSoftShadowMap;
    gl.shadowMap.needsUpdate = true;
  }, [gl, shadows]);

  return null;
}
