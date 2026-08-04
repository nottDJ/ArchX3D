/**
 * Tests for the editor's document model, history and geometry.
 *
 * These cover the logic that has no server counterpart and therefore no
 * coverage from the Python suite: undo/redo semantics, how a batch operation
 * becomes one history entry, snapping candidate selection, and the alignment
 * and distribution maths.
 *
 * Run with `npm test`, which compiles `lib/` to CommonJS first. Uses the
 * built-in `node:test` runner so the web app gains no test dependency.
 */

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");

const {
  INITIAL_STATE,
  editorReducer,
  toEdits,
  isDirty,
  countChanges,
  resolveObjects,
  toClipboard,
  offsetForPaste,
} = require("../.test-build/editor.js");
const {
  DEFAULT_SNAP,
  align,
  distribute,
  snapPosition,
  rotationToNearestWall,
  normaliseDegrees,
} = require("../.test-build/snapping.js");

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function object(id, x, y, width = 1, depth = 0.6, extra = {}) {
  return {
    id,
    category: "sofa",
    label: "",
    group: "furniture",
    room_id: "r1",
    position: { x, y, z: 0 },
    rotation_z: 0,
    dimensions: { width, depth, height: 0.8 },
    material: "fabric",
    color_hex: "#CCCCCC",
    asset: "",
    confidence: 0.9,
    band: "accept",
    uncertain: false,
    locked: false,
    support: "floor",
    support_id: "",
    will_build: true,
    source_images: [],
    observation_count: 1,
    flags: [],
    distance_to_nearest_wall: 0,
    ...extra,
  };
}

const ROOM = {
  id: "r1",
  room_type: "living_room",
  style: "modern",
  area: 30,
  width: 6,
  depth: 5,
  ceiling_height: 3,
  confidence: 0.9,
  polygon: [[0, 0], [6, 0], [6, 5], [0, 5]],
  bounds_min: [0, 0],
  bounds_max: [6, 5],
  connected_to: [],
  source_images: ["a.jpg"],
  has_imagery: true,
  finishes: { wall: null, floor: null, ceiling: null, ceiling_type: "plain" },
  object_count: 0,
  objects: [],
  lights: [],
  openings: [],
};

function review(objects) {
  return { rooms: [{ ...ROOM, objects, object_count: objects.length }] };
}

/** Apply a sequence of actions to a fresh state. */
function run(...actions) {
  return actions.reduce(editorReducer, INITIAL_STATE);
}

// ---------------------------------------------------------------------------
// Document
// ---------------------------------------------------------------------------

describe("editor document", () => {
  it("starts clean", () => {
    assert.equal(isDirty(INITIAL_STATE.doc), false);
    assert.deepEqual(toEdits(INITIAL_STATE.doc), {});
  });

  it("omits empty collections from the payload", () => {
    // The server reports unrecognised keys, so sending empty ones is noise.
    const state = run({ type: "remove", ids: ["a"] });
    assert.deepEqual(toEdits(state.doc), { remove_objects: ["a"] });
  });

  it("merges dimension patches per axis", () => {
    const state = run(
      { type: "patch", ids: ["a"], patch: { dimensions: { width: 2 } } },
      { type: "patch", ids: ["a"], patch: { dimensions: { depth: 1 } } },
    );
    assert.deepEqual(state.doc.overrides.a.dimensions, { width: 2, depth: 1 });
  });

  it("applies one patch to every id in a batch", () => {
    const state = run({ type: "patch", ids: ["a", "b", "c"], patch: { locked: true } });
    for (const id of ["a", "b", "c"]) {
      assert.equal(state.doc.overrides[id].locked, true);
    }
  });

  it("drops pending edits for an object that is then removed", () => {
    // Overrides for a deleted object would be rejected by the server.
    const state = run(
      { type: "patch", ids: ["a"], patch: { rotation_z: 90 } },
      { type: "remove", ids: ["a"] },
    );
    assert.equal("a" in state.doc.overrides, false);
    assert.deepEqual(state.doc.removed, ["a"]);
  });

  it("reverts pending edits without un-removing", () => {
    const state = run(
      { type: "remove", ids: ["a"] },
      { type: "patch", ids: ["b"], patch: { rotation_z: 90 } },
      { type: "revert", ids: ["b"] },
    );
    assert.equal("b" in state.doc.overrides, false);
    assert.deepEqual(state.doc.removed, ["a"]);
  });

  it("counts every kind of change", () => {
    const state = run(
      { type: "remove", ids: ["a"] },
      { type: "patch", ids: ["b"], patch: { locked: true } },
      { type: "room-type", roomId: "r1", value: "office" },
      { type: "light", lightId: "l1", patch: { power_w: 90 } },
    );
    assert.equal(countChanges(state.doc), 4);
    assert.equal(isDirty(state.doc), true);
  });
});

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

