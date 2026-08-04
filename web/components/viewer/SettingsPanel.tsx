"use client";

/**
 * ArchX3D — Settings panel
 * ========================
 * Movement, appearance and diagnostics, in a dismissible side panel.
 *
 * What belongs here and what belongs on the toolbar
 * ------------------------------------------------
 * The toolbar holds what a user touches repeatedly in a session — mode, roof,
 * view, screenshot. This panel holds what they set once and forget: walking
 * speed, eye height, mouse sensitivity, environment, exposure.
 *
 * The distinction matters because the toolbar is always visible and every
 * control added to it costs the model screen space. A setting that is adjusted
 * once per user, not once per minute, does not earn that.
 *
 * Live application
 * ----------------
 * Every control writes straight through to the settings store, and the frame
 * loop reads it on the next tick. Dragging the speed slider changes how fast
 * you are walking *while you are walking* — there is no apply step, because
 * these are all values whose effect you can only judge by feeling them.
 */

import { useCallback } from "react";

import { useViewerSettings } from "@/hooks/useViewerSettings";
import { SETTING_BOUNDS, type NumericSetting } from "@/lib/viewer/settings";
import type { EnvironmentPreset, ModelStats, ViewerSettings } from "@/types/viewer";
import { ENVIRONMENT_PRESETS } from "@/types/viewer";
import { CloseIcon } from "./icons";

export interface SettingsPanelProps {
  readonly open: boolean;
  readonly onClose: () => void;
  readonly stats: ModelStats | null;
  /** Proportion of meshes classified from metadata rather than inference. */
  readonly detectionRatio: number;
  readonly isDevelopment: boolean;
}

export function SettingsPanel({
  open,
  onClose,
  stats,
  detectionRatio,
  isDevelopment,
}: SettingsPanelProps) {
  const { settings, update, toggle, reset } = useViewerSettings();

  const setNumber = useCallback(
    (key: NumericSetting) => (event: React.ChangeEvent<HTMLInputElement>) => {
      update({ [key]: Number(event.target.value) } as Partial<ViewerSettings>);
    },
    [update],
  );

  if (!open) return null;

  return (
    <aside
      aria-label="Viewer settings"
      className="scroll-slim pointer-events-auto absolute top-0 right-0 bottom-0 z-30 w-[min(20rem,100vw)] overflow-y-auto border-l border-line bg-raised/95 backdrop-blur-xl"
    >
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-line-subtle bg-raised/95 px-5 py-4 backdrop-blur-xl">
        <h2 className="text-sm font-semibold text-primary">Settings</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close settings"
          className="flex h-7 w-7 items-center justify-center rounded-lg text-tertiary transition-colors hover:bg-surface-hover hover:text-primary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
        >
          <CloseIcon className="h-4 w-4" />
        </button>
      </header>

      <div className="space-y-7 px-5 py-5">
        {/* ---- Movement ------------------------------------------------- */}
        <Section title="Movement">
          <Slider
            label="Walking speed"
            value={settings.walkSpeed}
            unit="m/s"
            bounds={SETTING_BOUNDS.walkSpeed}
            onChange={setNumber("walkSpeed")}
          />
          <Slider
            label="Run multiplier"
            value={settings.runMultiplier}
            unit="×"
            bounds={SETTING_BOUNDS.runMultiplier}
            onChange={setNumber("runMultiplier")}
            hint="Applied while Shift is held"
          />
          <Slider
            label="Eye height"
            value={settings.eyeHeight}
            unit="m"
            bounds={SETTING_BOUNDS.eyeHeight}
            onChange={setNumber("eyeHeight")}
          />
          <Slider
            label="Mouse sensitivity"
            value={settings.lookSensitivity}
            unit=""
            format={(v) => (v * 1000).toFixed(1)}
            bounds={SETTING_BOUNDS.lookSensitivity}
            onChange={setNumber("lookSensitivity")}
          />
          <Toggle
            label="Collision"
            checked={settings.collisionEnabled}
            onChange={() => toggle("collisionEnabled")}
            hint="Stops the camera passing through walls. Off enables free flight."
          />
          <Toggle
            label="Jumping"
            checked={settings.jumpEnabled}
            onChange={() => toggle("jumpEnabled")}
            hint="Off by default — a building walkthrough is not a platformer."
            disabled={!settings.collisionEnabled}
          />
        </Section>

        {/* ---- Appearance ------------------------------------------------ */}
        <Section title="Appearance">
          <Field label="Environment" hint="Drives reflections, not the background">
            <select
              value={settings.environment}
              onChange={(event) =>
                update({ environment: event.target.value as EnvironmentPreset })
              }
              className="h-8 w-full cursor-pointer rounded-lg border border-line bg-surface px-2.5 text-[13px] text-primary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
            >
              {ENVIRONMENT_PRESETS.map((preset) => (
                <option key={preset} value={preset} className="bg-raised">
                  {preset[0].toUpperCase() + preset.slice(1)}
                </option>
              ))}
            </select>
          </Field>
          <Slider
            label="Exposure"
            value={settings.exposure}
            unit=""
            bounds={SETTING_BOUNDS.exposure}
            onChange={setNumber("exposure")}
          />
          <Slider
            label="Ambient light"
            value={settings.ambientIntensity}
            unit=""
            bounds={SETTING_BOUNDS.ambientIntensity}
            onChange={setNumber("ambientIntensity")}
          />
          <Toggle
            label="Shadows"
            checked={settings.shadows}
            onChange={() => toggle("shadows")}
            hint="The most expensive setting here. Turn off if the frame rate drops."
          />
          <Toggle
            label="Ground grid"
            checked={settings.showGrid}
            onChange={() => toggle("showGrid")}
          />
          <Toggle
            label="Minimap"
            checked={settings.showMinimap}
            onChange={() => toggle("showMinimap")}
            hint="Needs room data in the model"
          />
        </Section>

        {/* ---- Model ------------------------------------------------------ */}
        {stats && (
          <Section title="Model">
            <dl className="space-y-1.5 font-mono text-[11px] tabular-nums">
              <Stat label="Meshes" value={stats.meshes.toLocaleString()} />
              <Stat label="Triangles" value={stats.triangles.toLocaleString()} />
              <Stat label="Materials" value={String(stats.materials)} />
              <Stat label="Textures" value={String(stats.textures)} />
              <Stat
                label="Size"
                value={`${stats.size[0].toFixed(1)} × ${stats.size[2].toFixed(1)} × ${stats.size[1].toFixed(1)} m`}
              />
              <Stat
                label="Classified"
                value={`${Math.round(detectionRatio * 100)}% from metadata`}
                // Below half means most of the model is being classified by
                // name or shape, so the roof toggle and the view modes are
                // working from inference. Worth saying, not worth alarming.
                warn={detectionRatio < 0.5}
              />
            </dl>

            <details className="mt-3">
              <summary className="cursor-pointer text-[11px] text-tertiary transition-colors hover:text-secondary">
                Breakdown by element
              </summary>
              <dl className="mt-2 space-y-1 font-mono text-[11px] tabular-nums">
                {Object.entries(stats.byKind)
                  .filter(([, count]) => count > 0)
                  .sort(([, a], [, b]) => b - a)
                  .map(([kind, count]) => (
                    <Stat key={kind} label={kind} value={String(count)} />
                  ))}
              </dl>
            </details>
          </Section>
        )}

        {/* ---- Diagnostics ------------------------------------------------ */}
        {isDevelopment && (
          <Section title="Diagnostics">
            <Toggle
              label="Frame rate"
              checked={settings.showStats}
              onChange={() => toggle("showStats")}
              hint="Development builds only"
            />
          </Section>
        )}

        <button
          type="button"
          onClick={reset}
          className="w-full rounded-lg border border-line px-4 py-2 text-[13px] font-medium text-secondary transition-colors hover:border-line-strong hover:text-primary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
        >
          Reset to defaults
        </button>
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-3 font-mono text-[10px] tracking-widest text-tertiary uppercase">
        {title}
      </h3>
      <div className="space-y-4">{children}</div>
    </section>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-[13px] text-secondary">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[11px] leading-relaxed text-tertiary">{hint}</p>}
    </div>
  );
}

