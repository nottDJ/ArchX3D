"use client";

/**
 * ArchX3D — GLB loading
 * =====================
 * Fetches and parses the generated model, with real progress, real errors, and
 * disposal that actually frees the GPU memory.
 *
 * Why not `useGLTF` from drei
 * ---------------------------
 * drei's helper is excellent for static assets and wrong for this. It suspends,
 * so there is no progress to show while a 14 MB building downloads; it caches
 * globally by URL, so a re-generated model at the same path serves the stale
 * one; and it never disposes, because it assumes the asset is reused. A viewer
 * that opens one large model, replaces it, and must not leak between them needs
 * explicit control of all three.
 *
 * Compression support
 * -------------------
 * Draco, Meshopt and KTX2 are all wired up even though the current Blender
 * export uses none of them. They cost nothing when absent — the decoders are
 * only fetched if the file actually references the extension — and they mean a
 * future exporter change needs no viewer change. See `docs/VIEWER.md`.
 *
 * Disposal
 * --------
 * Three.js does not garbage-collect GPU resources. Dropping a reference to a
 * `Scene` leaves its geometries, materials and textures resident until the
 * context is lost. Every one is disposed explicitly on unmount and on URL
 * change; skipping this leaks tens of megabytes per model viewed.
 */

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { DRACOLoader } from "three/examples/jsm/loaders/DRACOLoader.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { KTX2Loader } from "three/examples/jsm/loaders/KTX2Loader.js";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";

import { parseManifest } from "@/lib/viewer/manifest";
import type { LoadState, SceneManifest } from "@/types/viewer";

/**
 * Decoders are served from the three.js CDN at the version we build against.
 *
 * Pinned to `three`'s own version so the decoder can never disagree with the
 * loader that drives it. Self-hosting these is a deployment decision documented
 * in `VIEWER.md`; the default keeps the repository free of binary blobs.
 */
const THREE_VERSION = THREE.REVISION;
const DRACO_PATH = `https://www.gstatic.com/draco/versioned/decoders/1.5.7/`;
const KTX2_PATH = `https://unpkg.com/three@0.${THREE_VERSION}.0/examples/jsm/libs/basis/`;

export interface LoadedModel {
  readonly scene: THREE.Group;
  readonly manifest: SceneManifest;
  /** Transferred bytes, when the server reported a length. */
  readonly bytes: number | null;
}

export interface UseGLTFModel {
  readonly model: LoadedModel | null;
  readonly state: LoadState;
  /** Re-fetch, bypassing any HTTP cache. */
  readonly retry: () => void;
}

const IDLE: LoadState = {
  phase: "idle",
  progress: null,
  loadedBytes: 0,
  totalBytes: null,
  error: null,
};

// ---------------------------------------------------------------------------
// Loader construction
// ---------------------------------------------------------------------------

/**
 * Build a loader with every compression extension wired in.
 *
 * KTX2 needs a renderer to decide which GPU texture formats are available. The
 * hook runs outside the canvas, so a throwaway context is created purely to ask
 * that question and immediately released. If WebGL is unavailable — a headless
 * test, a blocked context — KTX2 is skipped rather than throwing, because a
 * model with no compressed textures still loads perfectly.
 */
function createLoader(): { loader: GLTFLoader; dispose: () => void } {
  const loader = new GLTFLoader();

  const draco = new DRACOLoader().setDecoderPath(DRACO_PATH);
  loader.setDRACOLoader(draco);
  loader.setMeshoptDecoder(MeshoptDecoder);

  let ktx2: KTX2Loader | null = null;
  try {
    const probe = new THREE.WebGLRenderer();
    ktx2 = new KTX2Loader().setTranscoderPath(KTX2_PATH).detectSupport(probe);
    loader.setKTX2Loader(ktx2);
    probe.dispose();
    probe.forceContextLoss();
  } catch {
    // No WebGL context available. Uncompressed textures still work.
  }

  return {
    loader,
    dispose: () => {
      draco.dispose();
      ktx2?.dispose();
    },
  };
}

// ---------------------------------------------------------------------------
// Disposal
// ---------------------------------------------------------------------------

function disposeMaterial(material: THREE.Material): void {
  for (const value of Object.values(material)) {
    // Every map on a material is a Texture, whatever the slot is called;
    // enumerating beats maintaining a list that a three.js release will outgrow.
    if (value instanceof THREE.Texture) value.dispose();
  }
  material.dispose();
}

