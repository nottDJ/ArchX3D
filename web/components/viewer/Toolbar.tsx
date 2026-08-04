"use client";

/**
 * ArchX3D — Floating toolbar
 * ==========================
 * The viewer's primary controls, floating over the canvas rather than framing
 * it.
 *
 * Why floating
 * ------------
 * A chrome-heavy panel around a 3D view steals the space the model needs, and
 * on a laptop that is most of the window. Floating controls over a
 * corner-anchored layout give the building the whole viewport and keep every
 * action one click away. It is also what every tool in this category does —
 * Autodesk Viewer, SketchUp, Speckle — so it needs no explaining.
 *
 * Grouping
 * --------
 * Three clusters, by question:
 *
 * * **How am I moving?** — orbit / walk. A segmented control, because they are
 *   mutually exclusive and the current one must be obvious at a glance.
 * * **What am I looking at?** — view mode, roof. State, not actions: each shows
 *   whether it is on.
 * * **Do this** — fit, screenshot, fullscreen, settings. Momentary actions.
 *
 * Every control carries a keyboard shortcut in its tooltip. A viewer used for
 * more than a minute is used with the keyboard, and undiscoverable shortcuts
 * are the same as none.
 */

import { useCallback } from "react";

import type { ViewMode, ViewerSettings } from "@/types/viewer";
import { VIEW_MODES } from "@/types/viewer";
import {
  CameraIcon,
  FitIcon,
  FullscreenExitIcon,
  FullscreenIcon,
  LayersIcon,
  OrbitIcon,
  RoofIcon,
  RoofOffIcon,
  RoomsIcon,
  SettingsIcon,
  WalkIcon,
  WireframeIcon,
} from "./icons";

export interface ToolbarProps {
  readonly settings: ViewerSettings;
  readonly onUpdate: (patch: Partial<ViewerSettings>) => void;
  readonly onFit: () => void;
  readonly onScreenshot: () => void;
  readonly onToggleFullscreen: () => void;
  readonly onToggleSettings: () => void;
  readonly onToggleRooms: () => void;
  readonly isFullscreen: boolean;
  readonly settingsOpen: boolean;
  readonly roomsOpen: boolean;
  /** False when the model has no roof, which disables the roof control. */
  readonly hasRoof: boolean;
  /** False when the model carries no room metadata. */
  readonly hasRooms: boolean;
  readonly disabled: boolean;
}

