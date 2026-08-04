"use client";

import { useMemo, useState } from "react";

import {
  MAX_DIMENSION,
  MIN_DIMENSION,
  formatCategory,
  formatRoomType,
  type ObjectOverride,
  type ReviewObject,
  type ReviewPayload,
  type ReviewRoom,
} from "@/lib/wizard";
import {
  LockIcon,
  ResetIcon,
  TrashIcon,
  UndoIcon,
  UnlockIcon,
} from "./icons";

type Tab = "placement" | "asset" | "material";

export interface InspectorProps {
  /** The selected objects, with pending edits folded in. */
  objects: ReviewObject[];
  rooms: ReviewRoom[];
  vocabulary: ReviewPayload["vocabulary"];
  editedIds: Set<string>;
  removedIds: Set<string>;
  /** Apply a patch to every selected object as one undoable step. */
  onPatch: (patch: ObjectOverride) => void;
  onRevert: () => void;
  onToggleRemoved: () => void;
  onClose: () => void;
}

/**
 * Property editing for the current selection.
 *
 * Works on one object or many: a field shows a value when the selection agrees
 * on it and "mixed" when it does not, and editing writes to all of them. That
 * keeps one panel for both cases instead of a separate multi-select UI that
 * would inevitably support a different subset of the operations.
 */
