/**
 * ArchX3D — Editor document, history and selection
 * ================================================
 * The single source of truth for uncommitted edits in the review step.
 *
 * Why a document rather than scattered component state
 * ---------------------------------------------------
 * Undo has to restore *everything* a step changed — a batch rotate touches a
 * dozen objects, deleting a table cascades to what stood on it. Keeping each
 * edit type in its own `useState` makes that impossible to express, because
 * there is no single "before" to go back to. So every pending change lives in
 * one immutable `EditorDoc`, and history is a stack of those documents.
 *
 * Documents are small — ids and patches, never the scene graph itself — so
 * snapshotting one per action stays cheap even with a long history.
 *
 * The document is translated to the server's `ReviewEdits` shape only on
 * apply, by `toEdits`. Nothing here talks to the network.
 */

import {
  MAX_DIMENSION,
  MIN_DIMENSION,
  clampToPolygon,
  withOverride,
  type ObjectOverride,
  type ReviewEdits,
  type ReviewObject,
  type ReviewPayload,
  type ReviewRoom,
} from "./wizard";

/** How many undo steps to retain. Raise or set to Infinity for unlimited. */
export const HISTORY_LIMIT = 200;

export interface FinishEdit {
  material?: string;
  color_hex?: string;
  roughness?: number;
  metallic?: number;
  finish?: string;
}

export interface RoomFinishEdit {
  wall?: FinishEdit;
  floor?: FinishEdit;
  ceiling?: FinishEdit;
  ceiling_type?: string;
}

export interface LightOverride {
  kind?: string;
  mounting?: string;
  position?: { x: number; y: number; z?: number };
  color_temperature_k?: number;
  power_w?: number;
  size?: number;
  length?: number;
}

export interface AddObjectSpec extends ObjectOverride {
  /** Clone this object; omit to create a fresh one from `category`. */
  source_id?: string;
  category?: string;
  room_id?: string;
}

export interface AddLightSpec extends LightOverride {
  kind: string;
  room_id: string;
}

/** Every uncommitted change, as one immutable value. */
export interface EditorDoc {
  removed: string[];
  kept: string[];
  roomTypes: Record<string, string>;
  overrides: Record<string, ObjectOverride>;
  finishes: Record<string, RoomFinishEdit>;
  lights: Record<string, LightOverride>;
  removedLights: string[];
  added: AddObjectSpec[];
  addedLights: AddLightSpec[];
}

export const EMPTY_DOC: EditorDoc = {
  removed: [],
  kept: [],
  roomTypes: {},
  overrides: {},
  finishes: {},
  lights: {},
  removedLights: [],
  added: [],
  addedLights: [],
};

export interface EditorState {
  doc: EditorDoc;
  past: EditorDoc[];
  future: EditorDoc[];
  selection: string[];
  /** Objects held for paste. Stored as specs so paste works after a delete. */
  clipboard: AddObjectSpec[];
}

export const INITIAL_STATE: EditorState = {
  doc: EMPTY_DOC,
  past: [],
  future: [],
  selection: [],
  clipboard: [],
};

export function isDirty(doc: EditorDoc): boolean {
  return (
    doc.removed.length > 0 ||
    doc.kept.length > 0 ||
    doc.removedLights.length > 0 ||
    doc.added.length > 0 ||
    doc.addedLights.length > 0 ||
    Object.keys(doc.roomTypes).length > 0 ||
    Object.keys(doc.overrides).length > 0 ||
    Object.keys(doc.finishes).length > 0 ||
    Object.keys(doc.lights).length > 0
  );
}

export function countChanges(doc: EditorDoc): number {
  return (
    doc.removed.length +
    doc.kept.length +
    doc.removedLights.length +
    doc.added.length +
    doc.addedLights.length +
    Object.keys(doc.roomTypes).length +
    Object.keys(doc.overrides).length +
    Object.keys(doc.finishes).length +
    Object.keys(doc.lights).length
  );
}

