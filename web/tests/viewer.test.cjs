/**
 * Tests for the viewer's decision logic.
 *
 * Everything covered here is a pure function that decides something the user
 * will notice getting wrong: what a mesh is, whether the roof gets hidden, how
 * fast the camera moves, where it ends up when you fit the model, and whether a
 * malformed manifest takes the viewer down with it.
 *
 * Nothing here touches three.js, a canvas or a GPU. That is the point of the
 * split described in `lib/viewer/classify.ts`: the interesting judgements are
 * pure, so they can be tested in milliseconds, and the parts that genuinely
 * need a renderer contain no judgements to get wrong.
 *
 * Run with `npm test`, which compiles `lib/viewer/` to CommonJS first.
 */

const assert = require("node:assert/strict");
const { describe, it, beforeEach } = require("node:test");

const {
  classifyNode,
  isVisibleInMode,
  looksLikeRoof,
} = require("../.test-build/viewer/lib/viewer/classify.js");

const {
  boxCenter,
  boxSize,
  fitCameraToBox,
  floorProbeHeight,
  interiorSpawn,
  isDegenerateBox,
  lookAngles,
  planToViewer,
  roomViewpoint,
  viewerToPlan,
} = require("../.test-build/viewer/lib/viewer/bounds.js");

const {
  applyLook,
  clampDelta,
  clampPitch,
  dampVelocity,
  desiredVelocity,
  inputFromCodes,
  integrateVertical,
  MOVEMENT,
  NO_INPUT,
  subStepCount,
} = require("../.test-build/viewer/lib/viewer/movement.js");

const {
  parseManifest,
  parseRoom,
  roomAt,
  roomsExtent,
} = require("../.test-build/viewer/lib/viewer/manifest.js");

const {
  DEFAULT_SETTINGS,
  cameraStorageKey,
  clampSetting,
  parseSavedCamera,
  parseSettings,
} = require("../.test-build/viewer/lib/viewer/settings.js");

const { VIEW_MODES } = require("../.test-build/viewer/types/viewer.js");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A node descriptor with sensible blanks. */
function node(overrides = {}) {
  return { name: "", userData: {}, ...overrides };
}

/** A 10 x 3 x 8 m building, Y-up. */
const BUILDING = { min: [0, 0, 0], max: [10, 3, 8] };

// ---------------------------------------------------------------------------