describe("undo and redo", () => {
  it("restores the previous document", () => {
    const state = run(
      { type: "patch", ids: ["a"], patch: { rotation_z: 90 } },
      { type: "undo" },
    );
    assert.equal(isDirty(state.doc), false);
  });

  it("restores a whole batch as one step", () => {
    // The reason the document is a single value: undoing a batch rotate must
    // not require twelve undos.
    const state = run(
      { type: "patch", ids: ["a", "b", "c"], patch: { rotation_z: 90 } },
      { type: "undo" },
    );
    assert.deepEqual(state.doc.overrides, {});
    assert.equal(state.past.length, 0);
  });

  it("redoes what was undone", () => {
    const state = run(
      { type: "patch", ids: ["a"], patch: { rotation_z: 90 } },
      { type: "undo" },
      { type: "redo" },
    );
    assert.equal(state.doc.overrides.a.rotation_z, 90);
  });

  it("discards the redo branch after a new edit", () => {
    const state = run(
      { type: "patch", ids: ["a"], patch: { rotation_z: 90 } },
      { type: "undo" },
      { type: "patch", ids: ["b"], patch: { rotation_z: 45 } },
    );
    assert.equal(state.future.length, 0);
    assert.equal("a" in state.doc.overrides, false);
    assert.equal(state.doc.overrides.b.rotation_z, 45);
  });

  it("is a no-op at the ends of the history", () => {
    assert.equal(editorReducer(INITIAL_STATE, { type: "undo" }), INITIAL_STATE);
    assert.equal(editorReducer(INITIAL_STATE, { type: "redo" }), INITIAL_STATE);
  });

  it("does not record selection changes", () => {
    // Selecting is not an edit; undo should skip past it to the real change.
    const state = run(
      { type: "patch", ids: ["a"], patch: { rotation_z: 90 } },
      { type: "select", ids: ["b"] },
      { type: "select", ids: ["c"] },
      { type: "undo" },
    );
    assert.deepEqual(state.doc.overrides, {});
  });

  it("survives many steps and unwinds in order", () => {
    let state = INITIAL_STATE;
    for (let i = 0; i < 50; i += 1) {
      state = editorReducer(state, {
        type: "patch", ids: [`o${i}`], patch: { rotation_z: i },
      });
    }
    assert.equal(Object.keys(state.doc.overrides).length, 50);
    for (let i = 0; i < 50; i += 1) state = editorReducer(state, { type: "undo" });
    assert.deepEqual(state.doc.overrides, {});
  });
});

// ---------------------------------------------------------------------------
// Selection and clipboard
// ---------------------------------------------------------------------------

describe("selection", () => {
  it("replaces, adds and toggles", () => {
    let state = run({ type: "select", ids: ["a", "b"] });
    assert.deepEqual(state.selection, ["a", "b"]);

    state = editorReducer(state, { type: "select", ids: ["c"], mode: "add" });
    assert.deepEqual(state.selection.sort(), ["a", "b", "c"]);

    state = editorReducer(state, { type: "select", ids: ["b"], mode: "toggle" });
    assert.deepEqual(state.selection.sort(), ["a", "c"]);
  });

  it("never holds duplicates", () => {
    const state = run(
      { type: "select", ids: ["a"] },
      { type: "select", ids: ["a", "b"], mode: "add" },
    );
    assert.deepEqual(state.selection.sort(), ["a", "b"]);
  });
});

