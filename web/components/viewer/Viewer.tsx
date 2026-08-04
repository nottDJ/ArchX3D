"use client";

/**
 * ArchX3D — Interactive architectural viewer
 * ==========================================
 * The top-level component: owns the canvas, the DOM overlays, keyboard
 * shortcuts, fullscreen and screenshots, and joins them to the scene through a
 * single imperative command handle.
 *
 * The DOM / scene boundary
 * ------------------------
 * Everything in this file is DOM. Everything inside `<Canvas>` is three.js, and
 * lives in `Scene.tsx`. The two communicate in exactly two ways:
 *
 * * **Down** — props on `<Scene>`, which are plain data.
 * * **Up** — `commandsRef`, populated from inside the canvas by
 *   `CameraController`, and a handful of callbacks reporting state the overlays
 *   need to draw.
 *
 * Keeping that boundary at a file boundary is what stops a `<div>` being
 * written into the scene graph, which fails as a blank canvas with no error.
 *
 * Re-render discipline
 * --------------------
 * `<Scene>` receives `settings` — an object that only changes identity when a
 * setting really changes — plus stable callbacks. The frame loop never causes a
 * React render: FPS is sampled at 2 Hz, the minimap pose at 15 Hz and only when
 * the camera has actually moved, and everything else reads the settings store
 * imperatively. Walking around a building costs zero renders.
 */

import { Canvas } from "@react-three/fiber";
import Link from "next/link";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ViewerCommands } from "./CameraController";
import { LoadingOverlay } from "./LoadingOverlay";
import { Minimap } from "./Minimap";
import { RoomNavigator } from "./RoomNavigator";
import { RendererSettings, Scene } from "./Scene";
import { SettingsPanel } from "./SettingsPanel";
import { Toolbar } from "./Toolbar";
import { CubeIcon, DownloadIcon, WalkIcon } from "./icons";
import { cn } from "@/components/ui";
import { useGLTFModel } from "@/hooks/useGLTFModel";
import { detectionQuality, type ModelIndex } from "@/hooks/useRoofDetection";
import { useViewerSettings } from "@/hooks/useViewerSettings";
import type { RoomInfo, ViewerSource, ViewMode } from "@/types/viewer";
import { VIEW_MODES } from "@/types/viewer";

const IS_DEVELOPMENT = process.env.NODE_ENV !== "production";

/**
 * Keys the walk controller consumes.
 *
 * While the pointer is locked these are movement, not shortcuts — so `W` walks
 * forward rather than switching to walk mode, and `S` steps back rather than
 * opening settings. Suppressing the shortcut is the only correct resolution:
 * the alternative is a viewer where walking backwards opens a panel.
 */
const MOVEMENT_CODES = new Set([
  "KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE", "Space",
  "ShiftLeft", "ShiftRight",
]);

export interface ViewerProps {
  readonly source: ViewerSource;
  /** Rendered in the header — usually a link back to where the user came from. */
  readonly backHref?: string;
  readonly backLabel?: string;
  /**
   * Fill the parent instead of the viewport.
   *
   * The viewer is normally the whole page, so `h-screen` is right. Embedded —
   * in the comparison view, two side by side — it must take its height from
   * its container, and `h-screen` would make each side a full viewport tall.
   * The parent is responsible for being positioned and having a height.
   */
  readonly fill?: boolean;
}