describe("classification", () => {
  it("trusts generator metadata above everything else", () => {
    const result = classifyNode(
      node({ name: "Anonymous", userData: { archx3d_kind: "roof" } }),
    );
    assert.equal(result.kind, "roof");
    assert.equal(result.source, "metadata");
  });

  it("ignores a metadata kind it does not recognise", () => {
    // A future generator emitting a kind this viewer predates must fall through
    // to inference, not classify the mesh as the literal string.
    const result = classifyNode(
      node({ name: "Walls", userData: { archx3d_kind: "gazebo" } }),
    );
    assert.equal(result.kind, "wall");
    assert.equal(result.source, "name");
  });

  it("maps the catalogue group onto a kind", () => {
    for (const [group, kind] of [
      ["furniture", "furniture"],
      ["decor", "decor"],
      ["appliance", "appliance"],
    ]) {
      const result = classifyNode(
        node({ name: "x", userData: { archx3d_group: group } }),
      );
      assert.equal(result.kind, kind, group);
      assert.equal(result.source, "category");
    }
  });

  it("treats an unknown group as furniture rather than dropping it", () => {
    const result = classifyNode(
      node({ name: "x", userData: { archx3d_group: "sculpture" } }),
    );
    assert.equal(result.kind, "furniture");
  });

  it("recognises the generator's shell names", () => {
    assert.equal(classifyNode(node({ name: "Walls" })).kind, "wall");
    assert.equal(classifyNode(node({ name: "Floor" })).kind, "floor");
    assert.equal(classifyNode(node({ name: "Ceiling" })).kind, "roof");
  });

  it("strips Blender's .001 de-duplication suffix", () => {
    assert.equal(classifyNode(node({ name: "Ceiling.001" })).kind, "roof");
    assert.equal(classifyNode(node({ name: "Floor.042" })).kind, "floor");
    // A three-digit suffix only — `Floor.1` is somebody's deliberate name.
    assert.equal(classifyNode(node({ name: "Ceiling.1" })).kind, "unknown");
  });

  it("does not mistake a ceiling fan for the ceiling", () => {
    // The bug a substring match would introduce, and the reason `SHELL_NAMES`
    // is matched on the whole name. Hiding the roof must not take the fan.
    const fan = classifyNode(
      node({
        name: "ceiling_fan_fan_1",
        userData: { archx3d_category: "ceiling_fan", archx3d_group: "decor" },
      }),
    );
    assert.equal(fan.kind, "decor");
    assert.notEqual(fan.kind, "roof");
  });

  it("classifies a ceiling fan with no metadata by category alone", () => {
    const fan = classifyNode(node({ name: "ceiling_fan_1" }));
    assert.notEqual(fan.kind, "roof");
  });

  it("routes luminaire categories to light so Lighting mode shows fixtures", () => {
    const pendant = classifyNode(
      node({
        name: "pendant_light_l3",
        userData: { archx3d_category: "pendant_light", archx3d_group: "decor" },
      }),
    );
    assert.equal(pendant.kind, "light");
  });

  it("recognises builder prefixes and rig light names", () => {
    assert.equal(classifyNode(node({ name: "arch_column_2" })).kind, "structure");
    assert.equal(classifyNode(node({ name: "Light_l1" })).kind, "light");
    assert.equal(classifyNode(node({ name: "Sun_Daylight" })).kind, "light");
    assert.equal(classifyNode(node({ name: "KeyLight" })).kind, "light");
  });

  it("classifies any light datablock as a light", () => {
    const result = classifyNode(node({ name: "Untitled", isLight: true }));
    assert.equal(result.kind, "light");
  });

  it("inherits a kind from an ancestor", () => {
    const result = classifyNode(
      node({ name: "part_3", ancestors: ["arch_stair_1", "Root"] }),
    );
    assert.equal(result.kind, "structure");
    assert.equal(result.source, "hierarchy");
  });

  it("carries provenance fields through", () => {
    const result = classifyNode(
      node({
        name: "sofa_s1",
        userData: {
          archx3d_kind: "furniture",
          archx3d_id: "sofa_1",
          archx3d_category: "sofa",
          archx3d_room: "room_a",
        },
      }),
    );
    assert.equal(result.objectId, "sofa_1");
    assert.equal(result.category, "sofa");
    assert.equal(result.roomId, "room_a");
  });

  it("falls back to unknown rather than guessing", () => {
    const result = classifyNode(node({ name: "Mystery_Object" }));
    assert.equal(result.kind, "unknown");
    assert.equal(result.source, "fallback");
  });
});

describe("geometric roof inference", () => {
  const scene = { min: [0, 0, 0], max: [10, 3, 8] };

  it("accepts a thin plate spanning the plan near the top", () => {
    const ceiling = { min: [0, 2.9, 0], max: [10, 3, 8] };
    assert.equal(looksLikeRoof(ceiling, scene), true);
  });

  it("rejects the floor", () => {
    const floor = { min: [0, 0, 0], max: [10, 0.1, 8] };
    assert.equal(looksLikeRoof(floor, scene), false);
  });

  it("rejects a wall — tall, not a plate", () => {
    const wall = { min: [0, 0, 0], max: [10, 3, 0.15] };
    assert.equal(looksLikeRoof(wall, scene), false);
  });

  it("rejects a high shelf — too small a footprint", () => {
    const shelf = { min: [1, 2.4, 1], max: [2.2, 2.5, 1.4] };
    assert.equal(looksLikeRoof(shelf, scene), false);
  });

  it("rejects a mezzanine floor at mid height", () => {
    // The false positive that would matter most: hiding a floor the user is
    // standing on because it is flat and broad.
    const mezzanine = { min: [0, 1.5, 0], max: [10, 1.6, 8] };
    assert.equal(looksLikeRoof(mezzanine, scene), false);
  });

  it("is safe on a degenerate scene", () => {
    const flat = { min: [0, 0, 0], max: [0, 0, 0] };
    assert.equal(looksLikeRoof(flat, flat), false);
  });

  it("is only consulted when scene bounds are supplied", () => {
    const plate = { min: [0, 2.9, 0], max: [10, 3, 8] };
    // Without an extent the geometric rung is skipped entirely.
    assert.equal(classifyNode(node({ name: "x", bounds: plate })).kind, "unknown");
    assert.equal(
      classifyNode(node({ name: "x", bounds: plate }), scene).kind,
      "roof",
    );
  });
});

