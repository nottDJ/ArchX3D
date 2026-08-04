"use client";

import { useCallback, useMemo, useRef, useState } from "react";

import {
  MAX_DIMENSION,
  MIN_DIMENSION,
  clampToPolygon,
  formatRoomType,
  type ObjectOverride,
  type ReviewObject,
  type ReviewRoom,
} from "@/lib/wizard";
import type { ResolvedObject } from "@/lib/editor";
import {
  normaliseDegrees,
  snapPosition,
  type SnapIndicator,
  type SnapSettings,
} from "@/lib/snapping";

export interface PlanMapProps {
  rooms: ReviewRoom[];
  /** Every object with pending edits folded in, keyed by id. */
  resolved: Map<string, ResolvedObject>;
  selection: string[];
  onSelect: (ids: string[], mode?: "replace" | "toggle" | "add") => void;
  /** Commit a batch of transforms as one undoable step. */
  onTransform: (patches: Record<string, ObjectOverride>) => void;
  selectedRoomId: string | null;
  onSelectRoom: (roomId: string | null) => void;
  snap: SnapSettings;
  highlightedObjectId?: string | null;
}

const PADDING = 0.6; // metres of margin around the plan

/** Rotation detents, in degrees. Hold Alt to rotate freely. */
const ROTATION_SNAP = 15;

type DragMode = "move" | "rotate" | "resize" | "marquee";

interface DragState {
  mode: DragMode;
  /** The object under the pointer; absent for a marquee. */
  primaryId?: string;
  /** Pointer-to-centre offset at grab time, so the object does not jump. */
  offsetX: number;
  offsetY: number;
  /** Positions of everything being dragged, captured at grab time. */
  starts: Record<string, { x: number; y: number }>;
  /** Marquee anchor, in plan metres. */
  origin?: { x: number; y: number };
}

/**
 * Top-down plan of the reconstruction: room polygons with every object drawn
 * at its true oriented footprint, directly editable.
 *
 * Interaction model, which follows CAD convention rather than inventing one:
 *
 * * click selects, shift-click extends, dragging empty space marquee-selects
 * * dragging any selected object moves the whole selection together
 * * a single selection also gets a rotation arm and four resize handles
 * * Alt suspends snapping, for when the user means an odd number
 *
 * Drag state is local and committed on release, so the parent — and the undo
 * history — see one entry per gesture rather than one per pointer event.
 */