export function Inspector({
  objects,
  rooms,
  vocabulary,
  editedIds,
  removedIds,
  onPatch,
  onRevert,
  onToggleRemoved,
  onClose,
}: InspectorProps) {
  const [tab, setTab] = useState<Tab>("placement");

  const many = objects.length > 1;
  const first = objects[0];
  const locked = objects.every((object) => object.locked);
  const anyRemoved = objects.some((object) => removedIds.has(object.id));
  const anyEdited = objects.some((object) => editedIds.has(object.id));

  /** A value when every selected object agrees, otherwise null. */
  const shared = <T,>(read: (object: ReviewObject) => T): T | null => {
    const value = read(objects[0]);
    return objects.every((object) => read(object) === value) ? value : null;
  };

  const category = shared((object) => object.category);
  const roomId = shared((object) => object.room_id);

  return (
    <aside className="flex h-fit flex-col rounded-xl border border-line bg-surface lg:sticky lg:top-4">
      <header className="flex items-start gap-2 border-b border-line-subtle p-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-primary">
            {many
              ? `${objects.length} objects selected`
              : formatCategory(first.category)}
          </p>
          <p className="truncate font-mono text-[10px] text-tertiary">
            {many ? "editing all together" : first.id}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          title="Clear selection"
          className="rounded-md px-1.5 text-lg leading-none text-tertiary hover:text-primary"
        >
          ×
        </button>
      </header>

      <nav className="flex gap-1 border-b border-line-subtle p-1.5">
        {(["placement", "asset", "material"] as const).map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setTab(value)}
            className={`flex-1 rounded-md px-2 py-1 text-[11px] capitalize transition-colors ${
              tab === value
                ? "bg-surface-active text-primary"
                : "text-tertiary hover:text-secondary"
            }`}
          >
            {value}
          </button>
        ))}
      </nav>

      <div className="space-y-3 p-3">
        {!many && first.support === "on_object" && (
          <p className="rounded-lg border border-accent-border bg-accent-surface px-2.5 py-2 text-[11px] leading-relaxed text-accent-text/80">
            Rests on {first.support_id}. Moving that object carries this one with it.
          </p>
        )}

        {tab === "placement" && (
          <>
            <Field label="Category">
              <select
                value={category ?? ""}
                disabled={anyRemoved}
                onChange={(event) => onPatch({ category: event.target.value })}
                className={selectClass}
              >
                {category === null && <option value="">— mixed —</option>}
                {vocabulary.categories.map(({ category: name }) => (
                  <option key={name} value={name}>
                    {formatCategory(name)}
                  </option>
                ))}
              </select>
            </Field>

            <Field label="Room">
              <select
                value={roomId ?? ""}
                disabled={anyRemoved}
                onChange={(event) => onPatch({ room_id: event.target.value })}
                className={selectClass}
              >
                {roomId === null && <option value="">— mixed —</option>}
                {rooms.map((room) => (
                  <option key={room.id} value={room.id}>
                    {formatRoomType(room.room_type)}
                  </option>
                ))}
              </select>
            </Field>

            {!many && (
              <div className="grid grid-cols-2 gap-2">
                <NumberField
                  label="X"
                  value={first.position.x}
                  disabled={locked || anyRemoved}
                  onChange={(x) => onPatch({ position: { x, y: first.position.y } })}
                />
                <NumberField
                  label="Y"
                  value={first.position.y}
                  disabled={locked || anyRemoved}
                  onChange={(y) => onPatch({ position: { x: first.position.x, y } })}
                />
              </div>
            )}

            <NumberField
              label="Rotation"
              unit="°"
              step={5}
              value={shared((object) => object.rotation_z) ?? 0}
              mixed={shared((object) => object.rotation_z) === null}
              disabled={locked || anyRemoved}
              onChange={(value) =>
                onPatch({ rotation_z: ((value % 360) + 360) % 360 })
              }
            />

            <div className="grid grid-cols-3 gap-2">
              {(["width", "depth", "height"] as const).map((axis) => {
                const value = shared((object) => object.dimensions[axis]);
                return (
                  <NumberField
                    key={axis}
                    label={axis[0].toUpperCase() + axis.slice(1)}
                    value={value ?? 0}
                    mixed={value === null}
                    min={MIN_DIMENSION}
                    max={MAX_DIMENSION}
                    disabled={locked || anyRemoved}
                    onChange={(next) => onPatch({ dimensions: { [axis]: next } })}
                  />
                );
              })}
            </div>
          </>
        )}

        {tab === "asset" && (
          <AssetBrowser
            category={category}
            current={shared((object) => object.asset)}
            vocabulary={vocabulary}
            disabled={anyRemoved}
            onPick={(asset) => onPatch({ asset })}
          />
        )}

        {tab === "material" && (
          <MaterialPicker
            surface="object"
            material={shared((object) => object.material)}
            colour={shared((object) => object.color_hex)}
            vocabulary={vocabulary}
            disabled={anyRemoved}
            onMaterial={(material) => onPatch({ material })}
            onColour={(color_hex) => onPatch({ color_hex })}
          />
        )}

        <div className="flex flex-wrap gap-2 border-t border-line-subtle pt-3">
          <button
            type="button"
            onClick={() => onPatch({ locked: !locked })}
            disabled={anyRemoved}
            className={buttonClass}
          >
            {locked ? <LockIcon className="h-3.5 w-3.5" /> : <UnlockIcon className="h-3.5 w-3.5" />}
            {locked ? "Locked" : "Lock"}
          </button>

          <button
            type="button"
            onClick={onRevert}
            disabled={!anyEdited}
            title="Discard pending edits for the selection"
            className={buttonClass}
          >
            <ResetIcon className="h-3.5 w-3.5" />
            Revert
          </button>

          <button
            type="button"
            onClick={onToggleRemoved}
            className={`${buttonClass} ml-auto hover:border-danger-border/40 hover:text-danger-text`}
          >
            {anyRemoved ? <UndoIcon className="h-3.5 w-3.5" /> : <TrashIcon className="h-3.5 w-3.5" />}
            {anyRemoved ? "Restore" : "Remove"}
          </button>
        </div>

        {locked && (
          <p className="text-[11px] leading-relaxed text-tertiary">
            Locked objects are never moved by automatic correction. Unlock to
            reposition.
          </p>
        )}

        {!many && (
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1 border-t border-line-subtle pt-3 text-[11px]">
            <dt className="text-tertiary">Confidence</dt>
            <dd className="text-right font-mono text-secondary">
              {first.confidence.toFixed(2)}
            </dd>
            <dt className="text-tertiary">Seen in</dt>
            <dd className="text-right font-mono text-secondary">
              {first.observation_count} image{first.observation_count === 1 ? "" : "s"}
            </dd>
          </dl>
        )}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Asset browser
