"use client";

/**
 * ArchX3D — Camera orchestration
 * ==============================
 * Owns everything about *where the camera is*: which mode is active, how it
 * gets framed on the building, how it flies to a room, and what it remembers
 * between sessions.
 *
 * The two controllers stay ignorant of all of this. `OrbitController` orbits,
 * `WalkController` walks; neither knows the other exists, neither reads
 * persistence, and neither decides when to hand over. That separation is what
 * makes it possible to add a third mode — a top-down plan view, a fixed
 * elevation — without touching either.
 *
 * Mode transitions
 * ----------------
 * Switching modes is not just enabling a different controller. Orbit wants a
 * position and a target; walk wants a position, a yaw and a pitch, at eye
 * height, standing on a floor. Each transition saves the pose it is leaving and
 * restores — or derives — the pose it is entering, so going walk → orbit → walk
 * returns you to where you were standing rather than to the front door.
 *
 * Why poses are stored per model
 * ------------------------------
 * Resuming a camera position from a different building drops you inside a wall
 * or a hundred metres above a bungalow. `cameraStorageKey` keys on the model
 * URL so every project remembers its own vantage point.
 */

import { useFrame, useThree } from "@react-three/fiber";
import { useCallback, useEffect, useImperativeHandle, useMemo, useRef } from "react";
import * as THREE from "three";
import type {
  OrbitControls as OrbitControlsImpl,
  PointerLockControls as PointerLockControlsImpl,
} from "three-stdlib";

import type { Collider } from "./CollisionManager";
import { OrbitController } from "./OrbitController";
import { WalkController } from "./WalkController";
import type { Box } from "@/lib/viewer/bounds";
import {
  fitCameraToBox,
  interiorSpawn,
  lookAngles,
  planToViewer,
  roomViewpoint,
} from "@/lib/viewer/bounds";
import { loadCamera, saveCamera } from "@/lib/viewer/settings";
import type { CameraMode, CameraPose, RoomInfo, SavedCamera } from "@/types/viewer";

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/**
 * The imperative surface the toolbar drives.
 *
 * A ref rather than props because these are *events*, not state: "fit the
 * model" is a thing that happens once, and modelling it as state means
 * inventing a token to change so an effect notices. The ref is created outside
 * the canvas and populated from inside, which is how a DOM-side control reaches
 * scene-side behaviour.
 */
export interface ViewerCommands {
  /** Frame the whole building. */
  fitToModel(): void;
  /** Fit, and return to orbit mode. */
  resetCamera(): void;
  /** Smoothly move to a room's viewpoint. */
  flyToRoom(room: RoomInfo): void;
  /** Ask the browser for pointer lock. Needs a user gesture. */
  requestPointerLock(): void;
  /** PNG data URL of the current frame, or `null` if it could not be read. */
  screenshot(): string | null;
}

export interface CameraControllerProps {
  readonly mode: CameraMode;
  readonly bounds: Box | null;
  readonly collider: Collider | null;
  readonly rooms: readonly RoomInfo[];
  readonly modelUrl: string;
  /** True once the model is in the scene — framing before that fits nothing. */
  readonly ready: boolean;
  readonly eyeHeight: number;
  readonly commandsRef: React.MutableRefObject<ViewerCommands | null>;
  readonly onLockChange?: (locked: boolean) => void;
  /** Reports the room the camera is currently standing in, for the minimap. */
  readonly onRoomChange?: (roomId: string | null) => void;
}

/** Seconds a room fly-through takes. Long enough to read as travel. */
const FLIGHT_DURATION = 1.15;

interface Flight {
  elapsed: number;
  readonly fromPosition: THREE.Vector3;
  readonly toPosition: THREE.Vector3;
  readonly fromTarget: THREE.Vector3;
  readonly toTarget: THREE.Vector3;
}

/** Ease in and out — constant velocity reads as a machine, not a camera. */
function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

// ---------------------------------------------------------------------------