describe("view modes", () => {
  it("shows everything in full building", () => {
    const full = VIEW_MODES.find((m) => m.id === "full");
    for (const kind of ["wall", "floor", "furniture", "light", "roof"]) {
      assert.equal(isVisibleInMode(kind, full.shows, false), true, kind);
    }
  });

  it("hides the roof and nothing else in interior", () => {
    const interior = VIEW_MODES.find((m) => m.id === "interior");
    assert.equal(isVisibleInMode("roof", interior.shows, interior.hidesRoof), false);
    assert.equal(isVisibleInMode("wall", interior.shows, interior.hidesRoof), true);
    assert.equal(
      isVisibleInMode("furniture", interior.shows, interior.hidesRoof),
      true,
    );
  });

  it("drops furnishing in structure mode but keeps the shell", () => {
    const structure = VIEW_MODES.find((m) => m.id === "structure");
    assert.equal(isVisibleInMode("wall", structure.shows, false), true);
    assert.equal(isVisibleInMode("structure", structure.shows, false), true);
    assert.equal(isVisibleInMode("furniture", structure.shows, false), false);
    assert.equal(isVisibleInMode("decor", structure.shows, false), false);
  });

  it("keeps the floor in furniture mode so nothing floats", () => {
    const furniture = VIEW_MODES.find((m) => m.id === "furniture");
    assert.equal(isVisibleInMode("furniture", furniture.shows, true), true);
    assert.equal(isVisibleInMode("floor", furniture.shows, true), true);
    assert.equal(isVisibleInMode("wall", furniture.shows, true), false);
  });

  it("keeps the roof hidden in structure mode when the user asked", () => {
    // The toggle and the mode compose: structure mode permits the roof, the
    // user's preference removes it.
    const structure = VIEW_MODES.find((m) => m.id === "structure");
    assert.equal(isVisibleInMode("roof", structure.shows, true), false);
  });

  it("wireframe changes materials, not visibility", () => {
    const wireframe = VIEW_MODES.find((m) => m.id === "wireframe");
    assert.equal(wireframe.wireframe, true);
    assert.equal(wireframe.shows, null);
  });
});

describe("coordinates", () => {
  it("maps plan metres to viewer space by negating Y into Z", () => {
    assert.deepEqual(planToViewer(3, 4, 1.65), [3, 1.65, -4]);
  });

  it("round-trips plan to viewer and back", () => {
    const [x, y] = viewerToPlan(planToViewer(7.5, -2.25, 1));
    assert.equal(x, 7.5);
    assert.equal(y, -2.25);
  });

  it("computes centre and size", () => {
    assert.deepEqual(boxCenter(BUILDING), [5, 1.5, 4]);
    assert.deepEqual(boxSize(BUILDING), [10, 3, 8]);
  });

  it("detects a degenerate box", () => {
    assert.equal(isDegenerateBox({ min: [0, 0, 0], max: [0, 0, 0] }), true);
    assert.equal(
      isDegenerateBox({ min: [0, 0, 0], max: [Infinity, 1, 1] }),
      true,
    );
    assert.equal(isDegenerateBox(BUILDING), false);
  });
});

describe("camera framing", () => {
  it("targets the centre of the model", () => {
    const fit = fitCameraToBox(BUILDING, { fov: 50, aspect: 16 / 9 });
    assert.deepEqual(fit.target, [5, 1.5, 4]);
  });

  it("places the camera outside the model", () => {
    const fit = fitCameraToBox(BUILDING, { fov: 50, aspect: 16 / 9 });
    const dx = fit.position[0] - fit.target[0];
    const dy = fit.position[1] - fit.target[1];
    const dz = fit.position[2] - fit.target[2];
    const distance = Math.hypot(dx, dy, dz);
    assert.ok(distance > 8, `expected to be clear of the building, got ${distance}`);
    assert.ok(fit.position[1] > BUILDING.max[1], "should look down on the model");
  });

  it("pulls back further for a narrow viewport", () => {
    const wide = fitCameraToBox(BUILDING, { fov: 50, aspect: 21 / 9 });
    const narrow = fitCameraToBox(BUILDING, { fov: 50, aspect: 9 / 16 });
    // A portrait phone must not crop a wide building at the sides.
    assert.ok(narrow.distance > wide.distance);
  });

  it("produces a usable frustum rather than dividing by zero", () => {
    const fit = fitCameraToBox({ min: [0, 0, 0], max: [0, 0, 0] });
    assert.ok(Number.isFinite(fit.distance));
    assert.ok(fit.near > 0);
    assert.ok(fit.far > fit.near);
  });

  it("keeps near close enough to stand beside a wall in a large building", () => {
    const tower = { min: [0, 0, 0], max: [80, 40, 60] };
    const fit = fitCameraToBox(tower);
    assert.ok(fit.near <= 0.1, `near was ${fit.near}`);
  });
});