export function PlanMap({
  rooms,
  resolved,
  selection,
  onSelect,
  onTransform,
  selectedRoomId,
  onSelectRoom,
  snap,
  highlightedObjectId,
}: PlanMapProps) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const [draft, setDraft] = useState<Record<string, ObjectOverride>>({});
  const [indicators, setIndicators] = useState<SnapIndicator[]>([]);
  const [marquee, setMarquee] = useState<null | {
    x0: number; y0: number; x1: number; y1: number;
  }>(null);

  const selected = useMemo(() => new Set(selection), [selection]);

  const bounds = useMemo(() => {
    const xs: number[] = [];
    const ys: number[] = [];
    for (const room of rooms) {
      xs.push(room.bounds_min[0], room.bounds_max[0]);
      ys.push(room.bounds_min[1], room.bounds_max[1]);
    }
    if (xs.length === 0) return { minX: 0, minY: 0, width: 10, height: 8 };
    const minX = Math.min(...xs) - PADDING;
    const minY = Math.min(...ys) - PADDING;
    return {
      minX,
      minY,
      width: Math.max(1, Math.max(...xs) + PADDING - minX),
      height: Math.max(1, Math.max(...ys) + PADDING - minY),
    };
  }, [rooms]);

  // SVG y grows downward while plan y grows upward, so the whole scene is
  // flipped once here rather than negating every coordinate individually.
  // The map is its own inverse, so it converts pointer coordinates back too.
  const flipY = useCallback(
    (y: number) => bounds.minY + bounds.height - (y - bounds.minY),
    [bounds],
  );

  /** Objects as currently drawn: resolved edits plus the in-flight drag. */
  const drawn = useMemo(() => {
    const out: Array<{ object: ReviewObject; room: ReviewRoom; removed: boolean }> = [];
    for (const entry of resolved.values()) {
      const patch = draft[entry.object.id];
      out.push({
        object: patch ? applyPatch(entry.object, patch) : entry.object,
        room: entry.room,
        removed: entry.removed,
      });
    }
    return out;
  }, [resolved, draft]);

  const toPlan = useCallback(
    (event: { clientX: number; clientY: number }) => {
      const svg = svgRef.current;
      if (!svg) return null;
      const matrix = svg.getScreenCTM();
      if (!matrix) return null;
      const point = svg.createSVGPoint();
      point.x = event.clientX;
      point.y = event.clientY;
      const local = point.matrixTransform(matrix.inverse());
      return { x: local.x, y: flipY(local.y) };
    },
    [flipY],
  );

  // -------------------------------------------------------------------------
  // Gesture start
  // -------------------------------------------------------------------------

  const beginObjectDrag = (
    event: React.PointerEvent,
    objectId: string,
    mode: Exclude<DragMode, "marquee">,
  ) => {
    const entry = resolved.get(objectId);
    if (!entry || entry.object.locked || entry.removed) return;

    event.stopPropagation();
    const plan = toPlan(event);
    if (!plan) return;

    // Clicking an unselected object selects it first, so a drag always moves
    // what the user is pointing at rather than a stale selection elsewhere.
    let group = selection;
    if (!selected.has(objectId)) {
      const selectMode = event.shiftKey ? "add" : "replace";
      group = selectMode === "add" ? [...selection, objectId] : [objectId];
      onSelect([objectId], selectMode);
    }

    // Rotation and resize act on one object even inside a multi-selection:
    // a shared pivot is rarely what is wanted and never what is expected.
    const movers = mode === "move" ? group : [objectId];
    const starts: Record<string, { x: number; y: number }> = {};
    for (const id of movers) {
      const target = resolved.get(id);
      if (target && !target.object.locked && !target.removed) {
        starts[id] = { x: target.object.position.x, y: target.object.position.y };
      }
    }

    dragRef.current = {
      mode,
      primaryId: objectId,
      offsetX: entry.object.position.x - plan.x,
      offsetY: entry.object.position.y - plan.y,
      starts,
    };
    (event.target as Element).setPointerCapture?.(event.pointerId);
  };

  const beginMarquee = (event: React.PointerEvent) => {
    const plan = toPlan(event);
    if (!plan) return;
    dragRef.current = {
      mode: "marquee",
      offsetX: 0,
      offsetY: 0,
      starts: {},
      origin: plan,
    };
    if (!event.shiftKey) onSelect([], "replace");
    (event.target as Element).setPointerCapture?.(event.pointerId);
  };

  // -------------------------------------------------------------------------
  // Gesture update
  // -------------------------------------------------------------------------

  const handlePointerMove = (event: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    const plan = toPlan(event);
    if (!plan) return;

    if (drag.mode === "marquee") {
      const origin = drag.origin!;
      setMarquee({ x0: origin.x, y0: origin.y, x1: plan.x, y1: plan.y });
      return;
    }

    const entry = resolved.get(drag.primaryId!);
    if (!entry) return;
    const { object, room } = entry;
    const free = event.altKey;

    if (drag.mode === "move") {
      const start = drag.starts[drag.primaryId!];
      if (!start) return;

      let target = { x: plan.x + drag.offsetX, y: plan.y + drag.offsetY };

      // Snapping is computed for the grabbed object only; the rest of the
      // selection follows the same delta so the arrangement is preserved.
      let hints: SnapIndicator[] = [];
      if (!free) {
        const neighbours = drawn
          .filter((d) => d.room.id === room.id && !drag.starts[d.object.id] && !d.removed)
          .map((d) => d.object);
        const result = snapPosition(target, object, room, neighbours, snap);
        target = result.position;
        hints = result.indicators;
      }
      if (object.support === "floor") target = clampToPolygon(target, room.polygon);

      const delta = { x: target.x - start.x, y: target.y - start.y };
      const patches: Record<string, ObjectOverride> = {};
      for (const [id, from] of Object.entries(drag.starts)) {
        const moved = { x: from.x + delta.x, y: from.y + delta.y };
        const target_ = resolved.get(id);
        patches[id] = {
          position:
            target_ && target_.object.support === "floor"
              ? clampToPolygon(moved, target_.room.polygon)
              : moved,
        };
      }
      setDraft(patches);
      setIndicators(hints);
      return;
    }

    if (drag.mode === "rotate") {
      const degrees =
        (Math.atan2(plan.y - object.position.y, plan.x - object.position.x) * 180) /
        Math.PI;
      // The handle tracks the object's front, which points along local +Y, so
      // the pointer angle leads the stored rotation by a quarter turn.
      const raw = degrees - 90;
      const snapped =
        free || !snap.enabled ? raw : Math.round(raw / ROTATION_SNAP) * ROTATION_SNAP;
      setDraft({ [drag.primaryId!]: { rotation_z: normaliseDegrees(snapped) } });
      return;
    }

    // Resize: measure the pointer in the object's own frame and mirror it
    // across the centre, so the object grows symmetrically and stays put.
    const theta = (-object.rotation_z * Math.PI) / 180;
    const dx = plan.x - object.position.x;
    const dy = plan.y - object.position.y;
    const localX = dx * Math.cos(theta) - dy * Math.sin(theta);
    const localY = dx * Math.sin(theta) + dy * Math.cos(theta);

    const clamp = (value: number) =>
      Math.min(MAX_DIMENSION, Math.max(MIN_DIMENSION, value));
    const quantise = (value: number) =>
      free || !snap.grid ? value : Math.round(value / snap.grid) * snap.grid;

    setDraft({
      [drag.primaryId!]: {
        dimensions: {
          width: clamp(quantise(Math.abs(localX) * 2)),
          depth: clamp(quantise(Math.abs(localY) * 2)),
        },
      },
    });
  };

  // -------------------------------------------------------------------------
  // Gesture end
  // -------------------------------------------------------------------------

  const endDrag = (event: React.PointerEvent) => {
    const drag = dragRef.current;
    dragRef.current = null;
    (event.target as Element).releasePointerCapture?.(event.pointerId);

    if (drag?.mode === "marquee") {
      if (marquee) {
        const x0 = Math.min(marquee.x0, marquee.x1);
        const x1 = Math.max(marquee.x0, marquee.x1);
        const y0 = Math.min(marquee.y0, marquee.y1);
        const y1 = Math.max(marquee.y0, marquee.y1);
        // Centre-inside rather than fully-enclosed: with furniture-sized boxes
        // in a room-sized view, requiring full enclosure makes the marquee
        // almost unusable.
        const hits = drawn
          .filter(
            (d) =>
              !d.removed &&
              d.object.position.x >= x0 && d.object.position.x <= x1 &&
              d.object.position.y >= y0 && d.object.position.y <= y1,
          )
          .map((d) => d.object.id);
        if (hits.length) onSelect(hits, event.shiftKey ? "add" : "replace");
      }
      setMarquee(null);
      return;
    }

    if (Object.keys(draft).length) onTransform(draft);
    setDraft({});
    setIndicators([]);
  };

  if (rooms.length === 0) {
    return (
      <div className="rounded-xl border border-line-subtle bg-surface p-8 text-center text-sm text-tertiary">
        No rooms were detected in the floor plan.
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-line-subtle bg-sunken">
      <svg
        ref={svgRef}
        viewBox={`${bounds.minX} ${bounds.minY} ${bounds.width} ${bounds.height}`}
        className="block h-auto w-full touch-none"
        style={{ aspectRatio: `${bounds.width} / ${bounds.height}` }}
        role="img"
        aria-label="Editable plan view of the detected rooms and furniture"
        onPointerMove={handlePointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <defs>
          <pattern id="plan-grid" width="1" height="1" patternUnits="userSpaceOnUse">
            <path
              d="M 1 0 L 0 0 0 1"
              fill="none"
              stroke="rgb(255 255 255 / 0.045)"
              strokeWidth={0.01}
            />
          </pattern>
        </defs>
        <rect
          x={bounds.minX}
          y={bounds.minY}
          width={bounds.width}
          height={bounds.height}
          fill="url(#plan-grid)"
          onPointerDown={beginMarquee}
        />

        {rooms.map((room) => {
          const isSelected = room.id === selectedRoomId;
          const points = room.polygon.map(([x, y]) => `${x},${flipY(y)}`).join(" ");

          return (
            <g key={room.id}>
              <polygon
                points={points}
                onClick={() => onSelectRoom(isSelected ? null : room.id)}
                onPointerDown={beginMarquee}
                className="cursor-pointer transition-[fill,stroke] duration-200"
                fill={
                  isSelected
                    ? "rgb(56 132 255 / 0.18)"
                    : room.has_imagery
                      ? "rgb(255 255 255 / 0.05)"
                      : "rgb(255 255 255 / 0.02)"
                }
                stroke={
                  isSelected
                    ? "rgb(125 190 255 / 0.9)"
                    : room.has_imagery
                      ? "rgb(255 255 255 / 0.28)"
                      : "rgb(255 255 255 / 0.12)"
                }
                strokeWidth={isSelected ? 0.07 : 0.04}
                strokeDasharray={room.has_imagery ? undefined : "0.18 0.14"}
              />

              <text
                x={(room.bounds_min[0] + room.bounds_max[0]) / 2}
                y={flipY((room.bounds_min[1] + room.bounds_max[1]) / 2)}
                textAnchor="middle"
                className="pointer-events-none select-none"
                fill={room.has_imagery ? "rgb(255 255 255 / 0.55)" : "rgb(255 255 255 / 0.28)"}
                fontSize={Math.min(0.42, room.width / 9)}
              >
                {formatRoomType(room.room_type)}
              </text>
              <text
                x={(room.bounds_min[0] + room.bounds_max[0]) / 2}
                y={flipY((room.bounds_min[1] + room.bounds_max[1]) / 2) + 0.4}
                textAnchor="middle"
                className="pointer-events-none select-none"
                fill="rgb(255 255 255 / 0.3)"
                fontSize={Math.min(0.3, room.width / 13)}
              >
                {room.area.toFixed(0)} m² · {room.object_count} items
              </text>
            </g>
          );
        })}

        {/* Objects are drawn after every room so one room's fill never covers
            another's furniture, and the selection always sits on top. */}
        {drawn.map(({ object, room, removed }) => (
          <ObjectFootprint
            key={object.id}
            object={object}
            flipY={flipY}
            removed={removed}
            selected={selected.has(object.id)}
            solo={selection.length === 1 && selected.has(object.id)}
            highlighted={object.id === highlightedObjectId}
            dimmed={selectedRoomId !== null && room.id !== selectedRoomId}
            onBeginDrag={beginObjectDrag}
            onToggle={(event) =>
              onSelect([object.id], event.shiftKey ? "toggle" : "replace")
            }
          />
        ))}

        {indicators.map((indicator, index) => (
          <SnapGuide key={`${indicator.kind}-${index}`} indicator={indicator} flipY={flipY} />
        ))}

        {marquee && (
          <rect
            x={Math.min(marquee.x0, marquee.x1)}
            y={flipY(Math.max(marquee.y0, marquee.y1))}
            width={Math.abs(marquee.x1 - marquee.x0)}
            height={Math.abs(marquee.y1 - marquee.y0)}
            fill="rgb(56 189 248 / 0.10)"
            stroke="rgb(125 211 252 / 0.8)"
            strokeWidth={0.02}
            strokeDasharray="0.1 0.06"
            className="pointer-events-none"
          />
        )}
      </svg>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line-subtle px-4 py-2 text-[10px] text-tertiary">
        <Legend swatch="bg-accent-solid/70" label="Confident" />
        <Legend swatch="bg-warning-surface" label="Uncertain" />
        <Legend swatch="bg-danger-solid/60" label="Marked for removal" />
        <Legend swatch="border border-dashed border-line-strong" label="No reference image" />
        <span className="ml-auto text-tertiary">
          {selection.length > 1
            ? `${selection.length} selected — drag to move together`
            : "Drag to move · shift-click to add · drag empty space to marquee · Alt suspends snapping"}
        </span>
      </div>
    </div>
  );
}

/** Apply an uncommitted patch for drawing, without touching the source. */
function applyPatch(object: ReviewObject, patch: ObjectOverride): ReviewObject {
  return {
    ...object,
    position: patch.position
      ? { ...object.position, x: patch.position.x, y: patch.position.y }
      : object.position,
    rotation_z: patch.rotation_z ?? object.rotation_z,
    dimensions: patch.dimensions
      ? { ...object.dimensions, ...patch.dimensions }
      : object.dimensions,
  };
}

function SnapGuide({
  indicator,
  flipY,
}: {
  indicator: SnapIndicator;
  flipY: (y: number) => number;
}) {
  const colour =
    indicator.kind === "wall" || indicator.kind === "corner"
      ? "rgb(244 114 182 / 0.9)"
      : indicator.kind === "grid"
        ? "rgb(255 255 255 / 0.25)"
        : "rgb(45 212 191 / 0.9)";

  const isVertical = indicator.axis === "x";
  const x1 = isVertical ? indicator.at : indicator.from;
  const x2 = isVertical ? indicator.at : indicator.to;
  const y1 = flipY(isVertical ? indicator.from : indicator.at);
  const y2 = flipY(isVertical ? indicator.to : indicator.at);

  return (
    <g className="pointer-events-none">
      <line
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        stroke={colour}
        strokeWidth={0.018}
        strokeDasharray="0.12 0.08"
      />
      <title>{indicator.label}</title>
    </g>
  );
}

function Legend({ swatch, label }: { swatch: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`h-2 w-2.5 rounded-[2px] ${swatch}`} />
      {label}
    </span>
  );
}

