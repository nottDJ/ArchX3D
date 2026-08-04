"use client";

import { useState } from "react";

import type { ReviewPayload, ReviewRoom } from "@/lib/wizard";
import type { LightOverride, RoomFinishEdit } from "@/lib/editor";
import { Field, MaterialPicker, NumberField, selectClass } from "./Inspector";
import { BulbIcon, PaletteIcon, TrashIcon, WallIcon } from "./icons";

export interface RoomEditorProps {
  room: ReviewRoom;
  vocabulary: ReviewPayload["vocabulary"];
  finishEdit?: RoomFinishEdit;
  lightEdits: Record<string, LightOverride>;
  removedLights: Set<string>;
  onFinish: (patch: RoomFinishEdit) => void;
  onLight: (lightId: string, patch: LightOverride) => void;
  onRemoveLight: (lightId: string) => void;
  onAddLight: (kind: string) => void;
}

/**
 * Surface finishes and lighting for one room.
 *
 * These are room-scoped rather than object-scoped because that is how they are
 * observed and how they are built: the vision pipeline reports one wall finish
 * per room, and the generator applies it to every wall bounding that room.
 * Editing a surface here promotes it to a per-room override, so changing the
 * living room's walls does not repaint the bedroom.
 */
export function RoomEditor({
  room,
  vocabulary,
  finishEdit,
  lightEdits,
  removedLights,
  onFinish,
  onLight,
  onRemoveLight,
  onAddLight,
}: RoomEditorProps) {
  const [open, setOpen] = useState<"finishes" | "lighting" | null>(null);

  return (
    <div className="border-t border-line-subtle">
      <div className="flex flex-wrap gap-1.5 px-4 py-2.5">
        <Tab
          active={open === "finishes"}
          onClick={() => setOpen(open === "finishes" ? null : "finishes")}
        >
          <PaletteIcon className="h-3.5 w-3.5" />
          Finishes
        </Tab>
        <Tab
          active={open === "lighting"}
          onClick={() => setOpen(open === "lighting" ? null : "lighting")}
        >
          <BulbIcon className="h-3.5 w-3.5" />
          Lighting
          <span className="text-tertiary">({room.lights.length})</span>
        </Tab>
      </div>

      {open === "finishes" && (
        <div className="grid gap-4 border-t border-line-subtle px-4 py-3 sm:grid-cols-3">
          {(["wall", "floor", "ceiling"] as const).map((surface) => {
            const detected = room.finishes[surface];
            const pending = finishEdit?.[surface];
            return (
              <section key={surface}>
                <h5 className="mb-2 flex items-center gap-1.5 text-[11px] font-medium text-secondary capitalize">
                  <WallIcon className="h-3 w-3 text-tertiary" />
                  {surface}
                </h5>
                <MaterialPicker
                  surface={surface}
                  material={pending?.material ?? detected?.material ?? null}
                  colour={pending?.color_hex ?? detected?.color_hex ?? null}
                  vocabulary={vocabulary}
                  onMaterial={(material) => onFinish({ [surface]: { material } })}
                  onColour={(color_hex) => onFinish({ [surface]: { color_hex } })}
                />
              </section>
            );
          })}

          <Field label="Ceiling type">
            <select
              value={finishEdit?.ceiling_type ?? room.finishes.ceiling_type}
              onChange={(event) => onFinish({ ceiling_type: event.target.value })}
              className={selectClass}
            >
              {vocabulary.ceiling_types.map((value) => (
                <option key={value} value={value}>
                  {value.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </Field>
        </div>
      )}

      {open === "lighting" && (
        <div className="space-y-2 border-t border-line-subtle px-4 py-3">
          {room.lights.length === 0 && (
            <p className="py-2 text-[11px] text-tertiary">
              No fixtures were recovered for this room. The generator will fall
              back to default lighting unless you add one.
            </p>
          )}

          {room.lights.map((light) => {
            const pending = lightEdits[light.id] ?? {};
            const removed = removedLights.has(light.id);
            return (
              <article
                key={light.id}
                className={`rounded-lg border border-line-subtle p-2.5 ${
                  removed ? "opacity-40" : ""
                }`}
              >
                <div className="mb-2 flex items-center gap-2">
                  <select
                    value={pending.kind ?? light.kind}
                    disabled={removed}
                    onChange={(event) => onLight(light.id, { kind: event.target.value })}
                    className={`${selectClass} flex-1`}
                  >
                    {vocabulary.light_kinds.map((entry) => (
                      <option key={entry.kind} value={entry.kind}>
                        {entry.kind.replace(/_/g, " ")}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => onRemoveLight(light.id)}
                    title="Remove this fixture"
                    className="rounded-md p-1.5 text-tertiary transition-colors hover:bg-surface-hover hover:text-danger-text"
                  >
                    <TrashIcon className="h-3.5 w-3.5" />
                  </button>
                </div>

                <div className="grid grid-cols-3 gap-2">
                  <NumberField
                    label="Power"
                    unit="W"
                    step={5}
                    min={0}
                    max={5000}
                    value={pending.power_w ?? light.power_w}
                    disabled={removed}
                    onChange={(power_w) => onLight(light.id, { power_w })}
                  />
                  <NumberField
                    label="Temp"
                    unit="K"
                    step={100}
                    min={1500}
                    max={10000}
                    value={pending.color_temperature_k ?? light.color_temperature_k}
                    disabled={removed}
                    onChange={(color_temperature_k) =>
                      onLight(light.id, { color_temperature_k })
                    }
                  />
                  <NumberField
                    label="Height"
                    unit="m"
                    step={0.1}
                    min={0}
                    max={room.ceiling_height}
                    value={pending.position?.z ?? light.position.z}
                    disabled={removed}
                    onChange={(z) =>
                      onLight(light.id, {
                        position: {
                          x: pending.position?.x ?? light.position.x,
                          y: pending.position?.y ?? light.position.y,
                          z,
                        },
                      })
                    }
                  />
                </div>

                <div className="mt-2 flex items-center gap-2">
                  <span
                    className="h-3 w-full rounded-full"
                    style={{
                      background: `linear-gradient(90deg, ${kelvinToHex(
                        pending.color_temperature_k ?? light.color_temperature_k,
                      )}, transparent)`,
                    }}
                  />
                  <span className="shrink-0 font-mono text-[10px] text-tertiary">
                    {Math.round(pending.color_temperature_k ?? light.color_temperature_k)}K
                  </span>
                </div>
              </article>
            );
          })}

          <div className="flex items-center gap-2 pt-1">
            <select
              defaultValue=""
              onChange={(event) => {
                if (event.target.value) {
                  onAddLight(event.target.value);
                  event.target.value = "";
                }
              }}
              className={selectClass}
            >
              <option value="">Add a fixture…</option>
              {vocabulary.light_kinds.map((entry) => (
                <option key={entry.kind} value={entry.kind}>
                  {entry.kind.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
    </div>
  );
}

function Tab({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] transition-colors ${
        active
          ? "border-accent-border bg-accent-surface text-accent-text"
          : "border-line text-secondary hover:border-line-strong hover:text-primary"
      }`}
    >
      {children}
    </button>
  );
}

/**
 * Approximate sRGB for a colour temperature, for the preview swatch only.
 *
 * A rough piecewise fit of the Planckian locus — enough to tell warm from cool
 * at a glance, and deliberately not used for anything the renderer consumes,
 * which works from the Kelvin value directly.
 */
function kelvinToHex(kelvin: number): string {
  const t = Math.min(10000, Math.max(1500, kelvin)) / 100;

  const red = t <= 66 ? 255 : clamp(329.7 * Math.pow(t - 60, -0.1332));
  const green =
    t <= 66
      ? clamp(99.47 * Math.log(t) - 161.12)
      : clamp(288.12 * Math.pow(t - 60, -0.0755));
  const blue =
    t >= 66 ? 255 : t <= 19 ? 0 : clamp(138.52 * Math.log(t - 10) - 305.04);

  return `#${[red, green, blue]
    .map((value) => Math.round(value).toString(16).padStart(2, "0"))
    .join("")}`;
}

function clamp(value: number): number {
  return Math.min(255, Math.max(0, value));
}