describe("walk entry", () => {
  it("spawns at eye height above the floor", () => {
    const spawn = interiorSpawn(BUILDING, { eyeHeight: 1.7 });
    assert.equal(spawn[1], 1.7);
  });

  it("prefers a supplied room centre over the plan centroid", () => {
    const spawn = interiorSpawn(BUILDING, { eyeHeight: 1.6, preferred: [2, 6] });
    assert.deepEqual(spawn, [2, 1.6, -6]);
  });

  it("probes for the floor from the feet, not from above the head", () => {
    // The regression: probing from `eyeY + 3` starts the ray above a 3 m
    // ceiling, so it hits the roof and the camera ends up standing on it.
    const eyeHeight = 1.65;
    const spawn = interiorSpawn(BUILDING, { eyeHeight });
    const probe = floorProbeHeight(spawn[1], eyeHeight);

    assert.ok(probe > 0, "probe must start above the floor to find it");
    assert.ok(
      probe < 2.4,
      `probe must start well below a storey ceiling, got ${probe}`,
    );
  });

  it("keeps the probe below the ceiling on an upper storey", () => {
    // Standing on the second floor of a 3 m-storey building: the probe must
    // find that storey's slab, not the roof above it.
    const eyeHeight = 1.65;
    const probe = floorProbeHeight(3 + eyeHeight, eyeHeight);
    assert.ok(probe > 3, "probe must start above the storey's own floor");
    assert.ok(probe < 6, "probe must stay under the ceiling above it");
  });

  it("stands back from a room rather than in the middle of it", () => {
    const view = roomViewpoint([0, 0], [6, 4], 1.65, 0);
    // Long axis is X, so the camera backs off along X and looks down the room.
    assert.ok(view.position[0] < view.target[0]);
    assert.equal(view.position[1], 1.65);
  });

  it("looks toward the room centre", () => {
    const view = roomViewpoint([0, 0], [6, 4], 1.65, 0);
    const { yaw, pitch } = lookAngles(view.position, view.target);
    assert.ok(Number.isFinite(yaw));
    assert.ok(Math.abs(pitch) < Math.PI / 2);
  });
});