export function Toolbar({
  settings,
  onUpdate,
  onFit,
  onScreenshot,
  onToggleFullscreen,
  onToggleSettings,
  onToggleRooms,
  isFullscreen,
  settingsOpen,
  roomsOpen,
  hasRoof,
  hasRooms,
  disabled,
}: ToolbarProps) {
  const setViewMode = useCallback(
    (viewMode: ViewMode) => onUpdate({ viewMode }),
    [onUpdate],
  );

  const wireframe = settings.viewMode === "wireframe";

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20 flex justify-center px-4 pb-4 sm:pb-6">
      <div className="scroll-slim pointer-events-auto flex max-w-full items-center gap-1.5 overflow-x-auto rounded-2xl border border-line bg-raised/85 p-1.5 shadow-2xl shadow-black/50 backdrop-blur-xl">
        {/* ---- Camera mode --------------------------------------------- */}
        <div
          role="radiogroup"
          aria-label="Camera mode"
          className="flex shrink-0 items-center gap-0.5 rounded-xl bg-surface-hover p-0.5"
        >
          <SegmentButton
            active={settings.cameraMode === "orbit"}
            disabled={disabled}
            onClick={() => onUpdate({ cameraMode: "orbit" })}
            icon={<OrbitIcon className="h-4 w-4" />}
            label="Orbit"
            shortcut="O"
          />
          <SegmentButton
            active={settings.cameraMode === "walk"}
            disabled={disabled}
            onClick={() => onUpdate({ cameraMode: "walk" })}
            icon={<WalkIcon className="h-4 w-4" />}
            label="Walk"
            shortcut="W"
          />
        </div>

        <Divider />

        {/* ---- View mode ----------------------------------------------- */}
        <div className="flex shrink-0 items-center gap-0.5">
          <label className="sr-only" htmlFor="archx-view-mode">
            View mode
          </label>
          <div className="relative flex items-center">
            <LayersIcon className="pointer-events-none absolute left-2.5 h-4 w-4 text-tertiary" />
            <select
              id="archx-view-mode"
              value={settings.viewMode}
              disabled={disabled}
              onChange={(event) => setViewMode(event.target.value as ViewMode)}
              title="View mode — cycle with V"
              className="h-9 cursor-pointer appearance-none rounded-lg bg-transparent py-0 pr-7 pl-8 text-[13px] font-medium text-primary transition-colors hover:bg-surface-hover focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none disabled:opacity-40"
            >
              {VIEW_MODES.map((mode) => (
                <option key={mode.id} value={mode.id} className="bg-raised">
                  {mode.label}
                </option>
              ))}
            </select>
            <svg
              aria-hidden
              viewBox="0 0 12 12"
              className="pointer-events-none absolute right-2.5 h-2.5 w-2.5 text-tertiary"
            >
              <path d="M2 4.5L6 8.5l4-4" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" />
            </svg>
          </div>

          <ToolButton
            active={wireframe}
            disabled={disabled}
            onClick={() => setViewMode(wireframe ? "full" : "wireframe")}
            label="Wireframe"
            shortcut="F"
            icon={<WireframeIcon className="h-4 w-4" />}
          />
        </div>

        <Divider />

        {/* ---- Roof ------------------------------------------------------ */}
        <ToolButton
          active={!settings.showRoof}
          disabled={disabled || !hasRoof}
          onClick={() => onUpdate({ showRoof: !settings.showRoof })}
          label={
            !hasRoof
              ? "No roof detected in this model"
              : settings.showRoof
                ? "Hide roof"
                : "Show roof"
          }
          shortcut="R"
          icon={
            settings.showRoof ? (
              <RoofIcon className="h-4 w-4" />
            ) : (
              <RoofOffIcon className="h-4 w-4" />
            )
          }
        />

        <ToolButton
          active={roomsOpen}
          disabled={disabled || !hasRooms}
          onClick={onToggleRooms}
          label={hasRooms ? "Rooms" : "No room data in this model"}
          shortcut="M"
          icon={<RoomsIcon className="h-4 w-4" />}
        />

        <Divider />

        {/* ---- Actions --------------------------------------------------- */}
        <ToolButton
          disabled={disabled}
          onClick={onFit}
          label="Reset camera"
          shortcut="H"
          icon={<FitIcon className="h-4 w-4" />}
        />
        <ToolButton
          disabled={disabled}
          onClick={onScreenshot}
          label="Screenshot"
          shortcut="P"
          icon={<CameraIcon className="h-4 w-4" />}
        />
        <ToolButton
          active={isFullscreen}
          onClick={onToggleFullscreen}
          label={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
          shortcut="Enter"
          icon={
            isFullscreen ? (
              <FullscreenExitIcon className="h-4 w-4" />
            ) : (
              <FullscreenIcon className="h-4 w-4" />
            )
          }
        />
        <ToolButton
          active={settingsOpen}
          onClick={onToggleSettings}
          label="Settings"
          shortcut="S"
          icon={<SettingsIcon className="h-4 w-4" />}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function Divider() {
  return <span aria-hidden className="mx-0.5 h-6 w-px shrink-0 bg-surface-active" />;
}

interface ButtonProps {
  readonly icon: React.ReactNode;
  readonly label: string;
  readonly shortcut?: string;
  readonly active?: boolean;
  readonly disabled?: boolean;
  readonly onClick: () => void;
}

/**
 * `title` carries the shortcut rather than a custom tooltip.
 *
 * A hand-rolled tooltip needs positioning, dismissal, touch handling and
 * accessibility work to match what the browser already does correctly, and none
 * of it would be better. `aria-label` carries the same text for screen readers,
 * where the shortcut is also useful.
 */
function ToolButton({ icon, label, shortcut, active, disabled, onClick }: ButtonProps) {
  const hint = shortcut ? `${label} (${shortcut})` : label;

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={hint}
      aria-label={hint}
      aria-pressed={active}
      className={[
        "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg transition-colors focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none",
        disabled
          ? "cursor-not-allowed text-disabled"
          : active
            ? "bg-accent-solid/20 text-accent-text"
            : "text-secondary hover:bg-surface-hover hover:text-primary",
      ].join(" ")}
    >
      {icon}
    </button>
  );
}

function SegmentButton({
  icon,
  label,
  shortcut,
  active,
  disabled,
  onClick,
}: ButtonProps) {
  const hint = shortcut ? `${label} (${shortcut})` : label;

  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      aria-label={hint}
      title={hint}
      onClick={onClick}
      disabled={disabled}
      className={[
        "flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] font-medium transition-colors focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none",
        disabled
          ? "cursor-not-allowed text-disabled"
          : active
            ? "bg-white text-on-solid"
            : "text-secondary hover:text-primary",
      ].join(" ")}
    >
      {icon}
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
}