describe("clipboard", () => {
  it("captures placement so paste survives deleting the source", () => {
    const resolved = resolveObjects(review([object("a", 2, 2)]), INITIAL_STATE.doc);
    const specs = toClipboard(["a"], resolved);

    assert.equal(specs.length, 1);
    assert.equal(specs[0].source_id, "a");
    assert.deepEqual(specs[0].position, { x: 2, y: 2 });
  });

  it("offsets a paste so it does not hide under the original", () => {
    const specs = offsetForPaste(
      [{ source_id: "a", room_id: "r1", position: { x: 2, y: 2 } }],
      review([]),
    );
    assert.notDeepEqual(specs[0].position, { x: 2, y: 2 });
  });

  it("keeps an offset paste inside the room", () => {
    // Pasting a corner object must not push the copy through the wall.
    const specs = offsetForPaste(
      [{ source_id: "a", room_id: "r1", position: { x: 5.95, y: 0.05 } }],
      review([]),
    );
    const { x, y } = specs[0].position;
    assert.ok(x >= 0 && x <= 6, `x=${x} escaped the room`);
    assert.ok(y >= 0 && y <= 5, `y=${y} escaped the room`);
  });
});

// ---------------------------------------------------------------------------
// Resolution
// ---------------------------------------------------------------------------

describe("resolveObjects", () => {
  it("folds pending edits into the object", () => {
    const state = run({ type: "patch", ids: ["a"], patch: { rotation_z: 45 } });
    const resolved = resolveObjects(review([object("a", 2, 2)]), state.doc);

    assert.equal(resolved.get("a").object.rotation_z, 45);
    assert.equal(resolved.get("a").edited, true);
  });

  it("marks removal without dropping the entry", () => {
    // The plan still draws removed objects, struck through, so they can be
    // restored — they must survive resolution.
    const state = run({ type: "remove", ids: ["a"] });
    const resolved = resolveObjects(review([object("a", 2, 2)]), state.doc);

    assert.equal(resolved.get("a").removed, true);
  });
});

// ---------------------------------------------------------------------------
// Snapping
// ---------------------------------------------------------------------------

describe("snapping", () => {
  const settings = { ...DEFAULT_SNAP, grid: 0 };

  it("returns the input untouched when disabled", () => {
    const result = snapPosition(
      { x: 1.234, y: 2.345 }, object("a", 0, 0), ROOM, [],
      { ...settings, enabled: false },
    );
    assert.deepEqual(result.position, { x: 1.234, y: 2.345 });
  });

  it("puts an object's edge against the wall, not its centre", () => {
    // A 1 m wide sofa snapped to the left wall sits at x = 0.5, not x = 0.
    const result = snapPosition(
      { x: 0.52, y: 2.5 }, object("a", 0, 0, 1, 0.6), ROOM, [], settings,
    );
    assert.ok(Math.abs(result.position.x - 0.5) < 1e-6, `got ${result.position.x}`);
    assert.ok(result.indicators.some((i) => i.kind === "wall"));
  });

  it("accounts for rotation when snapping to a wall", () => {
    // Rotated 90°, the 1 x 0.6 m object presents its 0.6 m side to the wall.
    const rotated = object("a", 0, 0, 1, 0.6, { rotation_z: 90 });
    const result = snapPosition({ x: 0.32, y: 2.5 }, rotated, ROOM, [], settings);
    assert.ok(Math.abs(result.position.x - 0.3) < 1e-6, `got ${result.position.x}`);
  });

  it("leaves a position alone when nothing is within tolerance", () => {
    // Well clear of both walls (x = 0.5 / 5.5) and the room centre (3.0, 2.5).
    const result = snapPosition(
      { x: 2.02, y: 1.53 }, object("a", 0, 0), ROOM, [], settings,
    );
    assert.deepEqual(result.position, { x: 2.02, y: 1.53 });
    assert.deepEqual(result.indicators, []);
  });

  it("aligns with a neighbour's centre", () => {
    const neighbour = object("b", 4, 2.5);
    const result = snapPosition(
      { x: 4.05, y: 1 }, object("a", 0, 0), ROOM, [neighbour], settings,
    );
    assert.ok(Math.abs(result.position.x - 4) < 1e-6);
  });

  it("snaps the axes independently", () => {
    // Against the left wall on x, free on y.
    const result = snapPosition(
      { x: 0.52, y: 3.333 }, object("a", 0, 0, 1, 0.6), ROOM, [], settings,
    );
    assert.ok(Math.abs(result.position.x - 0.5) < 1e-6);
    assert.equal(result.position.y, 3.333);
  });

  it("falls back to the grid when nothing else is near", () => {
    const result = snapPosition(
      { x: 3.02, y: 2.53 }, object("a", 0, 0), ROOM, [],
      { ...DEFAULT_SNAP, grid: 0.25, toWalls: false, toCorners: false,
        toRoomCentre: false, toObjects: false, toAlignmentGuides: false },
    );
    assert.ok(Math.abs(result.position.x - 3.0) < 1e-9);
    assert.ok(Math.abs(result.position.y - 2.5) < 1e-9);
  });

  it("prefers a wall over the grid line beneath it", () => {
    const result = snapPosition(
      { x: 0.5, y: 2.5 }, object("a", 0, 0, 1, 0.6), ROOM, [],
      { ...DEFAULT_SNAP, grid: 0.5 },
    );
    const chosen = result.indicators.find((i) => i.axis === "x");
    assert.equal(chosen.kind, "wall");
  });
});