describe("movement", () => {
  it("reads WASD and the arrow keys from physical key codes", () => {
    const input = inputFromCodes(new Set(["KeyW", "ShiftLeft"]));
    assert.equal(input.forward, true);
    assert.equal(input.run, true);
    assert.equal(input.back, false);

    const arrows = inputFromCodes(new Set(["ArrowUp"]));
    assert.equal(arrows.forward, true);
  });

  it("stands still with no keys down", () => {
    assert.deepEqual(desiredVelocity(NO_INPUT, 0, 3, 2), [0, 0, 0]);
  });

  it("walks along -Z at yaw zero, matching the default camera", () => {
    const v = desiredVelocity({ ...NO_INPUT, forward: true }, 0, 3, 2);
    assert.ok(Math.abs(v[0]) < 1e-9);
    assert.ok(Math.abs(v[2] + 3) < 1e-9);
  });

  it("normalises diagonals so strafing is not faster", () => {
    const straight = desiredVelocity({ ...NO_INPUT, forward: true }, 0, 3, 2);
    const diagonal = desiredVelocity(
      { ...NO_INPUT, forward: true, right: true },
      0,
      3,
      2,
    );
    const speed = (v) => Math.hypot(v[0], v[2]);
    assert.ok(Math.abs(speed(straight) - speed(diagonal)) < 1e-9);
  });

  it("applies the run multiplier", () => {
    const walk = desiredVelocity({ ...NO_INPUT, forward: true }, 0, 3, 2.5);
    const run = desiredVelocity({ ...NO_INPUT, forward: true, run: true }, 0, 3, 2.5);
    assert.ok(Math.abs(Math.hypot(run[0], run[2]) / Math.hypot(walk[0], walk[2]) - 2.5) < 1e-9);
  });

  it("rotates movement with the camera", () => {
    // Facing -X (yaw = +90 degrees) should walk along -X.
    const v = desiredVelocity({ ...NO_INPUT, forward: true }, Math.PI / 2, 3, 2);
    assert.ok(Math.abs(v[0] + 3) < 1e-9);
    assert.ok(Math.abs(v[2]) < 1e-9);
  });

  it("eases toward the target rather than snapping", () => {
    const stepped = dampVelocity([0, 0, 0], [3, 0, 0], 1 / 60);
    assert.ok(stepped[0] > 0 && stepped[0] < 3, `got ${stepped[0]}`);
  });

  it("eases at the same rate whatever the frame rate", () => {
    // One 1/30 s step must land in the same place as two 1/60 s steps, or the
    // camera accelerates faster on a high-refresh display.
    let slow = [0, 0, 0];
    slow = dampVelocity(slow, [3, 0, 0], 1 / 30);

    let fast = [0, 0, 0];
    fast = dampVelocity(fast, [3, 0, 0], 1 / 60);
    fast = dampVelocity(fast, [3, 0, 0], 1 / 60);

    assert.ok(Math.abs(slow[0] - fast[0]) < 1e-9, `${slow[0]} vs ${fast[0]}`);
  });

  it("stops faster than it starts", () => {
    assert.ok(MOVEMENT.deceleration > MOVEMENT.acceleration);
  });

  it("accelerates downward when airborne", () => {
    const after = integrateVertical(0, 1 / 60, {
      grounded: false,
      jumpRequested: false,
      jumpEnabled: false,
      gravityEnabled: true,
    });
    assert.ok(after < 0);
  });

  it("caps the fall speed so nothing tunnels through the floor", () => {
    let speed = 0;
    for (let i = 0; i < 600; i += 1) {
      speed = integrateVertical(speed, 1 / 60, {
        grounded: false,
        jumpRequested: false,
        jumpEnabled: false,
        gravityEnabled: true,
      });
    }
    assert.ok(speed >= -MOVEMENT.maxFallSpeed);
  });

  it("ignores jump when jumping is disabled", () => {
    const after = integrateVertical(0, 1 / 60, {
      grounded: true,
      jumpRequested: true,
      jumpEnabled: false,
      gravityEnabled: true,
    });
    assert.ok(after <= 0, "must not launch when jumping is off");
  });

  it("jumps when enabled and grounded, but not mid-air", () => {
    const grounded = integrateVertical(0, 1 / 60, {
      grounded: true,
      jumpRequested: true,
      jumpEnabled: true,
      gravityEnabled: true,
    });
    assert.equal(grounded, MOVEMENT.jumpSpeed);

    const airborne = integrateVertical(1, 1 / 60, {
      grounded: false,
      jumpRequested: true,
      jumpEnabled: true,
      gravityEnabled: true,
    });
    assert.ok(airborne < 1, "holding jump must not let the camera climb");
  });

  it("clamps pitch short of vertical", () => {
    assert.ok(clampPitch(10) < Math.PI / 2);
    assert.ok(clampPitch(-10) > -Math.PI / 2);
    assert.equal(clampPitch(0), 0);
  });

  it("inverts mouse motion into a look direction", () => {
    const looked = applyLook(0, 0, 100, 50, 0.002);
    assert.ok(looked.yaw < 0, "moving the mouse right turns right");
    assert.ok(looked.pitch < 0, "moving the mouse down looks down");
  });

  it("splits a long move into sub-steps to prevent tunnelling", () => {
    assert.equal(subStepCount(1 / 60, 2), 1);
    assert.ok(subStepCount(0.5, 8) > 1);
    assert.ok(subStepCount(5, 20) <= MOVEMENT.maxSubSteps);
  });

  it("clamps a frame delta after a stall", () => {
    // A tab restored after ten seconds must not simulate ten seconds of fall.
    assert.equal(clampDelta(10), 0.1);
    assert.equal(clampDelta(-1), 0);
    assert.equal(clampDelta(Number.NaN), 0);
  });
});

