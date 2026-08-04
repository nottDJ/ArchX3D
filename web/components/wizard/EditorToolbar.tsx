"use client";

import { useState } from "react";

import type { AlignMode } from "@/lib/snapping";
import type { SnapSettings } from "@/lib/snapping";
import {
  AlignLeftIcon,
  CopyIcon,
  DistributeIcon,
  GridIcon,
  LockIcon,
  RedoIcon,
  ResetIcon,
  TrashIcon,
  UndoIcon,
  UnlockIcon,
} from "./icons";

export interface EditorToolbarProps {
  selectionCount: number;
  canUndo: boolean;
  canRedo: boolean;
  clipboardCount: number;
  snap: SnapSettings;
  onSnapChange: (snap: SnapSettings) => void;
  onUndo: () => void;
  onRedo: () => void;
  onCopy: () => void;
  onPaste: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onLock: (locked: boolean) => void;
  onAlign: (mode: AlignMode) => void;
  onDistribute: (axis: "x" | "y") => void;
  onRotateToWall: () => void;
  onSelectAll: () => void;
}

/**
 * The editor's command surface.
 *
 * Everything here is also available from the keyboard; the toolbar exists so
 * the operations are discoverable, and so their enabled state says plainly
 * what the current selection supports — distribute needs three objects,
 * alignment needs two, and showing them greyed out explains that faster than
 * a tooltip.
 */