/** Translate the document into the payload the edits endpoint accepts. */
export function toEdits(doc: EditorDoc): ReviewEdits {
  const edits: ReviewEdits = {};
  if (doc.removed.length) edits.remove_objects = doc.removed;
  if (doc.kept.length) edits.keep_objects = doc.kept;
  if (Object.keys(doc.roomTypes).length) edits.room_types = doc.roomTypes;
  if (Object.keys(doc.overrides).length) edits.object_overrides = doc.overrides;
  if (Object.keys(doc.finishes).length) edits.room_finishes = doc.finishes;
  if (Object.keys(doc.lights).length) edits.light_overrides = doc.lights;
  if (doc.removedLights.length) edits.remove_lights = doc.removedLights;
  if (doc.added.length) edits.add_objects = doc.added;
  if (doc.addedLights.length) edits.add_lights = doc.addedLights;
  return edits;
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

export type EditorAction =
  | { type: "select"; ids: string[]; mode?: "replace" | "toggle" | "add" }
  | { type: "clear-selection" }
  | { type: "patch"; ids: string[]; patch: ObjectOverride }
  | { type: "revert"; ids: string[] }
  | { type: "remove"; ids: string[] }
  | { type: "restore"; ids: string[] }
  | { type: "keep"; ids: string[]; keep: boolean }
  | { type: "room-type"; roomId: string; value: string }
  | { type: "finish"; roomId: string; patch: RoomFinishEdit }
  | { type: "light"; lightId: string; patch: LightOverride }
  | { type: "remove-light"; lightId: string }
  | { type: "add-light"; spec: AddLightSpec }
  | { type: "copy"; specs: AddObjectSpec[] }
  | { type: "add"; specs: AddObjectSpec[] }
  | { type: "undo" }
  | { type: "redo" }
  | { type: "reset" };

/** Actions that change the document and therefore create a history entry. */
const MUTATING = new Set([
  "patch", "revert", "remove", "restore", "keep", "room-type", "finish", "light",
  "remove-light", "add-light", "add",
]);

export function editorReducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {
    case "select": {
      const mode = action.mode ?? "replace";
      if (mode === "replace") return { ...state, selection: action.ids };
      if (mode === "add") {
        return { ...state, selection: [...new Set([...state.selection, ...action.ids])] };
      }
      const next = new Set(state.selection);
      for (const id of action.ids) {
        next.has(id) ? next.delete(id) : next.add(id);
      }
      return { ...state, selection: [...next] };
    }

    case "clear-selection":
      return { ...state, selection: [] };

    case "copy":
      return { ...state, clipboard: action.specs };

    case "undo": {
      if (!state.past.length) return state;
      const previous = state.past[state.past.length - 1];
      return {
        ...state,
        doc: previous,
        past: state.past.slice(0, -1),
        future: [state.doc, ...state.future],
      };
    }

    case "redo": {
      if (!state.future.length) return state;
      return {
        ...state,
        doc: state.future[0],
        past: [...state.past, state.doc],
        future: state.future.slice(1),
      };
    }

    case "reset":
      return { ...INITIAL_STATE, clipboard: state.clipboard };

    default:
      break;
  }

  const doc = applyToDoc(state.doc, action);
  if (doc === state.doc) return state;

  if (!MUTATING.has(action.type)) return { ...state, doc };

  // A new edit invalidates the redo branch, as in every editor.
  const past = [...state.past, state.doc];
  return {
    ...state,
    doc,
    past: past.length > HISTORY_LIMIT ? past.slice(past.length - HISTORY_LIMIT) : past,
    future: [],
  };
}

function applyToDoc(doc: EditorDoc, action: EditorAction): EditorDoc {
  switch (action.type) {
    case "patch": {
      if (!action.ids.length) return doc;
      const overrides = { ...doc.overrides };
      for (const id of action.ids) {
        overrides[id] = mergeOverride(overrides[id], action.patch);
      }
      return { ...doc, overrides };
    }

    case "revert": {
      // Discard pending edits for these objects, returning them to whatever
      // the server last served. Removal is a separate decision, so it stands.
      const overrides = { ...doc.overrides };
      let touched = false;
      for (const id of action.ids) {
        if (id in overrides) {
          delete overrides[id];
          touched = true;
        }
      }
      return touched ? { ...doc, overrides } : doc;
    }

    case "remove": {
      const removed = [...new Set([...doc.removed, ...action.ids])];
      // Dropping a removed object's pending edits keeps the payload honest:
      // the server would reject overrides for something being deleted.
      const overrides = { ...doc.overrides };
      for (const id of action.ids) delete overrides[id];
      return { ...doc, removed, overrides };
    }

    case "restore":
      return { ...doc, removed: doc.removed.filter((id) => !action.ids.includes(id)) };

    case "keep": {
      const kept = new Set(doc.kept);
      for (const id of action.ids) {
        action.keep ? kept.add(id) : kept.delete(id);
      }
      return { ...doc, kept: [...kept] };
    }

    case "room-type":
      return { ...doc, roomTypes: { ...doc.roomTypes, [action.roomId]: action.value } };

    case "finish": {
      const current = doc.finishes[action.roomId] ?? {};
      return {
        ...doc,
        finishes: {
          ...doc.finishes,
          [action.roomId]: {
            ...current,
            ...action.patch,
            wall: action.patch.wall ? { ...current.wall, ...action.patch.wall } : current.wall,
            floor: action.patch.floor
              ? { ...current.floor, ...action.patch.floor }
              : current.floor,
            ceiling: action.patch.ceiling
              ? { ...current.ceiling, ...action.patch.ceiling }
              : current.ceiling,
          },
        },
      };
    }

    case "light":
      return {
        ...doc,
        lights: {
          ...doc.lights,
          [action.lightId]: { ...doc.lights[action.lightId], ...action.patch },
        },
      };

    case "remove-light": {
      const lights = { ...doc.lights };
      delete lights[action.lightId];
      return {
        ...doc,
        lights,
        removedLights: [...new Set([...doc.removedLights, action.lightId])],
      };
    }

    case "add-light":
      return { ...doc, addedLights: [...doc.addedLights, action.spec] };

    case "add":
      return { ...doc, added: [...doc.added, ...action.specs] };

    default:
      return doc;
  }
}