describe("scene manifest", () => {
  const room = {
    id: "room_a",
    name: "Living Room",
    room_type: "living_room",
    area_m2: 24,
    ceiling_height: 2.7,
    bounds_min: [0, 0],
    bounds_max: [6, 4],
    polygon: [[0, 0], [6, 0], [6, 4], [0, 4]],
    connected_to: ["room_b"],
    object_count: 9,
  };

  it("parses the JSON string Blender custom properties travel as", () => {
    const manifest = parseManifest({
      archx3d: JSON.stringify({ version: "1.0", rooms: [room] }),
    });
    assert.equal(manifest.rooms.length, 1);
    assert.equal(manifest.rooms[0].name, "Living Room");
  });

  it("accepts an already-parsed object too", () => {
    const manifest = parseManifest({ archx3d: { version: "1.0", rooms: [room] } });
    assert.equal(manifest.rooms.length, 1);
  });

  it("returns an empty manifest for a model with no metadata", () => {
    // The graceful-degradation case: an older GLB must open, minus room
    // navigation and the minimap.
    assert.deepEqual(parseManifest({}).rooms, []);
    assert.deepEqual(parseManifest(null).rooms, []);
    assert.deepEqual(parseManifest(undefined).rooms, []);
  });

  it("survives a corrupt manifest without throwing", () => {
    assert.deepEqual(parseManifest({ archx3d: "{not json" }).rooms, []);
    assert.deepEqual(parseManifest({ archx3d: 42 }).rooms, []);
    assert.deepEqual(parseManifest({ archx3d: [] }).rooms, []);
  });

  it("drops rooms with no usable footprint rather than listing dead entries", () => {
    assert.equal(parseRoom({ id: "x" }), null);
    assert.equal(parseRoom({ id: "x", bounds_min: [0, 0], bounds_max: [0, 0] }), null);
    assert.equal(parseRoom({ bounds_min: [0, 0], bounds_max: [1, 1] }), null);
  });

  it("keeps a room whose polygon is missing, falling back to its bounds", () => {
    const parsed = parseRoom({ ...room, polygon: undefined });
    assert.ok(parsed);
    assert.deepEqual(parsed.polygon, []);
    assert.deepEqual(parsed.bounds_max, [6, 4]);
  });

  it("derives a readable name from the room type", () => {
    const parsed = parseRoom({ ...room, name: undefined });
    assert.equal(parsed.name, "Living Room");
  });

  it("sorts rooms largest first", () => {
    const manifest = parseManifest({
      archx3d: {
        rooms: [
          { ...room, id: "small", area_m2: 5 },
          { ...room, id: "big", area_m2: 40 },
        ],
      },
    });
    assert.equal(manifest.rooms[0].id, "big");
  });

  it("computes the plan extent for the minimap", () => {
    const extent = roomsExtent([
      { ...room, bounds_min: [0, 0], bounds_max: [6, 4] },
      { ...room, bounds_min: [6, 0], bounds_max: [10, 3] },
    ]);
    assert.deepEqual(extent.min, [0, 0]);
    assert.deepEqual(extent.max, [10, 4]);
  });

  it("returns no extent with no rooms", () => {
    assert.equal(roomsExtent([]), null);
  });

  it("finds the room containing a plan point", () => {
    const rooms = [
      { ...room, id: "a", bounds_min: [0, 0], bounds_max: [6, 4] },
      { ...room, id: "b", bounds_min: [6, 0], bounds_max: [10, 4] },
    ];
    assert.equal(roomAt(rooms, 3, 2).id, "a");
    assert.equal(roomAt(rooms, 8, 2).id, "b");
    assert.equal(roomAt(rooms, 20, 20), null);
  });
});