export function Viewer({
  source,
  backHref = "/",
  backLabel = "Back",
  fill = false,
}: ViewerProps) {
  const { settings, update } = useViewerSettings();
  const { model, state, retry } = useGLTFModel(source.url);

  const containerRef = useRef<HTMLDivElement>(null);
  const commandsRef = useRef<ViewerCommands | null>(null);

  const [index, setIndex] = useState<ModelIndex | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [roomsOpen, setRoomsOpen] = useState(false);
  const [selectedRoom, setSelectedRoom] = useState<string | null>(null);
  const [occupiedRoom, setOccupiedRoom] = useState<string | null>(null);
  const [pointerLocked, setPointerLocked] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [fps, setFps] = useState(0);
  const [pose, setPose] = useState<{
    position: readonly [number, number];
    heading: number;
  } | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const rooms = model?.manifest.rooms ?? [];
  const ready = state.phase === "ready" && model !== null;
  const hasRoof = (index?.roofs.length ?? 0) > 0;
  const quality = useMemo(() => detectionQuality(index), [index]);

  // -- Toast -------------------------------------------------------------
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 2600);
    return () => clearTimeout(timer);
  }, [toast]);

  // -- Fullscreen --------------------------------------------------------
  useEffect(() => {
    const handle = () => setIsFullscreen(document.fullscreenElement !== null);
    document.addEventListener("fullscreenchange", handle);
    return () => document.removeEventListener("fullscreenchange", handle);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      // Fullscreen the container, not the canvas: the overlays have to come
      // with it, or the user loses every control the moment they go fullscreen.
      void containerRef.current?.requestFullscreen().catch(() => {
        setToast("Fullscreen was blocked by the browser");
      });
    }
  }, []);

  // -- Actions -----------------------------------------------------------
  const handleFit = useCallback(() => {
    commandsRef.current?.resetCamera();
    setSelectedRoom(null);
  }, []);

  const handleScreenshot = useCallback(() => {
    const dataUrl = commandsRef.current?.screenshot();
    if (!dataUrl) {
      setToast("Screenshot failed");
      return;
    }

    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const link = document.createElement("a");
    link.href = dataUrl;
    link.download = `archx3d-${source.label.replace(/\W+/g, "-").toLowerCase()}-${stamp}.png`;
    link.click();
    setToast("Screenshot saved");
  }, [source.label]);

  const handleSelectRoom = useCallback((room: RoomInfo) => {
    setSelectedRoom(room.id);
    commandsRef.current?.flyToRoom(room);
  }, []);

  const handleClearRoom = useCallback(() => setSelectedRoom(null), []);

  const requestLock = useCallback(() => {
    commandsRef.current?.requestPointerLock();
  }, []);

  // -- Keyboard ----------------------------------------------------------
  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      // Never steal a key from a form control.
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.isContentEditable ||
          ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(target.tagName))
      ) {
        return;
      }

      // While walking, movement keys are movement. See MOVEMENT_CODES.
      if (pointerLocked && MOVEMENT_CODES.has(event.code)) return;

      switch (event.code) {
        case "KeyO":
          update({ cameraMode: "orbit" });
          break;
        case "KeyW":
          update({ cameraMode: "walk" });
          break;
        case "KeyR":
          if (hasRoof) update({ showRoof: !settings.showRoof });
          break;
        case "KeyH":
          handleFit();
          break;
        case "KeyP":
          handleScreenshot();
          break;
        case "KeyF":
          update({
            viewMode: settings.viewMode === "wireframe" ? "full" : "wireframe",
          });
          break;
        case "KeyV": {
          // Cycle forward through the view modes.
          const order = VIEW_MODES.map((mode) => mode.id);
          const next = order[(order.indexOf(settings.viewMode) + 1) % order.length];
          update({ viewMode: next as ViewMode });
          break;
        }
        case "KeyS":
          setSettingsOpen((open) => !open);
          break;
        case "KeyM":
          if (rooms.length > 0) setRoomsOpen((open) => !open);
          break;
        case "Enter":
          toggleFullscreen();
          break;
        default:
          return;
      }

      event.preventDefault();
    };

    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [
    handleFit,
    handleScreenshot,
    hasRoof,
    pointerLocked,
    rooms.length,
    settings.showRoof,
    settings.viewMode,
    toggleFullscreen,
    update,
  ]);

  // -- Panels are mutually exclusive on narrow viewports ------------------
  const openSettings = useCallback(() => {
    setSettingsOpen((open) => {
      if (!open) setRoomsOpen(false);
      return !open;
    });
  }, []);

  const openRooms = useCallback(() => {
    setRoomsOpen((open) => {
      if (!open) setSettingsOpen(false);
      return !open;
    });
  }, []);

  const handlePose = useCallback(
    (position: readonly [number, number], heading: number) => {
      setPose({ position, heading });
    },
    [],
  );

  const showWalkPrompt = ready && settings.cameraMode === "walk" && !pointerLocked;

  return (
    <div
      ref={containerRef}
      className={cn(
        "w-full overflow-hidden bg-canvas text-primary antialiased",
        fill ? "absolute inset-0 h-full" : "relative h-screen",
      )}
    >
      {/* ---- Canvas ------------------------------------------------------ */}
      <Canvas
        // `high-performance` asks a dual-GPU laptop for the discrete chip; the
        // default can silently land on integrated graphics and halve the frame
        // rate for no visible reason.
        gl={{ antialias: true, powerPreference: "high-performance" }}
        dpr={[1, 2]}
        shadows={settings.shadows}
        camera={{ fov: 55, near: 0.05, far: 1000, position: [8, 6, 8] }}
        // Only redraw when something changed. An architectural model being
        // looked at rather than walked through is a still image, and rendering
        // it 60 times a second drains a laptop battery for nothing. The
        // controllers invalidate on movement, so interaction stays smooth.
        frameloop={settings.cameraMode === "walk" ? "always" : "demand"}
        className="absolute inset-0"
      >
        <RendererSettings shadows={settings.shadows} />
        {/*
          Suspense catches drei's `Environment`, which fetches an HDRI. The
          model itself is loaded outside Suspense so its progress can be shown;
          see the note in `useGLTFModel`.
        */}
        <Suspense fallback={null}>
          <Scene
            scene={model?.scene ?? null}
            settings={settings}
            rooms={rooms}
            modelUrl={source.url}
            commandsRef={commandsRef}
            highlightRoom={selectedRoom}
            onIndexed={setIndex}
            onLockChange={setPointerLocked}
            onRoomChange={setOccupiedRoom}
            onPose={handlePose}
            onFps={IS_DEVELOPMENT && settings.showStats ? setFps : undefined}
          />
        </Suspense>
      </Canvas>

      {/* ---- Header ------------------------------------------------------ */}
      <header className="pointer-events-none absolute inset-x-0 top-0 z-20 flex items-start justify-between gap-3 p-4">
        <div className="pointer-events-auto flex items-center gap-2.5 rounded-xl border border-line bg-raised/80 px-3 py-2 backdrop-blur-xl">
          <span className="flex h-6 w-6 items-center justify-center rounded-md border border-line bg-surface text-accent-text">
            <CubeIcon className="h-3.5 w-3.5" />
          </span>
          <div className="min-w-0">
            <p className="truncate text-[13px] font-medium text-primary">
              {source.label}
            </p>
            {ready && index && (
              <p className="font-mono text-[10px] text-tertiary tabular-nums">
                {index.stats.triangles.toLocaleString()} tris ·{" "}
                {index.stats.size[0].toFixed(1)} × {index.stats.size[2].toFixed(1)} m
              </p>
            )}
          </div>
        </div>

        <div className="pointer-events-auto flex items-center gap-2">
          <a
            href={source.url}
            download
            title="Download the GLB"
            className="flex h-9 items-center gap-2 rounded-xl border border-line bg-raised/80 px-3 text-[13px] text-secondary backdrop-blur-xl transition-colors hover:border-line-strong hover:text-primary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
          >
            <DownloadIcon className="h-4 w-4" />
            <span className="hidden sm:inline">GLB</span>
          </a>
          <Link
            href={backHref}
            className="flex h-9 items-center rounded-xl border border-line bg-raised/80 px-3 text-[13px] text-secondary backdrop-blur-xl transition-colors hover:border-line-strong hover:text-primary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
          >
            {backLabel}
          </Link>
        </div>
      </header>

      {/* ---- Minimap ----------------------------------------------------- */}
      {ready && settings.showMinimap && !settingsOpen && (
        <div className="pointer-events-none absolute inset-0 z-10">
          <div className="absolute top-20 right-0">
            <Minimap
              rooms={rooms}
              position={pose?.position ?? null}
              heading={pose?.heading ?? 0}
              selected={selectedRoom}
              occupied={occupiedRoom}
              onSelect={handleSelectRoom}
            />
          </div>
        </div>
      )}

      {/* ---- Walk prompt ------------------------------------------------- */}
      {showWalkPrompt && (
        <button
          type="button"
          onClick={requestLock}
          className="absolute inset-0 z-20 flex cursor-pointer items-center justify-center bg-canvas/45 backdrop-blur-[2px] focus-visible:outline-none"
        >
          <div className="rounded-2xl border border-line bg-raised/90 px-7 py-6 text-center shadow-2xl shadow-black/50 backdrop-blur-xl">
            <span className="mx-auto mb-4 flex h-10 w-10 items-center justify-center rounded-xl border border-line bg-surface text-accent-text">
              <WalkIcon className="h-5 w-5" />
            </span>
            <p className="text-sm font-medium text-primary">Click to look around</p>
            <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-left font-mono text-[11px]">
              <Key>WASD</Key>
              <dd className="text-tertiary">Move</dd>
              <Key>Mouse</Key>
              <dd className="text-tertiary">Look</dd>
              <Key>Shift</Key>
              <dd className="text-tertiary">Run</dd>
              <Key>Esc</Key>
              <dd className="text-tertiary">Release the cursor</dd>
            </dl>
          </div>
        </button>
      )}

      {/* ---- Panels ------------------------------------------------------ */}
      <RoomNavigator
        open={roomsOpen}
        rooms={rooms}
        selected={selectedRoom}
        occupied={occupiedRoom}
        onSelect={handleSelectRoom}
        onClear={handleClearRoom}
        onClose={() => setRoomsOpen(false)}
      />

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        stats={index?.stats ?? null}
        detectionRatio={quality.ratio}
        isDevelopment={IS_DEVELOPMENT}
      />

      {/* ---- Toolbar ----------------------------------------------------- */}
      <Toolbar
        settings={settings}
        onUpdate={update}
        onFit={handleFit}
        onScreenshot={handleScreenshot}
        onToggleFullscreen={toggleFullscreen}
        onToggleSettings={openSettings}
        onToggleRooms={openRooms}
        isFullscreen={isFullscreen}
        settingsOpen={settingsOpen}
        roomsOpen={roomsOpen}
        hasRoof={hasRoof}
        hasRooms={rooms.length > 0}
        disabled={!ready}
      />

      {/* ---- Diagnostics -------------------------------------------------- */}
      {IS_DEVELOPMENT && settings.showStats && ready && (
        <div className="pointer-events-none absolute bottom-4 left-4 z-20 rounded-lg border border-line bg-raised/85 px-2.5 py-1.5 font-mono text-[11px] text-secondary tabular-nums backdrop-blur-xl">
          <span className={fps < 30 ? "text-warning-text" : "text-success-text"}>
            {fps.toFixed(0)} fps
          </span>
          {index && <span className="ml-2 text-tertiary">{index.stats.meshes} meshes</span>}
        </div>
      )}

      {/* ---- Toast --------------------------------------------------------- */}
      {toast && (
        <div
          role="status"
          className="animate-rise-in pointer-events-none absolute bottom-24 left-1/2 z-30 -translate-x-1/2 rounded-lg border border-line bg-raised/95 px-3.5 py-2 text-[13px] text-primary shadow-xl backdrop-blur-xl"
        >
          {toast}
        </div>
      )}

      {/* ---- Loading / error ------------------------------------------------ */}
      <LoadingOverlay state={state} onRetry={retry}>
        <Link
          href={backHref}
          className="rounded-lg border border-line px-4 py-2 text-sm font-medium text-secondary transition-colors hover:border-line-strong hover:text-primary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
        >
          {backLabel}
        </Link>
      </LoadingOverlay>
    </div>
  );
}

function Key({ children }: { children: React.ReactNode }) {
  return (
    <dt className="rounded border border-line bg-surface-hover px-1.5 py-0.5 text-center text-secondary">
      {children}
    </dt>
  );
}