export function CameraController({
  mode,
  bounds,
  collider,
  rooms,
  modelUrl,
  ready,
  eyeHeight,
  commandsRef,
  onLockChange,
  onRoomChange,
}: CameraControllerProps) {
  const camera = useThree((state) => state.camera);
  const gl = useThree((state) => state.gl);
  const scene = useThree((state) => state.scene);
  const size = useThree((state) => state.size);
  const invalidate = useThree((state) => state.invalidate);

  const orbitRef = useRef<OrbitControlsImpl | null>(null);
  const walkRef = useRef<PointerLockControlsImpl | null>(null);

  const flight = useRef<Flight | null>(null);
  const saved = useRef<SavedCamera>({});
  const framed = useRef(false);
  const previousMode = useRef<CameraMode | null>(null);
  const currentRoom = useRef<string | null>(null);

  const scratch = useMemo(
    () => ({
      target: new THREE.Vector3(),
      euler: new THREE.Euler(0, 0, 0, "YXZ"),
      quaternion: new THREE.Quaternion(),
      matrix: new THREE.Matrix4(),
      up: new THREE.Vector3(0, 1, 0),
    }),
    [],
  );

  /** The largest room's centre — a better walk spawn than the plan centroid. */
  const spawnHint = useMemo(() => {
    const largest = rooms[0];
    if (!largest) return undefined;
    return [
      (largest.bounds_min[0] + largest.bounds_max[0]) / 2,
      (largest.bounds_min[1] + largest.bounds_max[1]) / 2,
    ] as const;
  }, [rooms]);

  // -- Persistence -------------------------------------------------------

  useEffect(() => {
    saved.current = loadCamera(modelUrl);
    framed.current = false;
    previousMode.current = null;
  }, [modelUrl]);

  const persist = useCallback(
    (patch: Partial<SavedCamera>) => {
      saved.current = { ...saved.current, ...patch };
      saveCamera(modelUrl, saved.current);
    },
    [modelUrl],
  );

  const captureOrbit = useCallback((): CameraPose => {
    const target = orbitRef.current?.target ?? scratch.target.set(0, 0, 0);
    return {
      position: [camera.position.x, camera.position.y, camera.position.z],
      target: [target.x, target.y, target.z],
    };
  }, [camera, scratch]);

  const captureWalk = useCallback((): CameraPose => {
    scratch.euler.setFromQuaternion(camera.quaternion, "YXZ");
    return {
      position: [camera.position.x, camera.position.y, camera.position.z],
      yaw: scratch.euler.y,
      pitch: scratch.euler.x,
    };
  }, [camera, scratch]);

  // -- Framing -----------------------------------------------------------

  const applyOrbitPose = useCallback(
    (position: readonly [number, number, number], target: readonly [number, number, number]) => {
      camera.position.set(position[0], position[1], position[2]);
      const controls = orbitRef.current;
      if (controls) {
        controls.target.set(target[0], target[1], target[2]);
        controls.update();
      } else {
        camera.lookAt(target[0], target[1], target[2]);
      }
    },
    [camera],
  );

  const fitToModel = useCallback(() => {
    if (!bounds) return;
    flight.current = null;

    const fit = fitCameraToBox(bounds, {
      fov: camera instanceof THREE.PerspectiveCamera ? camera.fov : 50,
      aspect: size.width / Math.max(1, size.height),
    });

    if (camera instanceof THREE.PerspectiveCamera) {
      camera.near = fit.near;
      camera.far = fit.far;
      camera.updateProjectionMatrix();
    }

    applyOrbitPose(fit.position, fit.target);
    persist({ orbit: { position: fit.position, target: fit.target } });
  }, [applyOrbitPose, bounds, camera, persist, size]);

  /**
   * Put the camera somewhere sensible to stand.
   *
   * Spawning at a stored eye height is not enough: the floor may be at a
   * different level in this model, or the stored position may predate a
   * regeneration. Dropping onto whatever solid surface is below means walk mode
   * always begins standing on something.
   */
  const groundedSpawn = useCallback(
    (position: readonly [number, number, number]): readonly [number, number, number] => {
      if (!collider) return position;
      const floor = collider.groundBelow(position[0], position[2], position[1] + 3);
      if (floor === null) return position;
      return [position[0], floor + eyeHeight, position[2]];
    },
    [collider, eyeHeight],
  );

  const enterWalk = useCallback(() => {
    const stored = saved.current.walk;
    const base =
      stored?.position ??
      (bounds
        ? interiorSpawn(bounds, { eyeHeight, preferred: spawnHint })
        : ([0, eyeHeight, 0] as const));

    const position = groundedSpawn(base);
    camera.position.set(position[0], position[1], position[2]);

    if (stored?.yaw !== undefined) {
      scratch.euler.set(stored.pitch ?? 0, stored.yaw, 0, "YXZ");
      camera.quaternion.setFromEuler(scratch.euler);
    } else if (bounds) {
      // Face the middle of the building, so the first thing you see is the
      // interior rather than whichever wall you happen to be standing against.
      const centre: readonly [number, number, number] = [
        (bounds.min[0] + bounds.max[0]) / 2,
        position[1],
        (bounds.min[2] + bounds.max[2]) / 2,
      ];
      const { yaw, pitch } = lookAngles(position, centre);
      scratch.euler.set(pitch, yaw, 0, "YXZ");
      camera.quaternion.setFromEuler(scratch.euler);
    }
  }, [bounds, camera, eyeHeight, groundedSpawn, scratch, spawnHint]);

  const enterOrbit = useCallback(() => {
    const stored = saved.current.orbit;
    if (stored?.target) {
      applyOrbitPose(stored.position, stored.target);
      return;
    }
    fitToModel();
  }, [applyOrbitPose, fitToModel]);

  // -- Initial framing ---------------------------------------------------

  useEffect(() => {
    if (!ready || !bounds || framed.current) return;
    framed.current = true;

    if (mode === "orbit") enterOrbit();
    else enterWalk();
  }, [ready, bounds, mode, enterOrbit, enterWalk]);

  // -- Mode transitions --------------------------------------------------

  useEffect(() => {
    if (!ready || !framed.current) {
      previousMode.current = mode;
      return;
    }
    if (previousMode.current === mode) return;

    // Save where we were before moving.
    if (previousMode.current === "orbit") persist({ orbit: captureOrbit() });
    if (previousMode.current === "walk") persist({ walk: captureWalk() });

    flight.current = null;
    if (mode === "walk") enterWalk();
    else enterOrbit();

    persist({ mode });
    previousMode.current = mode;
  }, [mode, ready, captureOrbit, captureWalk, enterOrbit, enterWalk, persist]);

  // -- Flight ------------------------------------------------------------

  const flyTo = useCallback(
    (position: readonly [number, number, number], target: readonly [number, number, number]) => {
      const fromTarget = new THREE.Vector3();
      if (mode === "orbit" && orbitRef.current) {
        fromTarget.copy(orbitRef.current.target);
      } else {
        // Walk mode has no target, so synthesise one a short way ahead — the
        // point the camera is currently looking at.
        camera.getWorldDirection(fromTarget);
        fromTarget.multiplyScalar(3).add(camera.position);
      }

      flight.current = {
        elapsed: 0,
        fromPosition: camera.position.clone(),
        toPosition: new THREE.Vector3(position[0], position[1], position[2]),
        fromTarget,
        toTarget: new THREE.Vector3(target[0], target[1], target[2]),
      };

      // Orbit renders on demand; the flight needs the first frame requesting.
      invalidate();
    },
    [camera, invalidate, mode],
  );

  const flyToRoom = useCallback(
    (room: RoomInfo) => {
      const floorY = bounds?.min[1] ?? 0;
      const view = roomViewpoint(room.bounds_min, room.bounds_max, eyeHeight, floorY);

      if (mode === "walk") {
        flyTo(groundedSpawn(view.position), view.target);
        return;
      }

      // In orbit mode, look at the room from above and outside rather than
      // standing in it — otherwise "go to the kitchen" buries the camera in the
      // worktop and the user has to zoom back out to understand what happened.
      const centre = planToViewer(
        (room.bounds_min[0] + room.bounds_max[0]) / 2,
        (room.bounds_min[1] + room.bounds_max[1]) / 2,
        floorY + room.ceiling_height * 0.5,
      );
      const span = Math.max(
        Math.abs(room.bounds_max[0] - room.bounds_min[0]),
        Math.abs(room.bounds_max[1] - room.bounds_min[1]),
      );
      const distance = Math.max(3, span * 1.4);

      flyTo(
        [centre[0] + distance * 0.7, centre[1] + distance * 0.8, centre[2] + distance * 0.7],
        centre,
      );
    },
    [bounds, eyeHeight, flyTo, groundedSpawn, mode],
  );

  useFrame((_, delta) => {
    const active = flight.current;
    if (!active) return;

    active.elapsed += delta;
    const t = Math.min(1, active.elapsed / FLIGHT_DURATION);
    const eased = easeInOutCubic(t);

    camera.position.lerpVectors(active.fromPosition, active.toPosition, eased);
    scratch.target.lerpVectors(active.fromTarget, active.toTarget, eased);

    if (mode === "orbit" && orbitRef.current) {
      orbitRef.current.target.copy(scratch.target);
      orbitRef.current.update();
    } else {
      // Slerp rather than `lookAt` each frame: `lookAt` on a fast-moving camera
      // produces a visible swing as the up vector re-solves.
      scratch.matrix.lookAt(camera.position, scratch.target, scratch.up);
      scratch.quaternion.setFromRotationMatrix(scratch.matrix);
      camera.quaternion.slerp(scratch.quaternion, Math.min(1, delta * 8));
    }

    if (t >= 1) {
      flight.current = null;
      persist(mode === "orbit" ? { orbit: captureOrbit() } : { walk: captureWalk() });
    } else {
      // Keep the flight running under on-demand rendering.
      invalidate();
    }
  });

  // -- Which room are we in? ---------------------------------------------

  useFrame(() => {
    if (!onRoomChange || rooms.length === 0) return;

    // Plan space is the GLB's own frame with Z negated; see `planToViewer`.
    const x = camera.position.x;
    const y = -camera.position.z;

    let found: string | null = null;
    for (const room of rooms) {
      if (
        x >= room.bounds_min[0] && x <= room.bounds_max[0] &&
        y >= room.bounds_min[1] && y <= room.bounds_max[1]
      ) {
        found = room.id;
        break;
      }
    }

    if (found !== currentRoom.current) {
      currentRoom.current = found;
      onRoomChange(found);
    }
  });

  // -- Commands ----------------------------------------------------------

  useImperativeHandle(
    commandsRef,
    () => ({
      fitToModel,
      resetCamera: () => {
        flight.current = null;
        // Clear the stored walk pose too: "reset" that leaves you inside a
        // wall when you next switch to walk has not reset anything.
        saved.current = {};
        saveCamera(modelUrl, {});
        fitToModel();
      },
      flyToRoom,
      requestPointerLock: () => {
        try {
          walkRef.current?.lock();
        } catch {
          // Pointer lock needs a recent user gesture; the click-to-look overlay
          // is the fallback and is always available.
        }
      },
      screenshot: () => {
        try {
          // With `preserveDrawingBuffer` off — the default, and much cheaper —
          // the back buffer is undefined after presentation. Rendering
          // immediately before reading guarantees there is a frame to capture.
          gl.render(scene, camera);
          return gl.domElement.toDataURL("image/png");
        } catch {
          return null;
        }
      },
    }),
    [camera, fitToModel, flyToRoom, gl, modelUrl, scene],
  );

  // -- Controllers -------------------------------------------------------

  const handleOrbitSettled = useCallback(
    (position: readonly [number, number, number], target: readonly [number, number, number]) => {
      persist({ orbit: { position, target } });
    },
    [persist],
  );

  const handleWalkSettled = useCallback(
    (position: readonly [number, number, number], yaw: number, pitch: number) => {
      persist({ walk: { position, yaw, pitch } });
    },
    [persist],
  );

  return mode === "orbit" ? (
    <OrbitController
      enabled={flight.current === null}
      bounds={bounds}
      controlsRef={orbitRef}
      onSettled={handleOrbitSettled}
    />
  ) : (
    <WalkController
      enabled
      collider={collider}
      controlsRef={walkRef}
      onLockChange={onLockChange}
      onSettled={handleWalkSettled}
    />
  );
}