function ObjectFootprint({
  object,
  flipY,
  removed,
  selected,
  solo,
  highlighted,
  dimmed,
  onBeginDrag,
  onToggle,
}: {
  object: ReviewObject;
  flipY: (y: number) => number;
  removed: boolean;
  selected: boolean;
  solo: boolean;
  highlighted: boolean;
  dimmed: boolean;
  onBeginDrag: (
    event: React.PointerEvent,
    objectId: string,
    mode: "move" | "rotate" | "resize",
  ) => void;
  onToggle: (event: React.PointerEvent) => void;
}) {
  const { width, depth } = object.dimensions;
  const cx = object.position.x;
  const cy = flipY(object.position.y);
  const halfWidth = Math.max(width, 0.08) / 2;
  const halfDepth = Math.max(depth, 0.08) / 2;

  // Plan y is flipped for display, so the rotation must flip with it.
  const rotation = -object.rotation_z;
  const editable = !object.locked && !removed;

  const fill = removed
    ? "rgb(244 63 94 / 0.20)"
    : object.locked
      ? "rgb(148 163 184 / 0.22)"
      : object.uncertain
        ? "rgb(251 191 36 / 0.22)"
        : "rgb(56 189 248 / 0.28)";
  const stroke = removed
    ? "rgb(244 63 94 / 0.75)"
    : object.locked
      ? "rgb(203 213 225 / 0.8)"
      : object.uncertain
        ? "rgb(251 191 36 / 0.7)"
        : "rgb(125 211 252 / 0.75)";

  // Scale the handles to the object so a small stool stays grabbable and a
  // large sectional does not get absurd furniture-sized controls.
  const handle = Math.min(0.14, Math.max(0.07, Math.min(halfWidth, halfDepth) * 0.5));
  const armLength = halfDepth + handle * 2.2;

  return (
    <g
      transform={`rotate(${rotation} ${cx} ${cy})`}
      opacity={dimmed ? 0.3 : 1}
      className="transition-opacity duration-200"
    >
      <rect
        x={cx - halfWidth}
        y={cy - halfDepth}
        width={halfWidth * 2}
        height={halfDepth * 2}
        rx={0.05}
        fill={fill}
        stroke={selected ? "rgb(255 255 255 / 0.95)" : stroke}
        strokeWidth={selected ? 0.05 : highlighted ? 0.055 : 0.025}
        className={editable ? "cursor-move" : "cursor-pointer"}
        onPointerDown={(event) =>
          editable ? onBeginDrag(event, object.id, "move") : onToggle(event)
        }
      />

      {/* A short tick marks the object's front, so orientation is visible. */}
      {!removed && depth > 0.25 && (
        <line
          x1={cx}
          y1={cy}
          x2={cx}
          y2={cy - halfDepth}
          stroke={stroke}
          strokeWidth={0.02}
          className="pointer-events-none"
        />
      )}

      {object.locked && (
        <LockGlyph cx={cx} cy={cy} size={Math.min(halfWidth, halfDepth) * 0.9} />
      )}

      {/* Rotation and resize handles belong to a single selection: with several
          objects chosen, the meaningful operations are the batch ones in the
          toolbar, and per-object handles would just be in the way. */}
      {solo && editable && (
        <>
          <line
            x1={cx}
            y1={cy - halfDepth}
            x2={cx}
            y2={cy - armLength}
            stroke="rgb(255 255 255 / 0.5)"
            strokeWidth={0.018}
            className="pointer-events-none"
          />
          <circle
            cx={cx}
            cy={cy - armLength}
            r={handle * 0.75}
            fill="rgb(15 23 42 / 0.95)"
            stroke="rgb(255 255 255 / 0.95)"
            strokeWidth={0.022}
            className="cursor-grab"
            onPointerDown={(event) => onBeginDrag(event, object.id, "rotate")}
          />

          {[
            [-1, -1],
            [1, -1],
            [1, 1],
            [-1, 1],
          ].map(([sx, sy]) => (
            <rect
              key={`${sx},${sy}`}
              x={cx + sx * halfWidth - handle / 2}
              y={cy + sy * halfDepth - handle / 2}
              width={handle}
              height={handle}
              rx={handle * 0.25}
              fill="rgb(15 23 42 / 0.95)"
              stroke="rgb(255 255 255 / 0.95)"
              strokeWidth={0.022}
              className="cursor-nwse-resize"
              onPointerDown={(event) => onBeginDrag(event, object.id, "resize")}
            />
          ))}
        </>
      )}

      <title>
        {`${object.category} — confidence ${object.confidence.toFixed(2)}, ` +
          `${width.toFixed(2)}×${depth.toFixed(2)} m, ${object.rotation_z.toFixed(0)}°` +
          (object.locked ? " (locked)" : "")}
      </title>
    </g>
  );
}

/** A padlock drawn in plan units, marking an object the user has pinned. */
function LockGlyph({ cx, cy, size }: { cx: number; cy: number; size: number }) {
  const width = Math.min(0.22, Math.max(0.09, size));
  const height = width * 0.72;
  const x = cx - width / 2;
  const y = cy - height / 2 + height * 0.18;

  return (
    <g className="pointer-events-none" opacity={0.85}>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        rx={height * 0.22}
        fill="rgb(226 232 240 / 0.95)"
      />
      <path
        d={`M ${cx - width * 0.28} ${y} v ${-height * 0.42} a ${width * 0.28} ${
          width * 0.28
        } 0 0 1 ${width * 0.56} 0 v ${height * 0.42}`}
        fill="none"
        stroke="rgb(226 232 240 / 0.95)"
        strokeWidth={width * 0.14}
      />
    </g>
  );
}