function Slider({
  label,
  value,
  unit,
  bounds,
  onChange,
  hint,
  format,
}: {
  label: string;
  value: number;
  unit: string;
  bounds: { min: number; max: number; step: number };
  onChange: (event: React.ChangeEvent<HTMLInputElement>) => void;
  hint?: string;
  format?: (value: number) => string;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <label className="text-[13px] text-secondary">{label}</label>
        <span className="font-mono text-[11px] text-tertiary tabular-nums">
          {format ? format(value) : value.toFixed(2)}
          {unit && ` ${unit}`}
        </span>
      </div>
      <input
        type="range"
        min={bounds.min}
        max={bounds.max}
        step={bounds.step}
        value={value}
        onChange={onChange}
        aria-label={label}
        className="h-1 w-full cursor-pointer appearance-none rounded-full bg-white/[0.09] accent-[--accent-solid] focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
      />
      {hint && <p className="mt-1 text-[11px] leading-relaxed text-tertiary">{hint}</p>}
    </div>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  hint,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: () => void;
  hint?: string;
  disabled?: boolean;
}) {
  return (
    <div>
      <label
        className={[
          "flex items-center justify-between gap-3",
          disabled ? "cursor-not-allowed opacity-45" : "cursor-pointer",
        ].join(" ")}
      >
        <span className="text-[13px] text-secondary">{label}</span>
        <span className="relative inline-flex shrink-0">
          <input
            type="checkbox"
            checked={checked}
            disabled={disabled}
            onChange={onChange}
            className="peer sr-only"
          />
          <span className="block h-5 w-9 rounded-full bg-white/[0.12] transition-colors peer-checked:bg-accent-solid peer-focus-visible:ring-2 peer-focus-visible:ring-focus" />
          <span className="pointer-events-none absolute top-0.5 left-0.5 block h-4 w-4 rounded-full bg-white transition-transform peer-checked:translate-x-4" />
        </span>
      </label>
      {hint && <p className="mt-1 text-[11px] leading-relaxed text-tertiary">{hint}</p>}
    </div>
  );
}

function Stat({
  label,
  value,
  warn,
}: {
  label: string;
  value: string;
  warn?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-tertiary capitalize">{label}</dt>
      <dd className={warn ? "text-warning-text" : "text-secondary"}>{value}</dd>
    </div>
  );
}