// ---------------------------------------------------------------------------

/**
 * The procedural variants a category can resolve to.
 *
 * Swapping is placement-preserving by design — the server applies the asset
 * without touching position — so this is purely a question of what the object
 * looks like, and the list is filtered to the selected category so a sofa
 * cannot be turned into a bed by accident.
 */
function AssetBrowser({
  category,
  current,
  vocabulary,
  disabled,
  onPick,
}: {
  category: string | null;
  current: string | null;
  vocabulary: ReviewPayload["vocabulary"];
  disabled: boolean;
  onPick: (key: string) => void;
}) {
  const [query, setQuery] = useState("");

  const variants = useMemo(() => {
    const scoped = category
      ? vocabulary.assets.filter((asset) => asset.category === category)
      : vocabulary.assets;
    const needle = query.trim().toLowerCase();
    if (!needle) return scoped;
    return scoped.filter(
      (asset) =>
        asset.key.toLowerCase().includes(needle) ||
        asset.styles.some((style) => style.includes(needle)) ||
        asset.materials.some((material) => material.includes(needle)),
    );
  }, [vocabulary.assets, category, query]);

  if (category === null) {
    return (
      <p className="py-4 text-center text-[11px] text-tertiary">
        Select objects of a single category to swap their asset.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <input
        type="search"
        value={query}
        placeholder={`Search ${formatCategory(category)} variants…`}
        onChange={(event) => setQuery(event.target.value)}
        className="w-full rounded-md border border-line bg-raised px-2 py-1.5 text-xs text-primary focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none"
      />

      {variants.length === 0 ? (
        <p className="py-4 text-center text-[11px] text-tertiary">
          No variants match.
        </p>
      ) : (
        <ul className="max-h-64 space-y-1 overflow-y-auto">
          {variants.map((asset) => {
            const active = asset.key === current;
            return (
              <li key={asset.key}>
                <button
                  type="button"
                  disabled={disabled}
                  onClick={() => onPick(asset.key)}
                  className={`w-full rounded-lg border px-2.5 py-2 text-left transition-colors disabled:opacity-40 ${
                    active
                      ? "border-accent-border bg-accent-surface"
                      : "border-line-subtle hover:border-line-strong"
                  }`}
                >
                  <span className="block truncate font-mono text-[11px] text-primary">
                    {asset.key}
                  </span>
                  <span className="mt-0.5 block truncate text-[10px] text-tertiary">
                    {asset.styles.slice(0, 3).join(" · ") || "any style"}
                    {asset.materials.length > 0 && ` — ${asset.materials.join(", ")}`}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Material picker
// ---------------------------------------------------------------------------

/**
 * Material and colour for a surface or an object.
 *
 * The material list is filtered by what each material can be applied to, so a
 * floor cannot be set to "fabric". Picking a material also suggests its
 * characteristic colour, which the user can then override — most of the time
 * the default is what they wanted, and the swatch makes the exception easy.
 */
export function MaterialPicker({
  surface,
  material,
  colour,
  vocabulary,
  disabled,
  onMaterial,
  onColour,
}: {
  surface: "wall" | "floor" | "ceiling" | "object";
  material: string | null;
  colour: string | null;
  vocabulary: ReviewPayload["vocabulary"];
  disabled?: boolean;
  onMaterial: (material: string) => void;
  onColour: (colour: string) => void;
}) {
  const applicable = useMemo(
    () => vocabulary.materials.filter((entry) => entry.applies_to.includes(surface)),
    [vocabulary.materials, surface],
  );
  const options = applicable.length > 0 ? applicable : vocabulary.materials;

  return (
    <div className="space-y-2.5">
      <Field label="Material">
        <select
          value={material ?? ""}
          disabled={disabled}
          onChange={(event) => onMaterial(event.target.value)}
          className={selectClass}
        >
          {material === null && <option value="">— mixed —</option>}
          {options.map((entry) => (
            <option key={entry.material} value={entry.material}>
              {entry.material.replace(/_/g, " ")}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Colour">
        <div className="flex items-center gap-2">
          <input
            type="color"
            value={colour ?? "#CCCCCC"}
            disabled={disabled}
            onChange={(event) => onColour(event.target.value.toUpperCase())}
            className="h-8 w-10 shrink-0 cursor-pointer rounded-md border border-line bg-raised disabled:opacity-40"
          />
          <input
            type="text"
            value={colour ?? ""}
            placeholder="— mixed —"
            disabled={disabled}
            onChange={(event) => {
              const value = event.target.value.trim();
              if (/^#[0-9a-fA-F]{6}$/.test(value)) onColour(value.toUpperCase());
            }}
            className="w-full rounded-md border border-line bg-raised px-2 py-1.5 font-mono text-xs text-primary focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none disabled:opacity-40"
          />
        </div>
      </Field>

      <div className="flex flex-wrap gap-1.5">
        {options.slice(0, 12).map((entry) => (
          <button
            key={entry.material}
            type="button"
            disabled={disabled}
            title={`${entry.material.replace(/_/g, " ")} — ${entry.color_hex}`}
            onClick={() => {
              onMaterial(entry.material);
              onColour(entry.color_hex);
            }}
            className="h-6 w-6 rounded-md border border-white/15 transition-transform hover:scale-110 disabled:opacity-40"
            style={{ backgroundColor: entry.color_hex }}
          >
            <span className="sr-only">{entry.material}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared field primitives
// ---------------------------------------------------------------------------

export const selectClass =
  "w-full rounded-md border border-line bg-raised px-2 py-1.5 text-xs text-primary focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none disabled:opacity-40";

const buttonClass =
  "flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-xs text-secondary transition-colors hover:border-line-strong hover:text-primary disabled:opacity-30";

export function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] tracking-widest text-tertiary uppercase">
        {label}
      </span>
      {children}
    </label>
  );
}

/**
 * A numeric field that only reports committed values.
 *
 * It keeps what the user is typing in local state so an intermediate "2." or
 * "-" is not parsed into a placement, and pushes the value up on blur or
 * Enter. `mixed` renders an empty field for a selection that disagrees, so
 * typing sets every object rather than showing one object's value as if it
 * were shared.
 */
export function NumberField({
  label,
  value,
  unit = "m",
  step = 0.05,
  min,
  max,
  mixed,
  disabled,
  onChange,
}: {
  label: string;
  value: number;
  unit?: string;
  step?: number;
  min?: number;
  max?: number;
  mixed?: boolean;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const display = text ?? (mixed ? "" : value.toFixed(2));

  const commit = () => {
    if (text === null) return;
    const parsed = Number.parseFloat(text);
    setText(null);
    if (!Number.isFinite(parsed)) return;
    let next = parsed;
    if (min !== undefined) next = Math.max(min, next);
    if (max !== undefined) next = Math.min(max, next);
    if (mixed || next !== value) onChange(next);
  };

  return (
    <label className="block">
      <span className="mb-1 block text-[10px] tracking-widest text-tertiary uppercase">
        {label} <span className="normal-case">({unit})</span>
      </span>
      <input
        type="number"
        step={step}
        value={display}
        placeholder={mixed ? "mixed" : undefined}
        disabled={disabled}
        onChange={(event) => setText(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            event.currentTarget.blur();
          }
        }}
        className="w-full rounded-md border border-line bg-raised px-2 py-1.5 font-mono text-xs text-primary focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none disabled:opacity-40"
      />
    </label>
  );
}
