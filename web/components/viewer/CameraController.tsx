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
import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
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
  floorProbeHeight,
  lookAngles,
  planToViewer,
  roomViewpoint,
} from "@/lib/viewer/bounds";
import { loadCamera, saveCamera } from "@/lib/viewer/settings";
import {
  afterFallingOut,
  chooseWalkSpawn,
  hasFallenOut,
  openestYaw,
  spawnCandidates,
  supportAt,
} from "@/lib/viewer/spawn";
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

  /**
   * Set when walk mode found nowhere with a floor under it. Gravity is then
   * withheld - the walk controller gets no collider - so the user flies rather
   * than falls. Falling from an unsupported spawn is exactly the G5 black
   * screen; see lib/viewer/spawn.ts.
   */
  const [unsupported, setUnsupported] = useState(false);
  /** Fall-outs this model has had; see `afterFallingOut`. */
  const fallOuts = useRef(0);
  /**
   * The pose a fresh walk spawn was given, until the user moves or looks.
   *
   * A spawn chosen before the collider exists can only face the building's
   * centre. When the collider arrives the facing is chosen again from real
   * sightlines — but only if this pose is still exactly where the camera is,
   * so nobody who has started looking around gets turned.
   */
  const freshSpawn = useRef<{ position: THREE.Vector3; quaternion: THREE.Quaternion } | null>(null);

  // -- Persistence -------------------------------------------------------

  useEffect(() => {
    saved.current = loadCamera(modelUrl);
    framed.current = false;
    previousMode.current = null;
    fallOuts.current = 0;
    setUnsupported(false);
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
      // Probe from just above the feet. See `floorProbeHeight`: probing from
      // above the head starts the ray above the ceiling, which lands the
      // camera on the roof instead of on the floor.
      const probe = floorProbeHeight(position[1], eyeHeight);
      const floor = collider.groundBelow(position[0], position[2], probe);
      if (floor === null) return position;
      return [position[0], floor + eyeHeight, position[2]];
    },
    [collider, eyeHeight],
  );

  /**
   * Whether a walk pose is somewhere a person could be standing.
   *
   * A pose captured mid-fall used to be persisted, so the next session spawned
   * straight back into the void. Without a collider there is no way to tell,
   * so only the fall-out test applies.
   */
  const isStandable = useCallback(
    (position: readonly [number, number, number]) => {
      if (hasFallenOut(position[1], bounds)) return false;
      return collider ? supportAt(collider, position, eyeHeight) !== null : true;
    },
    [bounds, collider, eyeHeight],
  );

  /**
   * Level the camera and turn it toward the most open view from `position`.
   *
   * Facing the middle of the building is the fallback, and all there is before
   * the collider exists; with it, `openestYaw` avoids the wall a corner-room
   * spawn would otherwise stare at.
   */
  const faceOpenest = useCallback(
    (position: readonly [number, number, number]) => {
      if (!bounds) return;
      const centre: readonly [number, number, number] = [
        (bounds.min[0] + bounds.max[0]) / 2,
        position[1],
        (bounds.min[2] + bounds.max[2]) / 2,
      ];
      const towardCentre = lookAngles(position, centre).yaw;
      const yaw = collider ? openestYaw(collider, position, towardCentre, bounds) : towardCentre;
      scratch.euler.set(0, yaw, 0, "YXZ");
      camera.quaternion.setFromEuler(scratch.euler);
    },
    [bounds, camera, collider, scratch],
  );

  const enterWalk = useCallback(() => {
    const remembered = saved.current.walk;
    const stored =
      remembered?.position && isStandable(remembered.position) ? remembered : undefined;

    let position: readonly [number, number, number];
    if (stored?.position) {
      position = groundedSpawn(stored.position);
      setUnsupported(afterFallingOut(fallOuts.current) === "fly");
    } else if (bounds) {
      // Storey-aware, inside a room, and on a floor when the collider can say
      // so. Before the collider exists this is unconfirmed; the effect below
      // re-checks the moment it arrives.
      const spawn = chooseWalkSpawn(spawnCandidates(rooms, bounds, eyeHeight), collider, eyeHeight);
      position = spawn.position;
      setUnsupported(
        afterFallingOut(fallOuts.current) === "fly" || (collider !== null && !spawn.supported),
      );
      if (collider && !spawn.supported) {
        console.warn("[viewer] walk mode found no floor to stand on; flying instead of falling");
      }
    } else {
      position = [0, eyeHeight, 0];
    }

    camera.position.set(position[0], position[1], position[2]);

    freshSpawn.current = null;
    if (stored?.yaw !== undefined) {
      scratch.euler.set(stored.pitch ?? 0, stored.yaw, 0, "YXZ");
      camera.quaternion.setFromEuler(scratch.euler);
    } else if (bounds) {
      faceOpenest(position);
      freshSpawn.current = {
        position: camera.position.clone(),
        quaternion: camera.quaternion.clone(),
      };
    }
  }, [bounds, camera, collider, eyeHeight, faceOpenest, groundedSpawn, isStandable, rooms, scratch]);

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

    // Save where we were before moving - but never a walk pose with no floor
    // under it, or the next entry resumes the fall.
    if (previousMode.current === "orbit") persist({ orbit: captureOrbit() });
    if (previousMode.current === "walk") {
      const pose = captureWalk();
      persist({ walk: isStandable(pose.position) ? pose : undefined });
    }

    flight.current = null;
    if (mode === "walk") enterWalk();
    else enterOrbit();

    persist({ mode });
    previousMode.current = mode;
  }, [mode, ready, captureOrbit, captureWalk, enterOrbit, enterWalk, isStandable, persist]);

  // -- The collider arrives late -----------------------------------------

  /**
   * Re-check the walk spawn once there is something to check it against.
   *
   * The collider is built in an effect after the model is indexed, so the
   * initial walk entry almost always runs without one and cannot know whether
   * it placed the camera over a floor. The moment it exists, ask; if the answer
   * is no, choose again with the collider in hand.
   */
  useEffect(() => {
    if (!collider || mode !== "walk" || !framed.current || flight.current) return;
    const p = camera.position;
    if (supportAt(collider, [p.x, p.y, p.z], eyeHeight) === null) {
      saved.current = { ...saved.current, walk: undefined };
      enterWalk();
      return;
    }
    // Standing is fine; the facing was chosen blind. Choose again if untouched.
    const spawn = freshSpawn.current;
    if (
      spawn &&
      spawn.position.distanceToSquared(p) < 1e-8 &&
      spawn.quaternion.angleTo(camera.quaternion) < 1e-6
    ) {
      faceOpenest([p.x, p.y, p.z]);
      spawn.quaternion.copy(camera.quaternion);
    }
    // Only the collider's arrival should trigger this; re-running whenever
    // enterWalk changes identity would teleport a user who is walking.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [collider]);

  /** Called by the walk controller when the camera has left the model. */
  const handleFellOut = useCallback(() => {
    flight.current = null;
    saved.current = { ...saved.current, walk: undefined };
    saveCamera(modelUrl, saved.current);
    fallOuts.current += 1;
    enterWalk();
    if (afterFallingOut(fallOuts.current) === "fly") {
      // enterWalk may have just cleared this; the repeat fall overrides it.
      setUnsupported(true);
      console.warn("[viewer] the walk camera fell out of this model twice; flying instead of falling");
    }
  }, [enterWalk, modelUrl]);

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
      // The room's own storey, not the model's lowest point: flying to a
      // first-floor bedroom at ground level lands the camera under its floor.
      const floorY = (bounds?.min[1] ?? 0) + room.elevation;
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
      if (!isStandable(position)) return;
      persist({ walk: { position, yaw, pitch } });
    },
    [isStandable, persist],
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
      collider={unsupported ? null : collider}
      bounds={bounds}
      controlsRef={walkRef}
      onLockChange={onLockChange}
      onSettled={handleWalkSettled}
      onFellOut={handleFellOut}
    />
  );
}