describe("settings", () => {
  it("returns the defaults for anything unreadable", () => {
    assert.deepEqual(parseSettings(null), DEFAULT_SETTINGS);
    assert.deepEqual(parseSettings("nonsense"), DEFAULT_SETTINGS);
    assert.deepEqual(parseSettings(42), DEFAULT_SETTINGS);
  });

  it("keeps the rest of a user's preferences when one field is bad", () => {
    const parsed = parseSettings({ walkSpeed: null, showRoof: false });
    assert.equal(parsed.walkSpeed, DEFAULT_SETTINGS.walkSpeed);
    assert.equal(parsed.showRoof, false);
  });

  it("clamps a value that would break the viewer", () => {
    assert.equal(parseSettings({ walkSpeed: 1e9 }).walkSpeed, 12);
    assert.equal(parseSettings({ walkSpeed: -5 }).walkSpeed, 0.5);
    assert.equal(parseSettings({ eyeHeight: Number.NaN }).eyeHeight, DEFAULT_SETTINGS.eyeHeight);
  });

  it("rejects an unrecognised view mode or environment", () => {
    assert.equal(parseSettings({ viewMode: "xray" }).viewMode, "full");
    assert.equal(parseSettings({ environment: "mars" }).environment, "studio");
  });

  it("defaults jumping off", () => {
    assert.equal(DEFAULT_SETTINGS.jumpEnabled, false);
  });

  it("defaults collision on", () => {
    assert.equal(DEFAULT_SETTINGS.collisionEnabled, true);
  });

  it("clamps individual settings to their documented bounds", () => {
    assert.equal(clampSetting("runMultiplier", 100), 5);
    assert.equal(clampSetting("exposure", 0), 0.2);
    assert.equal(clampSetting("ambientIntensity", -1), 0);
  });

  it("keys stored camera poses per model", () => {
    const a = cameraStorageKey("http://host/a/model.glb");
    const b = cameraStorageKey("http://host/b/model.glb");
    assert.notEqual(a, b);
  });

  it("drops a malformed saved camera instead of using it", () => {
    // Restoring a partial pose would put the camera at NaN and blank the screen.
    assert.deepEqual(parseSavedCamera({ orbit: { position: [1, 2] } }).orbit, undefined);
    assert.deepEqual(parseSavedCamera({ orbit: { position: "x" } }).orbit, undefined);
    assert.deepEqual(parseSavedCamera(null), {});
  });

  it("accepts a well-formed saved camera", () => {
    const saved = parseSavedCamera({
      orbit: { position: [1, 2, 3], target: [0, 0, 0] },
      walk: { position: [4, 1.6, 5], yaw: 0.5, pitch: -0.1 },
      mode: "walk",
    });
    assert.deepEqual(saved.orbit.position, [1, 2, 3]);
    assert.equal(saved.walk.yaw, 0.5);
    assert.equal(saved.mode, "walk");
  });
});

// ---------------------------------------------------------------------------
// Adaptive render quality
// ---------------------------------------------------------------------------

const {
  planQuality,
  HEAVY_MESH_COUNT,
  VERY_HEAVY_MESH_COUNT,
} = require("../.test-build/viewer/lib/viewer/quality.js");

describe("adaptive quality", () => {
  const on = { shadows: true, autoQuality: true };

  it("leaves a small model at full quality and says nothing", () => {
    const plan = planQuality({ meshes: 40, ...on });
    assert.equal(plan.shadows, true);
    assert.deepEqual(plan.dpr, [1, 2]);
    assert.equal(plan.reducedReason, null);
  });

  it("does not decide anything before the model is measured", () => {
    const plan = planQuality({ meshes: null, ...on });
    assert.equal(plan.shadows, true);
    assert.equal(plan.reducedReason, null);
  });

  it("drops shadows on a heavy model — the pass that doubles draw calls", () => {
    const plan = planQuality({ meshes: HEAVY_MESH_COUNT, ...on });
    assert.equal(plan.shadows, false);
    assert.ok(plan.dpr[1] < 2, "should also lower the pixel count");
  });

  it("renders a very heavy model at 1x", () => {
    const plan = planQuality({ meshes: VERY_HEAVY_MESH_COUNT, ...on });
    assert.deepEqual(plan.dpr, [1, 1]);
    assert.equal(plan.shadows, false);
  });

  it("explains every reduction it makes", () => {
    const plan = planQuality({ meshes: 448, ...on });
    assert.ok(plan.reducedReason, "a silent downgrade is a bug report waiting to happen");
    assert.match(plan.reducedReason, /448/);
  });

  it("says nothing when it changed nothing the user would notice", () => {
    // Shadows already off, so turning them off is not a reduction to report.
    const plan = planQuality({ meshes: 300, shadows: false, autoQuality: true });
    assert.equal(plan.reducedReason, null);
  });

  it("obeys the user when automatic reduction is off", () => {
    const plan = planQuality({ meshes: 5000, shadows: true, autoQuality: false });
    assert.equal(plan.shadows, true);
    assert.deepEqual(plan.dpr, [1, 2]);
    assert.equal(plan.reducedReason, null);
  });
});