describe("rotationToNearestWall", () => {
  it("faces an object away from the wall it is nearest", () => {
    // Near the bottom wall (y = 0), the object should face +y, i.e. 0°.
    const angle = rotationToNearestWall(object("a", 3, 0.4), ROOM);
    assert.ok(Math.abs(normaliseDegrees(angle)) < 1e-6, `got ${angle}`);
  });

  it("gives a different answer against a different wall", () => {
    const bottom = rotationToNearestWall(object("a", 3, 0.4), ROOM);
    const left = rotationToNearestWall(object("a", 0.4, 2.5), ROOM);
    assert.notEqual(normaliseDegrees(bottom), normaliseDegrees(left));
  });
});

// ---------------------------------------------------------------------------
// Alignment and distribution
// ---------------------------------------------------------------------------

describe("align", () => {
  const objects = [object("a", 1, 1, 1, 1), object("b", 3, 2, 2, 1), object("c", 5, 3, 0.5, 1)];

  it("does nothing with fewer than two objects", () => {
    assert.deepEqual(align([objects[0]], "left"), {});
  });

  it("aligns left edges, not centres", () => {
    const result = align(objects, "left");
    // Leftmost edge is a's: 1 - 0.5 = 0.5. Every object's left edge lands there.
    assert.ok(Math.abs(result.a.x - 1) < 1e-9);
    assert.ok(Math.abs(result.b.x - 1.5) < 1e-9);
    assert.ok(Math.abs(result.c.x - 0.75) < 1e-9);
  });

  it("aligns right edges", () => {
    const result = align(objects, "right");
    const rightmost = 5 + 0.25;
    for (const [id, position] of Object.entries(result)) {
      const source = objects.find((o) => o.id === id);
      assert.ok(Math.abs(position.x + source.dimensions.width / 2 - rightmost) < 1e-9);
    }
  });

  it("centres on the mean", () => {
    const result = align(objects, "centre-x");
    const mean = (1 + 3 + 5) / 3;
    for (const position of Object.values(result)) {
      assert.ok(Math.abs(position.x - mean) < 1e-9);
    }
  });

  it("leaves the other axis alone", () => {
    const result = align(objects, "left");
    assert.equal(result.a.y, 1);
    assert.equal(result.b.y, 2);
    assert.equal(result.c.y, 3);
  });
});

describe("distribute", () => {
  it("needs at least three objects", () => {
    assert.deepEqual(distribute([object("a", 1, 1), object("b", 3, 1)], "x"), {});
  });

  it("equalises the gaps and leaves the extremes in place", () => {
    const objects = [
      object("a", 0.5, 1, 1, 1),
      object("b", 3, 1, 1, 1),
      object("c", 9.5, 1, 1, 1),
    ];
    const result = distribute(objects, "x");

    assert.equal("a" in result, false, "first must not move");
    assert.equal("c" in result, false, "last must not move");

    // Span between inner edges is 1 to 9; two gaps around a 1 m object.
    const moved = result.b.x;
    const gapBefore = moved - 0.5 - (0.5 + 0.5);
    const gapAfter = 9.5 - 0.5 - (moved + 0.5);
    assert.ok(Math.abs(gapBefore - gapAfter) < 1e-9, `${gapBefore} vs ${gapAfter}`);
  });

  it("sorts before distributing, so input order does not matter", () => {
    const forward = distribute(
      [object("a", 0.5, 1), object("b", 3, 1), object("c", 9.5, 1)], "x",
    );
    const shuffled = distribute(
      [object("c", 9.5, 1), object("a", 0.5, 1), object("b", 3, 1)], "x",
    );
    assert.deepEqual(forward, shuffled);
  });
});
