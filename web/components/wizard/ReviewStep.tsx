"use client";

import { useCallback, useEffect, useMemo, useReducer, useState } from "react";

import {
  ANALYSIS_MODE_COPY,
  clampToPolygon,
  confidenceTone,
  formatCategory,
  formatRoomType,
  wizardApi,
  type ObjectOverride,
  type ReviewEdits,
  type ReviewObject,
  type ReviewPayload,
  type ReviewRoom,
  type ValidationReport,
} from "@/lib/wizard";
import {
  INITIAL_STATE,
  countChanges,
  editorReducer,
  idsInRoom,
  isDirty,
  offsetForPaste,
  resolveObjects,
  toClipboard,
  toEdits,
  type EditorDoc,
  type LightOverride,
  type ResolvedObject,
  type RoomFinishEdit,
} from "@/lib/editor";
import {
  DEFAULT_SNAP,
  align,
  distribute,
  rotationToNearestWall,
  type AlignMode,
  type SnapSettings,
} from "@/lib/snapping";
import { EditorToolbar } from "./EditorToolbar";
import { Inspector } from "./Inspector";
import { PlanMap } from "./PlanMap";
import { RoomEditor } from "./RoomEditor";
import { CheckIcon, ChevronIcon, InfoIcon, TrashIcon, UndoIcon, WarningIcon } from "./icons";

/** Arrow-key nudge, in metres; Alt gives the finer step. */
const NUDGE = 0.05;
const NUDGE_FINE = 0.01;

export interface ReviewStepProps {
  projectId: string;
  review: ReviewPayload;
  busy: boolean;
  onApply: (edits: ReviewEdits) => Promise<void>;
  onGenerate: () => void;
}

/**
 * Step 3 — what the AI understood, before any render time is spent.
 *
 * The point of this screen is *disagreement*: everything shown is editable or
 * removable, and the things the pipeline threw away are shown too, so the user
 * can tell the difference between "not in the room" and "not detected".
 *
 * All pending edits live in one document (see `lib/editor`), which is what
 * makes undo able to restore a whole batch operation rather than one field.
 */