export function EditorToolbar({
  selectionCount,
  canUndo,
  canRedo,
  clipboardCount,
  snap,
  onSnapChange,
  onUndo,
  onRedo,
  onCopy,
  onPaste,
  onDuplicate,
  onDelete,
  onLock,
  onAlign,
  onDistribute,
  onRotateToWall,
  onSelectAll,
}: EditorToolbarProps) {
  const [showSnap, setShowSnap] = useState(false);
  const hasSelection = selectionCount > 0;
  const canAlign = selectionCount >= 2;
  const canDistribute = selectionCount >= 3;

  return (
    <div className="rounded-xl border border-line-subtle bg-surface">
      <div className="flex flex-wrap items-center gap-1 p-2">
        <Group>
          <Button label="Undo" hint="Ctrl+Z" disabled={!canUndo} onClick={onUndo}>
            <UndoIcon className="h-3.5 w-3.5" />
          </Button>
          <Button label="Redo" hint="Ctrl+Shift+Z" disabled={!canRedo} onClick={onRedo}>
            <RedoIcon className="h-3.5 w-3.5" />
          </Button>
        </Group>

        <Divider />

        <Group>
          <Button label="Copy" hint="Ctrl+C" disabled={!hasSelection} onClick={onCopy}>
            <CopyIcon className="h-3.5 w-3.5" />
          </Button>
          <Button
            label="Paste"
            hint="Ctrl+V"
            disabled={clipboardCount === 0}
            onClick={onPaste}
          >
            Paste
          </Button>
          <Button
            label="Duplicate"
            hint="Ctrl+D"
            disabled={!hasSelection}
            onClick={onDuplicate}
          >
            Duplicate
          </Button>
        </Group>

        <Divider />

        <Group>
          <Button label="Align left" disabled={!canAlign} onClick={() => onAlign("left")}>
            <AlignLeftIcon className="h-3.5 w-3.5" />
          </Button>
          <Button label="Align right" disabled={!canAlign} onClick={() => onAlign("right")}>
            <AlignLeftIcon className="h-3.5 w-3.5 rotate-180" />
          </Button>
          <Button
            label="Centre horizontally"
            disabled={!canAlign}
            onClick={() => onAlign("centre-x")}
          >
            Centre X
          </Button>
          <Button
            label="Centre vertically"
            disabled={!canAlign}
            onClick={() => onAlign("centre-y")}
          >
            Centre Y
          </Button>
          <Button
            label="Distribute horizontally"
            disabled={!canDistribute}
            onClick={() => onDistribute("x")}
          >
            <DistributeIcon className="h-3.5 w-3.5" />
          </Button>
          <Button
            label="Distribute vertically"
            disabled={!canDistribute}
            onClick={() => onDistribute("y")}
          >
            <DistributeIcon className="h-3.5 w-3.5 rotate-90" />
          </Button>
          <Button
            label="Rotate to face away from the nearest wall"
            disabled={!hasSelection}
            onClick={onRotateToWall}
          >
            To wall
          </Button>
        </Group>

        <Divider />

        <Group>
          <Button label="Lock" disabled={!hasSelection} onClick={() => onLock(true)}>
            <LockIcon className="h-3.5 w-3.5" />
          </Button>
          <Button label="Unlock" disabled={!hasSelection} onClick={() => onLock(false)}>
            <UnlockIcon className="h-3.5 w-3.5" />
          </Button>
          <Button
            label="Delete"
            hint="Del"
            disabled={!hasSelection}
            danger
            onClick={onDelete}
          >
            <TrashIcon className="h-3.5 w-3.5" />
          </Button>
        </Group>

        <div className="ml-auto flex items-center gap-1">
          <Button label="Select all" hint="Ctrl+A" onClick={onSelectAll}>
            Select all
          </Button>
          <Button
            label="Snapping options"
            active={snap.enabled}
            onClick={() => setShowSnap((value) => !value)}
          >
            <GridIcon className="h-3.5 w-3.5" />
            Snap
          </Button>
        </div>
      </div>

      {showSnap && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-line-subtle px-3 py-2.5">
          <Toggle
            label="Snapping"
            checked={snap.enabled}
            onChange={(enabled) => onSnapChange({ ...snap, enabled })}
          />
          <Toggle
            label="Walls"
            checked={snap.toWalls}
            onChange={(toWalls) => onSnapChange({ ...snap, toWalls })}
          />
          <Toggle
            label="Corners"
            checked={snap.toCorners}
            onChange={(toCorners) => onSnapChange({ ...snap, toCorners })}
          />
          <Toggle
            label="Room centre"
            checked={snap.toRoomCentre}
            onChange={(toRoomCentre) => onSnapChange({ ...snap, toRoomCentre })}
          />
          <Toggle
            label="Furniture"
            checked={snap.toObjects}
            onChange={(toObjects) => onSnapChange({ ...snap, toObjects })}
          />
          <Toggle
            label="Guides"
            checked={snap.toAlignmentGuides}
            onChange={(toAlignmentGuides) => onSnapChange({ ...snap, toAlignmentGuides })}
          />

          <label className="flex items-center gap-1.5 text-[11px] text-secondary">
            Grid
            <select
              value={String(snap.grid)}
              onChange={(event) =>
                onSnapChange({ ...snap, grid: Number(event.target.value) })
              }
              className="rounded-md border border-line bg-raised px-1.5 py-1 text-[11px] text-primary focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none"
            >
              <option value="0">off</option>
              <option value="0.05">5 cm</option>
              <option value="0.1">10 cm</option>
              <option value="0.25">25 cm</option>
              <option value="0.5">50 cm</option>
            </select>
          </label>

          <label className="flex items-center gap-1.5 text-[11px] text-secondary">
            Tolerance
            <select
              value={String(snap.tolerance)}
              onChange={(event) =>
                onSnapChange({ ...snap, tolerance: Number(event.target.value) })
              }
              className="rounded-md border border-line bg-raised px-1.5 py-1 text-[11px] text-primary focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none"
            >
              <option value="0.06">tight</option>
              <option value="0.12">normal</option>
              <option value="0.25">loose</option>
            </select>
          </label>

          <button
            type="button"
            onClick={() => setShowSnap(false)}
            className="ml-auto flex items-center gap-1 text-[11px] text-tertiary hover:text-secondary"
          >
            <ResetIcon className="h-3 w-3" />
            Done
          </button>
        </div>
      )}
    </div>
  );
}

function Group({ children }: { children: React.ReactNode }) {
  return <div className="flex items-center gap-0.5">{children}</div>;
}

function Divider() {
  return <span className="mx-1 h-5 w-px bg-surface-active" />;
}

function Button({
  label,
  hint,
  disabled,
  danger,
  active,
  onClick,
  children,
}: {
  label: string;
  hint?: string;
  disabled?: boolean;
  danger?: boolean;
  active?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={hint ? `${label} (${hint})` : label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={`flex items-center gap-1.5 rounded-lg border px-2 py-1.5 text-[11px] transition-colors disabled:opacity-30 ${
        active
          ? "border-accent-border bg-accent-surface text-accent-text"
          : "border-line text-secondary"
      } ${
        disabled
          ? ""
          : danger
            ? "hover:border-danger-border/40 hover:text-danger-text"
            : "hover:border-line-strong hover:text-primary"
      }`}
    >
      {children}
    </button>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-[11px] text-secondary">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="h-3 w-3 accent-[--accent-solid]"
      />
      {label}
    </label>
  );
}