function mergeOverride(
  current: ObjectOverride | undefined,
  patch: ObjectOverride,
): ObjectOverride {
  return {
    ...current,
    ...patch,
    // Dimensions merge per-axis so a width-only edit does not discard a
    // previously set depth.
    dimensions: patch.dimensions
      ? { ...current?.dimensions, ...patch.dimensions }
      : current?.dimensions,
  };
}

// ---------------------------------------------------------------------------
// Derived views
// ---------------------------------------------------------------------------

export interface ResolvedObject {
  object: ReviewObject;
  room: ReviewRoom;
  edited: boolean;
  removed: boolean;
}

/**
 * Every object with its pending edits folded in, indexed by id.
 *
 * One place computes this so the plan, the list and the inspector cannot show
 * different states of the same object.
 */
export function resolveObjects(
  review: ReviewPayload,
  doc: EditorDoc,
): Map<string, ResolvedObject> {
  const index = new Map<string, ResolvedObject>();
  const removed = new Set(doc.removed);

  for (const room of review.rooms) {
    for (const raw of room.objects) {
      const override = doc.overrides[raw.id];
      index.set(raw.id, {
        object: withOverride(raw, override),
        room,
        edited: Boolean(override),
        removed: removed.has(raw.id),
      });
    }
  }
  return index;
}

/** Ids matching a selection query, used by "select all in room" and friends. */
export function idsInRoom(review: ReviewPayload, roomId: string): string[] {
  const room = review.rooms.find((entry) => entry.id === roomId);
  return room ? room.objects.map((object) => object.id) : [];
}

export function idsByCategory(review: ReviewPayload, category: string): string[] {
  return review.rooms.flatMap((room) =>
    room.objects.filter((object) => object.category === category).map((o) => o.id),
  );
}

/** Build clipboard specs from the current selection. */
export function toClipboard(
  ids: string[],
  resolved: Map<string, ResolvedObject>,
): AddObjectSpec[] {
  const specs: AddObjectSpec[] = [];
  for (const id of ids) {
    const entry = resolved.get(id);
    if (!entry) continue;
    specs.push({
      source_id: id,
      room_id: entry.object.room_id,
      position: { x: entry.object.position.x, y: entry.object.position.y },
      rotation_z: entry.object.rotation_z,
      dimensions: { ...entry.object.dimensions },
    });
  }
  return specs;
}

/**
 * Offset pasted copies so they do not land exactly on their originals.
 *
 * An invisible paste reads as a no-op, and the user then pastes again. The
 * offset is clamped into the room so the copy is not born somewhere the server
 * would refuse to put it.
 */
export function offsetForPaste(
  specs: AddObjectSpec[],
  review: ReviewPayload,
  step = 0.35,
): AddObjectSpec[] {
  return specs.map((spec) => {
    if (!spec.position) return spec;
    const room = review.rooms.find((entry) => entry.id === spec.room_id);
    const moved = { x: spec.position.x + step, y: spec.position.y - step };
    return {
      ...spec,
      position: room ? clampToPolygon(moved, room.polygon) : moved,
    };
  });
}

/** Clamp a dimension to what the server will accept. */
export function clampDimension(value: number): number {
  return Math.min(MAX_DIMENSION, Math.max(MIN_DIMENSION, value));
}