export function ReviewStep({ projectId, review, busy, onApply, onGenerate }: ReviewStepProps) {
  const [state, dispatch] = useReducer(editorReducer, INITIAL_STATE);
  const [snap, setSnap] = useState<SnapSettings>(DEFAULT_SNAP);
  const [selectedRoom, setSelectedRoom] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [showIgnored, setShowIgnored] = useState(false);
  const [validation, setValidation] = useState<ValidationReport | null>(null);
  const [checking, setChecking] = useState(false);

  const { doc, selection, clipboard, past, future } = state;
  const dirty = isDirty(doc);

  const resolved = useMemo(() => resolveObjects(review, doc), [review, doc]);

  const selectedObjects = useMemo(
    () =>
      selection
        .map((id) => resolved.get(id)?.object)
        .filter((object): object is ReviewObject => Boolean(object)),
    [selection, resolved],
  );

  const buildCount = useMemo(() => {
    let count = 0;
    for (const entry of resolved.values()) {
      if (entry.removed) continue;
      if (entry.object.will_build || doc.kept.includes(entry.object.id) || entry.edited) {
        count += 1;
      }
    }
    return count + doc.added.length;
  }, [resolved, doc.kept, doc.added]);

  // -------------------------------------------------------------------------
  // Commands
  // -------------------------------------------------------------------------

  const patchSelection = useCallback(
    (patch: ObjectOverride) => dispatch({ type: "patch", ids: selection, patch }),
    [selection],
  );

  const duplicate = useCallback(() => {
    const specs = offsetForPaste(toClipboard(selection, resolved), review);
    if (specs.length) dispatch({ type: "add", specs });
  }, [selection, resolved, review]);

  const paste = useCallback(() => {
    if (clipboard.length) {
      dispatch({ type: "add", specs: offsetForPaste(clipboard, review) });
    }
  }, [clipboard, review]);

  const applyAlign = useCallback(
    (mode: AlignMode) => {
      const positions = align(selectedObjects, mode);
      for (const [id, position] of Object.entries(positions)) {
        const room = resolved.get(id)?.room;
        dispatch({
          type: "patch",
          ids: [id],
          patch: { position: room ? clampToPolygon(position, room.polygon) : position },
        });
      }
    },
    [selectedObjects, resolved],
  );

  const applyDistribute = useCallback(
    (axis: "x" | "y") => {
      const positions = distribute(selectedObjects, axis);
      for (const [id, position] of Object.entries(positions)) {
        dispatch({ type: "patch", ids: [id], patch: { position } });
      }
    },
    [selectedObjects],
  );

  const rotateToWall = useCallback(() => {
    for (const object of selectedObjects) {
      const room = resolved.get(object.id)?.room;
      if (!room) continue;
      const angle = rotationToNearestWall(object, room);
      if (angle !== null) {
        dispatch({ type: "patch", ids: [object.id], patch: { rotation_z: angle } });
      }
    }
  }, [selectedObjects, resolved]);

  const selectAll = useCallback(() => {
    const ids = selectedRoom
      ? idsInRoom(review, selectedRoom)
      : [...resolved.keys()];
    dispatch({ type: "select", ids, mode: "replace" });
  }, [selectedRoom, review, resolved]);

  // -------------------------------------------------------------------------
  // Keyboard
  // -------------------------------------------------------------------------

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) return;

      const meta = event.ctrlKey || event.metaKey;

      if (meta && event.key.toLowerCase() === "z") {
        event.preventDefault();
        dispatch({ type: event.shiftKey ? "redo" : "undo" });
        return;
      }
      if (meta && event.key.toLowerCase() === "y") {
        event.preventDefault();
        dispatch({ type: "redo" });
        return;
      }
      if (meta && event.key.toLowerCase() === "a") {
        event.preventDefault();
        selectAll();
        return;
      }
      if (meta && event.key.toLowerCase() === "c") {
        if (selection.length) {
          event.preventDefault();
          dispatch({ type: "copy", specs: toClipboard(selection, resolved) });
        }
        return;
      }
      if (meta && event.key.toLowerCase() === "v") {
        event.preventDefault();
        paste();
        return;
      }
      if (meta && event.key.toLowerCase() === "d") {
        event.preventDefault();
        duplicate();
        return;
      }
      if (event.key === "Escape") {
        dispatch({ type: "clear-selection" });
        return;
      }
      if ((event.key === "Delete" || event.key === "Backspace") && selection.length) {
        event.preventDefault();
        dispatch({ type: "remove", ids: selection });
        dispatch({ type: "clear-selection" });
        return;
      }

      // Arrow keys nudge. Dragging is quick but imprecise; this is how a user
      // moves a sofa exactly 20 cm without fighting the pointer.
      const deltas: Record<string, [number, number]> = {
        ArrowLeft: [-1, 0],
        ArrowRight: [1, 0],
        ArrowUp: [0, 1],
        ArrowDown: [0, -1],
      };
      const delta = deltas[event.key];
      if (!delta || !selection.length) return;

      event.preventDefault();
      const step = event.altKey ? NUDGE_FINE : NUDGE;
      for (const id of selection) {
        const entry = resolved.get(id);
        if (!entry || entry.object.locked || entry.removed) continue;
        const next = {
          x: entry.object.position.x + delta[0] * step,
          y: entry.object.position.y + delta[1] * step,
        };
        dispatch({
          type: "patch",
          ids: [id],
          patch: {
            position:
              entry.object.support === "floor"
                ? clampToPolygon(next, entry.room.polygon)
                : next,
          },
        });
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selection, resolved, selectAll, paste, duplicate]);

  // -------------------------------------------------------------------------
  // Apply and validate
  // -------------------------------------------------------------------------

  const handleApply = async () => {
    await onApply(toEdits(doc));
    dispatch({ type: "reset" });
  };

  const runCheck = async () => {
    setChecking(true);
    try {
      setValidation(await wizardApi.validate(projectId));
    } finally {
      setChecking(false);
    }
  };

  const visibleRooms = selectedRoom
    ? review.rooms.filter((room) => room.id === selectedRoom)
    : review.rooms;

  return (
    <div className="space-y-6">
      {/* ---- Summary ------------------------------------------------- */}
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Rooms" value={`${review.totals.rooms_with_imagery}/${review.totals.rooms}`}
              hint="with reference imagery" />
        <Stat label="Objects" value={String(buildCount)} hint="will be built" />
        <Stat label="Lights" value={String(review.totals.lights)} hint="recovered fixtures" />
        <Stat
          label="Confidence"
          value={review.confidence.mean ? review.confidence.mean.toFixed(2) : "—"}
          hint="mean across objects"
        />
      </section>

      {/* ---- Warnings ------------------------------------------------ */}
      {review.warnings.length > 0 && (
        <section className="rounded-xl border border-warning-border bg-warning-surface p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-warning-text">
            <WarningIcon className="h-4 w-4" />
            Worth checking before you generate
          </h3>
          <ul className="mt-2 space-y-1">
            {review.warnings.map((warning) => (
              <li key={warning} className="text-sm leading-relaxed text-warning-text">
                • {warning}
              </li>
            ))}
          </ul>
        </section>
      )}

      <ValidationPanel
        report={validation}
        checking={checking}
        dirty={dirty}
        onRun={runCheck}
      />

      {/* ---- Images and how each was used ---------------------------- */}
      <section>
        <h3 className="mb-3 text-sm font-semibold text-primary">
          Reference images and how each was used
        </h3>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {review.images.map((image) => {
            const copy = ANALYSIS_MODE_COPY[image.analysis_mode];
            return (
              <article
                key={image.image_id}
                className="overflow-hidden rounded-xl border border-line-subtle bg-surface"
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={wizardApi.imageUrl(projectId, image.file)}
                  alt={image.file}
                  className="h-32 w-full bg-black/40 object-cover"
                />
                <div className="space-y-1.5 p-3">
                  <p className="truncate font-mono text-[11px] text-secondary" title={image.file}>
                    {image.file}
                  </p>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <Tag tone="neutral">{image.image_class.replace(/_/g, " ")}</Tag>
                    <Tag tone={image.contributes_appearance ? "sky" : "amber"}>{copy.label}</Tag>
                    {image.medium === "photo" ? (
                      <Tag tone="emerald">photo</Tag>
                    ) : (
                      <Tag tone="neutral">{image.medium}</Tag>
                    )}
                  </div>
                  <p className="text-[11px] leading-relaxed text-tertiary">{copy.detail}</p>
                </div>
              </article>
            );
          })}
        </div>
      </section>

      {/* ---- Editor -------------------------------------------------- */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-primary">Detected layout</h3>
          {selectedRoom && (
            <button
              type="button"
              onClick={() => setSelectedRoom(null)}
              className="text-xs text-accent-text hover:text-accent-text"
            >
              Show all rooms
            </button>
          )}
        </div>

        <EditorToolbar
          selectionCount={selection.length}
          canUndo={past.length > 0}
          canRedo={future.length > 0}
          clipboardCount={clipboard.length}
          snap={snap}
          onSnapChange={setSnap}
          onUndo={() => dispatch({ type: "undo" })}
          onRedo={() => dispatch({ type: "redo" })}
          onCopy={() => dispatch({ type: "copy", specs: toClipboard(selection, resolved) })}
          onPaste={paste}
          onDuplicate={duplicate}
          onDelete={() => {
            dispatch({ type: "remove", ids: selection });
            dispatch({ type: "clear-selection" });
          }}
          onLock={(locked) => patchSelection({ locked })}
          onAlign={applyAlign}
          onDistribute={applyDistribute}
          onRotateToWall={rotateToWall}
          onSelectAll={selectAll}
        />

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
          <PlanMap
            rooms={review.rooms}
            resolved={resolved}
            selection={selection}
            onSelect={(ids, mode) => dispatch({ type: "select", ids, mode })}
            onTransform={(patches) => {
              for (const [id, patch] of Object.entries(patches)) {
                dispatch({ type: "patch", ids: [id], patch });
              }
            }}
            selectedRoomId={selectedRoom}
            onSelectRoom={setSelectedRoom}
            snap={snap}
            highlightedObjectId={hovered}
          />

          {selectedObjects.length > 0 ? (
            <Inspector
              objects={selectedObjects}
              rooms={review.rooms}
              vocabulary={review.vocabulary}
              editedIds={new Set(Object.keys(doc.overrides))}
              removedIds={new Set(doc.removed)}
              onPatch={patchSelection}
              onRevert={() => dispatch({ type: "revert", ids: selection })}
              onToggleRemoved={() => {
                const anyRemoved = selection.some((id) => doc.removed.includes(id));
                dispatch({ type: anyRemoved ? "restore" : "remove", ids: selection });
              }}
              onClose={() => dispatch({ type: "clear-selection" })}
            />
          ) : (
            <aside className="hidden rounded-xl border border-dashed border-line p-6 text-center text-xs leading-relaxed text-tertiary lg:flex lg:items-center lg:justify-center">
              Select an object in the plan to move, rotate, resize, reclassify,
              swap its asset or change its material.
            </aside>
          )}
        </div>
      </section>

      {/* ---- Rooms --------------------------------------------------- */}
      <section className="space-y-3">
        <h3 className="text-sm font-semibold text-primary">Rooms and their contents</h3>
        {visibleRooms.map((room) => (
          <RoomPanel
            key={room.id}
            room={room}
            doc={doc}
            resolved={resolved}
            vocabulary={review.vocabulary}
            roomTypeOptions={review.vocabulary.room_types}
            selection={selection}
            onSelectObject={(id, additive) =>
              dispatch({ type: "select", ids: [id], mode: additive ? "toggle" : "replace" })
            }
            onRoomType={(value) =>
              dispatch({ type: "room-type", roomId: room.id, value })
            }
            onToggleRemoved={(id) =>
              dispatch({
                type: doc.removed.includes(id) ? "restore" : "remove",
                ids: [id],
              })
            }
            onToggleKept={(id) =>
              dispatch({ type: "keep", ids: [id], keep: !doc.kept.includes(id) })
            }
            onHover={setHovered}
            onFinish={(patch) => dispatch({ type: "finish", roomId: room.id, patch })}
            onLight={(lightId, patch) => dispatch({ type: "light", lightId, patch })}
            onRemoveLight={(lightId) => dispatch({ type: "remove-light", lightId })}
            onAddLight={(kind) =>
              dispatch({ type: "add-light", spec: { kind, room_id: room.id } })
            }
          />
        ))}
      </section>

      {/* ---- Ignored detections -------------------------------------- */}
      {review.ignored.length > 0 && (
        <section className="rounded-xl border border-line-subtle bg-surface">
          <button
            type="button"
            onClick={() => setShowIgnored((value) => !value)}
            className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm text-secondary hover:text-primary"
          >
            <ChevronIcon
              className={`h-3.5 w-3.5 transition-transform ${showIgnored ? "rotate-90" : ""}`}
            />
            Ignored detections
            <span className="text-tertiary">
              ({review.ignored.reduce((total, entry) => total + entry.count, 0)})
            </span>
          </button>
          {showIgnored && (
            <ul className="space-y-2 border-t border-line-subtle px-4 py-3">
              {review.ignored.map((entry) => (
                <li key={entry.reason} className="flex gap-3 text-xs">
                  <span className="w-8 shrink-0 text-right font-mono text-secondary">
                    {entry.count}
                  </span>
                  <span className="text-tertiary">{entry.explanation}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* ---- Actions ------------------------------------------------- */}
      <section className="sticky bottom-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-raised/90 p-4 backdrop-blur">
        <p className="text-xs text-secondary">
          {dirty ? (
            <>
              <span className="text-primary">{countChanges(doc)}</span> pending change
              {countChanges(doc) === 1 ? "" : "s"}
              {past.length > 0 && (
                <span className="text-tertiary"> · {past.length} undo step
                  {past.length === 1 ? "" : "s"}</span>
              )}
            </>
          ) : (
            <>
              <span className="text-primary">{buildCount}</span> objects ready to build
            </>
          )}
        </p>

        <div className="flex gap-2.5">
          {dirty && (
            <button
              type="button"
              disabled={busy}
              onClick={handleApply}
              className="flex items-center gap-2 rounded-lg border border-white/15 px-4 py-2 text-sm font-medium text-primary transition-colors hover:border-white/30 hover:text-primary disabled:opacity-50"
            >
              <CheckIcon className="h-3.5 w-3.5" />
              Apply changes
            </button>
          )}
          <button
            type="button"
            disabled={busy || dirty}
            title={dirty ? "Apply your changes first" : undefined}
            onClick={onGenerate}
            className="rounded-lg bg-white px-5 py-2 text-sm font-medium text-on-solid transition-transform hover:-translate-y-px active:translate-y-0 disabled:translate-y-0 disabled:opacity-40"
          >
            Generate 3D model
          </button>
        </div>
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Validation panel
// ---------------------------------------------------------------------------

/**
 * Results of the deterministic re-check.
 *
 * Deliberately advisory: it never blocks generation, because reaching this
 * screen means the user has looked at the scene and chosen it. An error here
 * says "this will render badly", not "this is forbidden".
 */
function ValidationPanel({
  report,
  checking,
  dirty,
  onRun,
}: {
  report: ValidationReport | null;
  checking: boolean;
  dirty: boolean;
  onRun: () => void;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <section className="rounded-xl border border-line-subtle bg-surface p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold text-primary">Physical checks</h3>

        {report && (
          <span className="flex items-center gap-2 text-xs">
            <Tag tone={report.errors > 0 ? "amber" : "emerald"}>
              {report.errors} error{report.errors === 1 ? "" : "s"}
            </Tag>
            <Tag tone="neutral">
              {report.warnings} warning{report.warnings === 1 ? "" : "s"}
            </Tag>
          </span>
        )}

        <button
          type="button"
          onClick={onRun}
          disabled={checking}
          className="ml-auto rounded-lg border border-line px-3 py-1.5 text-xs text-secondary transition-colors hover:border-line-strong hover:text-primary disabled:opacity-40"
        >
          {checking ? "Checking…" : report ? "Re-check" : "Run checks"}
        </button>
      </div>

      <p className="mt-2 text-[11px] leading-relaxed text-tertiary">
        Collision, containment, support, door clearance, circulation and
        reachability — all geometric, no AI. Findings are advisory: your
        placements are never overruled.
        {dirty && " Apply your pending changes to check them."}
      </p>

      {report && report.issues.length > 0 && (
        <>
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="mt-3 flex items-center gap-2 text-xs text-accent-text hover:text-accent-text"
          >
            <ChevronIcon
              className={`h-3 w-3 transition-transform ${expanded ? "rotate-90" : ""}`}
            />
            {expanded ? "Hide" : "Show"} {report.issues.length} finding
            {report.issues.length === 1 ? "" : "s"}
          </button>

          {expanded && (
            <ul className="mt-2 max-h-72 space-y-1.5 overflow-y-auto">
              {report.issues.map((issue, index) => (
                <li
                  key={`${issue.kind}-${issue.subject}-${index}`}
                  className="flex gap-2.5 rounded-lg border border-line-subtle px-2.5 py-2 text-[11px]"
                >
                  <span
                    className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${
                      issue.severity === "error" ? "bg-danger-solid" : "bg-warning-solid"
                    }`}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="font-mono text-secondary">{issue.kind}</span>
                    <span className="text-tertiary"> — {issue.detail}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Room panel
// ---------------------------------------------------------------------------

function RoomPanel({
  room,
  doc,
  resolved,
  vocabulary,
  roomTypeOptions,
  selection,
  onSelectObject,
  onRoomType,
  onToggleRemoved,
  onToggleKept,
  onHover,
  onFinish,
  onLight,
  onRemoveLight,
  onAddLight,
}: {
  room: ReviewRoom;
  doc: EditorDoc;
  resolved: Map<string, ResolvedObject>;
  vocabulary: ReviewPayload["vocabulary"];
  roomTypeOptions: string[];
  selection: string[];
  onSelectObject: (id: string, additive: boolean) => void;
  onRoomType: (value: string) => void;
  onToggleRemoved: (id: string) => void;
  onToggleKept: (id: string) => void;
  onHover: (id: string | null) => void;
  onFinish: (patch: RoomFinishEdit) => void;
  onLight: (lightId: string, patch: LightOverride) => void;
  onRemoveLight: (lightId: string) => void;
  onAddLight: (kind: string) => void;
}) {
  const [open, setOpen] = useState(room.object_count > 0);
  const currentType = doc.roomTypes[room.id] ?? room.room_type;
  const selected = new Set(selection);

  return (
    <article className="overflow-hidden rounded-xl border border-line-subtle bg-surface">
      <header className="flex flex-wrap items-center gap-3 border-b border-line-subtle px-4 py-3">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex items-center gap-2 text-sm font-medium text-primary"
        >
          <ChevronIcon
            className={`h-3.5 w-3.5 text-tertiary transition-transform ${open ? "rotate-90" : ""}`}
          />
          {formatRoomType(currentType)}
        </button>

        <span className="font-mono text-[11px] text-tertiary">
          {room.area.toFixed(1)} m² · {room.width.toFixed(1)}×{room.depth.toFixed(1)} m
        </span>

        {!room.has_imagery && <Tag tone="amber">no reference image — will be empty</Tag>}

        <div className="ml-auto flex items-center gap-2">
          <label className="text-[11px] text-tertiary">
            Room type
            <select
              value={currentType}
              onChange={(event) => onRoomType(event.target.value)}
              className="ml-2 rounded-md border border-line bg-raised px-2 py-1 text-xs text-primary focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none"
            >
              {roomTypeOptions.map((value) => (
                <option key={value} value={value}>
                  {formatRoomType(value)}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>

      {open && (
        <>
          <div className="divide-y divide-white/[0.04]">
            {room.objects.length === 0 ? (
              <p className="px-4 py-6 text-center text-xs text-tertiary">
                {room.has_imagery
                  ? "No objects were detected in this room."
                  : "Upload a reference image of this room to furnish it."}
              </p>
            ) : (
              room.objects.map((raw) => {
                const entry = resolved.get(raw.id);
                if (!entry) return null;
                return (
                  <ObjectRow
                    key={raw.id}
                    object={entry.object}
                    edited={entry.edited}
                    selected={selected.has(raw.id)}
                    removed={entry.removed}
                    kept={doc.kept.includes(raw.id)}
                    onSelect={(additive) => onSelectObject(raw.id, additive)}
                    onToggleRemoved={() => onToggleRemoved(raw.id)}
                    onToggleKept={() => onToggleKept(raw.id)}
                    onHover={onHover}
                  />
                );
              })
            )}
          </div>

          <RoomEditor
            room={room}
            vocabulary={vocabulary}
            finishEdit={doc.finishes[room.id]}
            lightEdits={doc.lights}
            removedLights={new Set(doc.removedLights)}
            onFinish={onFinish}
            onLight={onLight}
            onRemoveLight={onRemoveLight}
            onAddLight={onAddLight}
          />
        </>
      )}
    </article>
  );
}

function ObjectRow({
  object,
  edited,
  selected,
  removed,
  kept,
  onSelect,
  onToggleRemoved,
  onToggleKept,
  onHover,
}: {
  object: ReviewObject;
  edited: boolean;
  selected: boolean;
  removed: boolean;
  kept: boolean;
  onSelect: (additive: boolean) => void;
  onToggleRemoved: () => void;
  onToggleKept: () => void;
  onHover: (id: string | null) => void;
}) {
  const tone = confidenceTone(object.confidence);
  const willBuild = !removed && (object.will_build || kept || edited);

  return (
    <div
      onMouseEnter={() => onHover(object.id)}
      onMouseLeave={() => onHover(null)}
      onClick={(event) => onSelect(event.shiftKey)}
      className={`flex cursor-pointer flex-wrap items-center gap-3 px-4 py-2.5 transition-colors hover:bg-surface ${
        removed ? "opacity-45" : ""
      } ${selected ? "bg-accent-surface ring-1 ring-inset ring-focus" : ""}`}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          tone === "high" ? "bg-success-solid" : tone === "medium" ? "bg-warning-solid" : "bg-danger-solid"
        }`}
      />

      <span
        className={`min-w-0 flex-1 truncate text-sm ${
          removed ? "text-tertiary line-through" : "text-primary"
        }`}
        title={object.label || object.category}
      >
        {formatCategory(object.category)}
        {object.label && <span className="ml-2 text-xs text-tertiary">{object.label}</span>}
      </span>

      <span className="hidden font-mono text-[11px] text-tertiary sm:block">
        {object.dimensions.width.toFixed(2)}×{object.dimensions.depth.toFixed(2)}×
        {object.dimensions.height.toFixed(2)} m
      </span>

      {object.observation_count > 1 && (
        <Tag tone="emerald">{object.observation_count} views</Tag>
      )}

      {object.uncertain && !kept && !edited && <Tag tone="amber">uncertain — skipped</Tag>}
      {kept && <Tag tone="sky">kept</Tag>}
      {edited && <Tag tone="sky">edited</Tag>}
      {object.locked && <Tag tone="neutral">locked</Tag>}

      <span className="w-10 text-right font-mono text-[11px] text-secondary">
        {object.confidence.toFixed(2)}
      </span>

      {/* The row itself selects, so the buttons must not bubble into it. */}
      <div className="flex gap-1" onClick={(event) => event.stopPropagation()}>
        {object.uncertain && !removed && (
          <button
            type="button"
            onClick={onToggleKept}
            title={kept ? "Skip this detection again" : "Build this despite low confidence"}
            className="rounded-md p-1.5 text-tertiary transition-colors hover:bg-surface-hover hover:text-accent-text"
          >
            <CheckIcon className="h-3.5 w-3.5" />
          </button>
        )}
        <button
          type="button"
          onClick={onToggleRemoved}
          title={removed ? "Restore this object" : "Remove this object"}
          className="rounded-md p-1.5 text-tertiary transition-colors hover:bg-surface-hover hover:text-danger-text"
        >
          {removed ? <UndoIcon className="h-3.5 w-3.5" /> : <TrashIcon className="h-3.5 w-3.5" />}
        </button>
      </div>

      {!willBuild && !removed && !object.uncertain && (
        <span className="w-full text-[11px] text-warning-text">
          <InfoIcon className="mr-1 inline h-3 w-3" />
          withheld by validation
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small pieces
// ---------------------------------------------------------------------------

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-xl border border-line-subtle bg-surface px-4 py-3">
      <p className="font-mono text-[10px] tracking-widest text-tertiary uppercase">{label}</p>
      <p className="mt-1 text-xl font-semibold text-primary">{value}</p>
      <p className="mt-0.5 text-[11px] text-tertiary">{hint}</p>
    </div>
  );
}

function Tag({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "sky" | "amber" | "emerald";
}) {
  const tones = {
    neutral: "border-line text-secondary",
    sky: "border-accent-border text-accent-text",
    amber: "border-warning-border text-warning-text",
    emerald: "border-success-border text-success-text",
  } as const;

  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-[10px] whitespace-nowrap ${tones[tone]}`}
    >
      {children}
    </span>
  );
}