/** Release every GPU resource the model holds. */
export function disposeScene(root: THREE.Object3D): void {
  root.traverse((object) => {
    const mesh = object as Partial<THREE.Mesh>;
    mesh.geometry?.dispose();

    const material = mesh.material;
    if (Array.isArray(material)) material.forEach(disposeMaterial);
    else if (material) disposeMaterial(material);
  });
}

// ---------------------------------------------------------------------------
// The hook
// ---------------------------------------------------------------------------

export function useGLTFModel(url: string | null): UseGLTFModel {
  const [model, setModel] = useState<LoadedModel | null>(null);
  const [state, setState] = useState<LoadState>(IDLE);
  const [attempt, setAttempt] = useState(0);

  // Progress fires far more often than the UI can usefully redraw. Throttling
  // to ~15 Hz keeps the bar smooth while cutting renders by an order of
  // magnitude on a fast connection.
  const lastProgressAt = useRef(0);

  useEffect(() => {
    if (!url) {
      setModel(null);
      setState(IDLE);
      return;
    }

    let cancelled = false;
    let loaded: THREE.Group | null = null;

    const { loader, dispose } = createLoader();
    setState({ ...IDLE, phase: "downloading" });

    // A retry must not be answered from the cache that produced the failure.
    const request = attempt === 0 ? url : `${url}${url.includes("?") ? "&" : "?"}retry=${attempt}`;

    loader.load(
      request,
      (gltf) => {
        if (cancelled) {
          disposeScene(gltf.scene);
          return;
        }
        loaded = gltf.scene;

        // Scene-level `extras` land on `userData`; older files simply have none.
        const manifest = parseManifest(gltf.scene.userData);

        setModel({ scene: gltf.scene, manifest, bytes: null });
        setState((previous) => ({
          ...previous,
          phase: "ready",
          progress: 1,
          error: null,
        }));
      },
      (event) => {
        if (cancelled) return;

        const now = performance.now();
        const total = event.total > 0 ? event.total : null;
        const finished = total !== null && event.loaded >= total;

        // Always report the last event, however soon it arrives — otherwise the
        // bar can freeze at 94% while the parse runs.
        if (!finished && now - lastProgressAt.current < 66) return;
        lastProgressAt.current = now;

        setState({
          phase: finished ? "parsing" : "downloading",
          progress: total ? Math.min(1, event.loaded / total) : null,
          loadedBytes: event.loaded,
          totalBytes: total,
          error: null,
        });
      },
      (error) => {
        if (cancelled) return;
        setState({
          ...IDLE,
          phase: "error",
          error: describeLoadError(error, url),
        });
      },
    );

    return () => {
      cancelled = true;
      dispose();
      // The scene may have arrived after unmount; dispose whichever exists.
      if (loaded) disposeScene(loaded);
    };
  }, [url, attempt]);

  // Dispose the outgoing model whenever it is replaced or the viewer closes.
  useEffect(() => {
    return () => {
      if (model) disposeScene(model.scene);
    };
  }, [model]);

  return {
    model,
    state,
    retry: () => {
      setModel(null);
      setAttempt((n) => n + 1);
    },
  };
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/**
 * Turn a loader failure into something a user can act on.
 *
 * `GLTFLoader` reports almost everything as a bare `ErrorEvent`, so the message
 * is usually empty and always unhelpful. The three failures that actually
 * happen here — the backend is not running, the model was not generated, the
 * file is corrupt — each have a different fix, and saying which is the
 * difference between a user retrying and a user giving up.
 */
export function describeLoadError(error: unknown, url: string): string {
  const raw =
    error instanceof Error
      ? error.message
      : typeof error === "object" && error !== null && "message" in error
        ? String((error as { message: unknown }).message)
        : String(error ?? "");

  const text = raw.toLowerCase();
  const origin = safeOrigin(url);

  if (text.includes("404") || text.includes("not found")) {
    return `No model at ${url}. The build may still be running, or it finished without producing a GLB.`;
  }
  if (text.includes("failed to fetch") || text.includes("networkerror") || raw === "") {
    return `Could not reach ${origin}. Check that the ArchX3D server is running and that it allows requests from this page.`;
  }
  if (text.includes("json") || text.includes("parse") || text.includes("glb")) {
    return "The model downloaded but could not be parsed. It may be truncated — try regenerating it.";
  }
  return raw || "The model could not be loaded.";
}

function safeOrigin(url: string): string {
  try {
    return new URL(url, typeof window === "undefined" ? undefined : window.location.href)
      .origin;
  } catch {
    return url;
  }
}
