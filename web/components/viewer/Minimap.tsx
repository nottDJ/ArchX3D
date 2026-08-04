"use client";

/**
 * ArchX3D — Plan minimap
 * ======================
 * A small orthographic plan of the building with the camera's position and
 * heading on it.
 *
 * Why SVG and not a second WebGL view
 * -----------------------------------
 * A minimap rendered with a second camera means a second render pass over the
 * whole scene every frame, for a 180-pixel square. The room manifest already
 * carries exactly what a plan needs — polygons and bounds, in metres — so the
 * map is drawn from data rather than from geometry, at a cost of one SVG
 * re-render when the camera moves and nothing at all when it does not.
 *
 * It also degrades correctly: a model with no room metadata has no plan to
 * draw, and this component renders nothing rather than an empty box.
 *
 * The camera marker updates at 15 Hz
 * ----------------------------------
 * Fast enough to read as live, slow enough that walking does not cause sixty
 * React renders a second for a decorative widget. The position comes in as a
 * prop from a `useFrame` sampler that is already throttled.
 *
 * Coordinates
 * -----------
 * The plan is in Blender plan metres, so the SVG's `viewBox` is metres and the
 * Y axis is flipped once, in the transform, rather than at every point — a
 * building drawn upside-down is a subtle enough error to survive review.
 */

import { useMemo } from "react";

import { roomsExtent } from "@/lib/viewer/manifest";
import type { RoomInfo } from "@/types/viewer";

export interface MinimapProps {
  readonly rooms: readonly RoomInfo[];
  /** Camera position in plan metres — see `viewerToPlan`. */
  readonly position: readonly [number, number] | null;
  /** Camera heading in radians, matching the walk controller's yaw. */
  readonly heading: number;
  readonly selected: string | null;
  readonly occupied: string | null;
  readonly onSelect: (room: RoomInfo) => void;
}

/** Metres of padding around the plan, so edge rooms are not clipped. */
const MARGIN = 1.5;

export function Minimap({
  rooms,
  position,
  heading,
  selected,
  occupied,
  onSelect,
}: MinimapProps) {
  const extent = useMemo(() => roomsExtent(rooms), [rooms]);

  // No rooms, no plan. The infrastructure stays; the widget does not appear.
  if (!extent || rooms.length === 0) return null;

  const width = extent.max[0] - extent.min[0] + MARGIN * 2;
  const height = extent.max[1] - extent.min[1] + MARGIN * 2;
  const originX = extent.min[0] - MARGIN;
  const originY = extent.min[1] - MARGIN;

  return (
    <div className="pointer-events-auto absolute top-4 right-4 z-20 hidden sm:block">
      <div className="rounded-xl border border-line bg-raised/85 p-2 shadow-2xl shadow-black/50 backdrop-blur-xl">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="h-40 w-40"
          role="img"
          aria-label="Floor plan overview"
        >
          {/*
            One transform carries the whole conversion: shift the plan origin to
            zero, then flip Y so that +Y in plan metres runs up the screen the
            way a floor plan is drawn.
          */}
          <g transform={`translate(${-originX} ${height + originY}) scale(1 -1)`}>
            {rooms.map((room) => {
              const isSelected = selected === room.id;
              const isOccupied = occupied === room.id;

              const points =
                room.polygon.length >= 3
                  ? room.polygon.map(([x, y]) => `${x},${y}`).join(" ")
                  : null;

              const common = {
                className: "cursor-pointer transition-[fill,stroke] duration-150",
                fill: isSelected
                  ? "rgba(56,189,248,0.32)"
                  : isOccupied
                    ? "rgba(52,211,153,0.20)"
                    : "rgba(255,255,255,0.055)",
                stroke: isSelected
                  ? "rgba(125,211,252,0.9)"
                  : isOccupied
                    ? "rgba(110,231,183,0.75)"
                    : "rgba(255,255,255,0.22)",
                // Stroke is in metres because the viewBox is; a fixed pixel
                // width would need vector-effect and would scale unevenly
                // between a bungalow and a tower.
                strokeWidth: Math.max(0.04, Math.min(width, height) * 0.006),
                onClick: () => onSelect(room),
              };

              return points ? (
                <polygon key={room.id} points={points} {...common}>
                  <title>{`${room.name} · ${room.area_m2.toFixed(1)} m²`}</title>
                </polygon>
              ) : (
                // No polygon — fall back to the bounding box, which the
                // manifest guarantees.
                <rect
                  key={room.id}
                  x={room.bounds_min[0]}
                  y={room.bounds_min[1]}
                  width={room.bounds_max[0] - room.bounds_min[0]}
                  height={room.bounds_max[1] - room.bounds_min[1]}
                  {...common}
                >
                  <title>{`${room.name} · ${room.area_m2.toFixed(1)} m²`}</title>
                </rect>
              );
            })}

            {position && (
              <CameraMarker
                x={position[0]}
                y={position[1]}
                heading={heading}
                scale={Math.min(width, height)}
              />
            )}
          </g>
        </svg>
      </div>
    </div>
  );
}

/**
 * The "you are here" marker: a dot with a view cone.
 *
 * The cone matters more than the dot — position alone leaves the user working
 * out which way they are facing from the 3D view, which is exactly the question
 * a minimap exists to answer.
 */
function CameraMarker({
  x,
  y,
  heading,
  scale,
}: {
  x: number;
  y: number;
  heading: number;
  scale: number;
}) {
  const radius = Math.max(0.14, scale * 0.018);
  const reach = radius * 6;
  const spread = 0.42; // radians either side — roughly a 48° field of view

  // Walk-mode yaw is measured about +Y with zero looking down −Z. In plan
  // metres that is −Y, so the cone's centre line is (−sin, −cos) — the same
  // mapping `planToViewer` implies, read backwards.
  const cone = [
    [x, y],
    [
      x - Math.sin(heading - spread) * reach,
      y - Math.cos(heading - spread) * reach,
    ],
    [
      x - Math.sin(heading + spread) * reach,
      y - Math.cos(heading + spread) * reach,
    ],
  ]
    .map(([px, py]) => `${px},${py}`)
    .join(" ");

  return (
    <g>
      <polygon points={cone} fill="rgba(56,189,248,0.28)" />
      <circle cx={x} cy={y} r={radius} fill="rgb(125,211,252)" />
    </g>
  );
}
