"use client";

/**
 * ArchX3D — Room navigation
 * =========================
 * Click a room, fly there.
 *
 * Graceful absence
 * ----------------
 * Room data comes from the scene manifest the generator embeds in the GLB. A
 * model built before the metadata pass, or by another tool, has none — and this
 * component renders nothing at all in that case, rather than an empty list or a
 * "no rooms found" message.
 *
 * That is deliberate. An empty panel invites the user to work out what they did
 * wrong; an absent one simply is not part of the interface for that model. The
 * toolbar button is disabled with a tooltip saying why, which is the one place
 * the explanation belongs.
 *
 * Highlight
 * ---------
 * Selecting a room dims everything outside it rather than hiding it, so the
 * room stays in the context of the building. Walls and floors keep their
 * opacity — dimming the enclosure of the room you are looking into removes the
 * very thing that makes it a room.
 */

import { useMemo } from "react";

import type { RoomInfo } from "@/types/viewer";
import { CloseIcon } from "./icons";

export interface RoomNavigatorProps {
  readonly open: boolean;
  readonly rooms: readonly RoomInfo[];
  readonly selected: string | null;
  /** The room the camera is physically inside, if any. */
  readonly occupied: string | null;
  readonly onSelect: (room: RoomInfo) => void;
  readonly onClear: () => void;
  readonly onClose: () => void;
}

export function RoomNavigator({
  open,
  rooms,
  selected,
  occupied,
  onSelect,
  onClear,
  onClose,
}: RoomNavigatorProps) {
  const totals = useMemo(() => {
    const area = rooms.reduce((sum, room) => sum + room.area_m2, 0);
    const objects = rooms.reduce((sum, room) => sum + room.object_count, 0);
    return { area, objects };
  }, [rooms]);

  // Nothing to navigate: render nothing. See the note above.
  if (!open || rooms.length === 0) return null;

  return (
    <aside
      aria-label="Rooms"
      className="scroll-slim pointer-events-auto absolute top-0 bottom-0 left-0 z-30 w-[min(17rem,100vw)] overflow-y-auto border-r border-line bg-raised/95 backdrop-blur-xl"
    >
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-line-subtle bg-raised/95 px-4 py-4 backdrop-blur-xl">
        <div>
          <h2 className="text-sm font-semibold text-primary">Rooms</h2>
          <p className="mt-0.5 font-mono text-[10px] text-tertiary tabular-nums">
            {rooms.length} · {totals.area.toFixed(0)} m² · {totals.objects} objects
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close room list"
          className="flex h-7 w-7 items-center justify-center rounded-lg text-tertiary transition-colors hover:bg-surface-hover hover:text-primary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
        >
          <CloseIcon className="h-4 w-4" />
        </button>
      </header>

      <ul className="p-2">
        {rooms.map((room) => {
          const isSelected = selected === room.id;
          const isOccupied = occupied === room.id;

          return (
            <li key={room.id}>
              <button
                type="button"
                onClick={() => (isSelected ? onClear() : onSelect(room))}
                aria-pressed={isSelected}
                className={[
                  "group w-full rounded-xl px-3 py-2.5 text-left transition-colors focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none",
                  isSelected
                    ? "bg-accent-surface ring-1 ring-focus"
                    : "hover:bg-surface-hover",
                ].join(" ")}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span
                    className={[
                      "truncate text-[13px] font-medium",
                      isSelected ? "text-accent-text" : "text-primary",
                    ].join(" ")}
                  >
                    {room.name}
                  </span>
                  {isOccupied && (
                    <span
                      title="You are here"
                      className="shrink-0 rounded-full bg-success-surface px-1.5 py-0.5 font-mono text-[9px] tracking-wide text-success-text uppercase"
                    >
                      here
                    </span>
                  )}
                </div>

                <p className="mt-0.5 font-mono text-[10px] text-tertiary tabular-nums">
                  {room.area_m2.toFixed(1)} m²
                  {room.object_count > 0 && ` · ${room.object_count} objects`}
                  {room.style && ` · ${room.style}`}
                </p>
              </button>
            </li>
          );
        })}
      </ul>

      {selected && (
        <div className="px-4 pb-4">
          <button
            type="button"
            onClick={onClear}
            className="w-full rounded-lg border border-line px-3 py-1.5 text-[12px] text-secondary transition-colors hover:border-line-strong hover:text-primary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
          >
            Clear highlight
          </button>
        </div>
      )}
    </aside>
  );
}
