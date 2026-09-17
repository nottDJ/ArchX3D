/**
 * Walk mode: where the camera stands, and that it stays standing (G5).
 *
 * The bug: on a real two-storey drawing the default walk view was black. The
 * renderer was healthy; the camera was 107 m below the ground and falling. The
 * largest room was upstairs, the spawn put the camera at ground height beneath
 * it, the ground slab had a void there, and gravity ran forever.
 *
 * These tests rebuild that configuration in miniature and drive the *real*
 * BVH collider, the real movement integrator and the real spawn logic against
 * it — no GPU, no mocks of the thing that failed.
 *
 *     plan (metres, y up the page)        storeys
 *
 *     12 ┌────┬───────────┐               ground slab: the L of "hall" and
 *        │    │ UPSTAIRS  │               "living" only — nothing under the
 *        │hall│ 48 m², on │               upstairs room, which is also the
 *        │    │ the first │               largest room in the building
 *      4 │    │ floor     │
 *        ├────┴───────────┤               first-floor slab: the whole plan
 *        │     living     │               at 3 m
 *      0 └────────────────┘
 *        0    4           10
 */

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const THREE = require("three");

const { buildCollider } = require("../.test-build/viewer/lib/viewer/collider.js");
const { integrateVertical } = require("../.test-build/viewer/lib/viewer/movement.js");
const { interiorSpawn } = require("../.test-build/viewer/lib/viewer/bounds.js");
const { cameraStorageKey } = require("../.test-build/viewer/lib/viewer/settings.js");
const { CANDELA_PER_VIEWER_UNIT, toViewerLightUnits } = require("../.test-build/viewer/lib/viewer/lights.js");
const {
  FALL_OUT_DEPTH,
  SIGHT_SAMPLES,
  chooseWalkSpawn,
  hasFallenOut,
  interiorPoint,
  openestYaw,
  pointInPolygon,
  spawnCandidates,
  supportAt,
} = require("../.test-build/viewer/lib/viewer/spawn.js");

const EYE = 1.65;
const SLAB = 0.12;

/** A horizontal slab whose top face is at `top`, over plan rectangle [x0,x1]x[y0,y1]. */
function slab(x0, x1, y0, y1, top) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(x1 - x0, SLAB, y1 - y0));
  // Plan y maps to viewer -z; see planToViewer.
  mesh.position.set((x0 + x1) / 2, top - SLAB / 2, -(y0 + y1) / 2);
  mesh.updateMatrixWorld(true);
  return mesh;
}

function wall(x0, x1, y0, y1, height) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(x1 - x0, height, y1 - y0));
  mesh.position.set((x0 + x1) / 2, height / 2, -(y0 + y1) / 2);
  mesh.updateMatrixWorld(true);
  return mesh;
}

function rect(id, x0, x1, y0, y1, elevation) {
  return {
    id,
    name: id,
    room_type: "room",
    area_m2: (x1 - x0) * (y1 - y0),
    ceiling_height: 3,
    elevation,
    bounds_min: [x0, y0],
    bounds_max: [x1, y1],
    polygon: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
    connected_to: [],
    object_count: 0,
  };
}

/** The G5 building. Bounds as the viewer measures them: bottom of the ground slab. */
function g5Building() {
  const meshes = [
    slab(0, 10, 0, 4, 0), //   living — ground
    slab(0, 4, 4, 12, 0), //   hall   — ground (nothing at 4-10 x 4-12)
    slab(0, 10, 0, 12, 3), //  first floor, whole plan
    slab(0, 10, 0, 12, 6), //  roof
  ];
  const collider = buildCollider(meshes);
  const box = { min: [0, -SLAB, -12], max: [10, 6, 0] };
  const rooms = [
    // Largest room in the building, and the one the old spawn chose: upstairs.
    rect("upstairs", 4, 10, 4, 12, 3), // 48 m²
    rect("living", 0, 10, 0, 4, 0), //   40 m²
    rect("hall", 0, 4, 4, 12, 0), //     32 m²
  ];
  // The manifest arrives sorted largest-first; make sure that is the case here
  // too so the test does not pass by accident of ordering.
  rooms.sort((a, b) => b.area_m2 - a.area_m2);
  return { collider, box, rooms };
}

/** Run the walk controller's vertical loop: gravity plus capsule resolution. */
function stand(collider, start, seconds = 5, dt = 1 / 60) {
  const eye = new THREE.Vector3(...start);
  const velocity = new THREE.Vector3();
  let grounded = false;
  let lowest = eye.y;
  for (let t = 0; t < seconds; t += dt) {
    velocity.y = integrateVertical(velocity.y, dt, {
      grounded,
      jumpRequested: false,
      jumpEnabled: false,
      gravityEnabled: true,
    });
    grounded = collider.resolve(eye, velocity, dt, { height: EYE, gravity: true }).grounded;
    lowest = Math.min(lowest, eye.y);
    if (!Number.isFinite(eye.y) || eye.y < -500) break;
  }
  return { y: eye.y, lowest };
}

// ---------------------------------------------------------------------------

describe("G5 — the black walk view, reproduced", () => {
  it("the old spawn stood over a void, and gravity dropped it out of the model", () => {
    // Characterises the root cause with the real collider, so a future change
    // that brings the old choice back fails here with the reason attached.
    const { collider, box, rooms } = g5Building();
    const largest = rooms[0];
    assert.equal(largest.id, "upstairs");
    const old = interiorSpawn(box, {
      eyeHeight: EYE,
      preferred: [
        (largest.bounds_min[0] + largest.bounds_max[0]) / 2,
        (largest.bounds_min[1] + largest.bounds_max[1]) / 2,
      ],
    });

    assert.equal(supportAt(collider, old, EYE), null, "no floor under the old spawn");
    const { y } = stand(collider, old, 3);
    assert.ok(hasFallenOut(y, box), `camera should have fallen out, ended at y=${y}`);
  });

  it("chooses a spawn on the storey you walk in on, with a floor under it", () => {
    const { collider, box, rooms } = g5Building();
    const spawn = chooseWalkSpawn(spawnCandidates(rooms, box, EYE), collider, EYE);

    assert.equal(spawn.supported, true);
    assert.equal(spawn.roomId, "living", "largest room on the lowest storey");
    assert.ok(Math.abs(spawn.position[1] - (0 + EYE)) < 1e-6, `eye at ${spawn.position[1]}`);
  });

  it("stays standing: five seconds of gravity from the chosen spawn does not fall", () => {
    const { collider, box, rooms } = g5Building();
    const spawn = chooseWalkSpawn(spawnCandidates(rooms, box, EYE), collider, EYE);
    const { y, lowest } = stand(collider, spawn.position, 5);

    assert.ok(Math.abs(y - EYE) < 0.05, `settled at y=${y}, expected ~${EYE}`);
    assert.ok(lowest > EYE - 0.1, `dipped to y=${lowest}`);
  });

  it("puts an upper-storey room at its own elevation when that is all there is", () => {
    const { collider, box } = g5Building();
    const onlyUpstairs = [rect("upstairs", 4, 10, 4, 12, 3)];
    const spawn = chooseWalkSpawn(spawnCandidates(onlyUpstairs, box, EYE), collider, EYE);

    assert.equal(spawn.supported, true);
    assert.equal(spawn.roomId, "upstairs");
    assert.ok(Math.abs(spawn.position[1] - (3 + EYE)) < 1e-6, `eye at ${spawn.position[1]}`);
    const { y } = stand(collider, spawn.position, 3);
    assert.ok(Math.abs(y - (3 + EYE)) < 0.05, `upstairs settled at y=${y}`);
  });

  it("skips a room with no floor under it and takes the next, for a manifest with no elevations", () => {
    // A 1.0 manifest reads every room as ground level. The upstairs room then
    // looks like a ground-floor room over a void; it must be passed over.
    const { collider, box } = g5Building();
    const flat = [
      rect("upstairs", 4, 10, 4, 12, 0),
      rect("hall", 0, 4, 4, 12, 0),
    ];
    const spawn = chooseWalkSpawn(spawnCandidates(flat, box, EYE), collider, EYE);

    assert.equal(spawn.supported, true);
    assert.equal(spawn.roomId, "hall");
  });

  it("says so when nothing has a floor, so the caller flies instead of falling", () => {
    const collider = buildCollider([wall(0, 0.2, 0, 10, 3), wall(9.8, 10, 0, 10, 3)]);
    const box = { min: [0, 0, -10], max: [10, 3, 0] };
    const spawn = chooseWalkSpawn(
      spawnCandidates([rect("r", 1, 9, 1, 9, 0)], box, EYE),
      collider,
      EYE,
    );
    assert.equal(spawn.supported, false);
  });

  it("returns the best candidate unconfirmed while the collider is still being built", () => {
    const { box, rooms } = g5Building();
    const spawn = chooseWalkSpawn(spawnCandidates(rooms, box, EYE), null, EYE);
    assert.equal(spawn.supported, false);
    assert.equal(spawn.roomId, "living");
  });
});

describe("walk support and recovery", () => {
  it("finds the floor under someone standing, and nothing over a void", () => {
    const { collider } = g5Building();
    assert.ok(Math.abs(supportAt(collider, [2, EYE, -6], EYE)) < 1e-6);
    assert.equal(supportAt(collider, [7, EYE, -9], EYE), null);
    assert.equal(supportAt(collider, [Number.NaN, EYE, 0], EYE), null);
  });

  it("honours the maximum drop when looking for the floor", () => {
    // groundBelow used to ignore maxDrop, so any surface arbitrarily far below
    // counted as "the floor" and a spawn over a void could look supported.
    const { collider } = g5Building();
    assert.equal(collider.groundBelow(2, -6, 5, 1), null, "slab top at 0 is 5 m down");
    assert.ok(Math.abs(collider.groundBelow(2, -6, 1, 5)) < 1e-6);
  });

  it("calls a camera that has dropped a storey below the model fallen out", () => {
    const box = { min: [0, -0.12, 0], max: [10, 6, 10] };
    assert.equal(hasFallenOut(-0.12 - FALL_OUT_DEPTH + 0.01, box), false);
    assert.equal(hasFallenOut(-0.12 - FALL_OUT_DEPTH - 0.01, box), true);
    assert.equal(hasFallenOut(Number.NaN, box), true, "NaN is never somewhere to render from");
    assert.equal(hasFallenOut(-1000, null), false, "no bounds, no judgement");
  });
});

describe("spawn geometry", () => {
  it("stands inside an L-shaped room, not at its bounding-box centre", () => {
    const room = {
      polygon: [[0, 0], [10, 0], [10, 2], [2, 2], [2, 10], [0, 10]],
      bounds_min: [0, 0],
      bounds_max: [10, 10],
    };
    assert.equal(pointInPolygon([5, 5], room.polygon), false, "box centre is outside");
    const p = interiorPoint(room);
    assert.equal(pointInPolygon(p, room.polygon), true, `interior point ${p}`);
  });

  it("orders candidates lowest storey first, largest room first within it", () => {
    const { box, rooms } = g5Building();
    const ids = spawnCandidates(rooms, box, EYE).map((c) => c.roomId);
    assert.deepEqual(ids, ["living", "hall", "upstairs", null]);
  });
});

/** A solid between two heights over a plan rectangle — a lintel, a sill wall. */
function block(x0, x1, y0, y1, bottom, top) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(x1 - x0, top - bottom, y1 - y0));
  mesh.position.set((x0 + x1) / 2, (bottom + top) / 2, -(y0 + y1) / 2);
  mesh.updateMatrixWorld(true);
  return mesh;
}

/** Plan direction a yaw looks along, in the convention of `lookAngles`. */
function planHeading(yaw) {
  // Viewer forward is (-sin, -cos) in (x, z); plan y is viewer -z.
  return [-Math.sin(yaw), Math.cos(yaw)];
}

function turnBetween(a, b) {
  const d = Math.abs(a - b) % (Math.PI * 2);
  return d > Math.PI ? Math.PI * 2 - d : d;
}

describe("which way a fresh walk spawn faces", () => {
  it("looks down the length of a room rather than at the wall toward the building's centre", () => {
    // A 12 x 3 m room along the south edge of a 20 x 20 m building. From its
    // west end the building's centre lies up and to the right — through the
    // room's north wall, 2 m away. The room's own view runs 11 m east.
    const W = 0.2;
    const collider = buildCollider([
      wall(0, 12, 0 - W, 0, 3), //    south
      wall(0, 12, 3, 3 + W, 3), //    north
      wall(0 - W, 0, 0, 3, 3), //     west
      wall(12, 12 + W, 0, 3, 3), //   east
    ]);
    const box = { min: [0, 0, -20], max: [20, 3, 0] };
    const eye = [1, EYE, -1.5];
    const towardCentre = Math.atan2(-(10 - 1), -(-10 - -1.5));

    const yaw = openestYaw(collider, eye, towardCentre, box);
    const [hx, hy] = planHeading(yaw);
    assert.ok(hx > 0.9, `should look east along the room, heading (${hx.toFixed(2)}, ${hy.toFixed(2)})`);

    const blind = collider.clearance(eye[0], eye[1], eye[2], -Math.sin(towardCentre), -Math.cos(towardCentre), 20);
    assert.ok(blind < 2.5, `the old facing met the north wall ${blind.toFixed(2)} m away`);
  });

  it("faces into the building, not out of a wide window, when it knows the building's extent", () => {
    // 12 x 6 m room; the south wall is an 8 m window at eye height. Standing
    // 1 m in from it, a sightline south runs on outdoors forever.
    const W = 0.2;
    const meshes = [
      wall(0, 12, 6, 6 + W, 3), //                north
      wall(0 - W, 0, 0, 6, 3), //                 west
      wall(12, 12 + W, 0, 6, 3), //               east
      wall(0, 2, 0 - W, 0, 3), //                 south, west pier
      wall(10, 12, 0 - W, 0, 3), //               south, east pier
      block(2, 10, 0 - W, 0, 0, 0.9), //          sill wall
      block(2, 10, 0 - W, 0, 2.2, 3), //          lintel
    ];
    const collider = buildCollider(meshes);
    const eye = [6, EYE, -1];

    // "Out" and "in" are the half-planes either side of the window wall; which
    // diagonal wins inside each is a detail of the room's proportions.
    const outdoors = openestYaw(collider, eye, 0, null);
    const out = planHeading(outdoors);
    assert.ok(out[1] < -0.5, `without the extent, the window's endless view wins: (${out})`);

    const box = { min: [0 - W, 0, -(6 + W)], max: [12 + W, 3, 0 + W] };
    const indoors = openestYaw(collider, eye, 0, box);
    const inside = planHeading(indoors);
    assert.ok(inside[1] > 0.5, `with it, the room ahead wins: (${inside})`);
  });

  it("keeps facing the building's centre where every direction is equally open", () => {
    const collider = buildCollider([block(-50, -49, -50, -49, 0, 0.1)]);
    const preferred = 0.3;
    const yaw = openestYaw(collider, [0, EYE, 0], preferred, null);
    assert.ok(turnBetween(yaw, preferred) <= Math.PI / SIGHT_SAMPLES + 1e-9, `turned to ${yaw}`);
  });

  it("does not turn a camera it cannot place", () => {
    const collider = buildCollider([block(0, 1, 0, 1, 0, 1)]);
    assert.equal(openestYaw(collider, [NaN, EYE, 0], 1.25, null), 1.25);
  });
});

describe("the white walk view: the file's lamps are in candela", () => {
  const exported = (watts) => (watts * 683) / (4 * Math.PI);

  it("brings each lamp back to its watts, the scale the viewer's rig is lit in", () => {
    // The live numbers from a generated model: an 18 W table lamp arrived as
    // 978.3 cd and a 35 W floor lamp as 1902.3 cd, against a sun of 1.15.
    const root = new THREE.Group();
    const room = new THREE.Group();
    const table = new THREE.PointLight(0xffffff, exported(18));
    const floor = new THREE.SpotLight(0xffffff, exported(35));
    const sun = new THREE.DirectionalLight(0xffffff, 1.15);
    room.add(table, floor);
    root.add(room, sun);

    assert.ok(Math.abs(table.intensity - 978.3) < 0.1, `as filed: ${table.intensity}`);
    assert.equal(toViewerLightUnits(root), 2, "point and spot lights only");
    assert.ok(Math.abs(table.intensity - 18) < 1e-9, `table lamp ${table.intensity}`);
    assert.ok(Math.abs(floor.intensity - 35) < 1e-9, `floor lamp ${floor.intensity}`);
    assert.equal(sun.intensity, 1.15, "directional light is left in lux");
    assert.ok(Math.abs(CANDELA_PER_VIEWER_UNIT - 54.35) < 0.01);
  });

  it("converts once: the same scene passed through twice keeps its lamps", () => {
    const root = new THREE.Group();
    const lamp = new THREE.PointLight(0xffffff, exported(20));
    root.add(lamp);
    toViewerLightUnits(root);
    toViewerLightUnits(root);
    assert.ok(Math.abs(lamp.intensity - 20) < 1e-9, `after two passes: ${lamp.intensity}`);
  });
});

describe("camera persistence", () => {
  it("finds a saved pose again after the desktop backend moves to a new port", () => {
    const a = cameraStorageKey("http://127.0.0.1:50814/api/projects/abc/model.glb");
    const b = cameraStorageKey("http://127.0.0.1:64077/api/projects/abc/model.glb");
    assert.equal(a, b);
  });

  it("still keeps different models and different jobs apart", () => {
    assert.notEqual(
      cameraStorageKey("http://127.0.0.1:1/api/projects/a/model.glb"),
      cameraStorageKey("http://127.0.0.1:1/api/projects/b/model.glb"),
    );
    assert.notEqual(
      cameraStorageKey("http://h/output/model.glb?job=1"),
      cameraStorageKey("http://h/output/model.glb?job=2"),
    );
  });
});

describe("repeated falls", () => {
  it("respawns after the first fall and flies after the second, so a fall is never a loop", () => {
    const { afterFallingOut } = require("../.test-build/viewer/lib/viewer/spawn.js");
    assert.equal(afterFallingOut(1), "respawn");
    assert.equal(afterFallingOut(2), "fly");
    assert.equal(afterFallingOut(5), "fly");
  });
});

describe("furniture is not solid (the primitive split)", () => {
  const { classifyNode } = require("../.test-build/viewer/lib/viewer/classify.js");
  const COLLIDING = new Set(["wall", "floor", "roof", "structure", "opening", "unknown"]);

  // How GLTFLoader delivers a two-material sofa: a tagged group, untagged meshes.
  const group = { archx3d_kind: "furniture", archx3d_room: "r13", archx3d_id: "sofa_0" };

  it("a mesh inherits its kind from the tagged group glTF split it out of", () => {
    const c = classifyNode({
      name: "sofa_r13__proc_sofa_0Mesh_1",
      userData: {},
      ancestors: ["sofa_r13__proc_sofa_0", "Scene"],
      ancestorData: [group, {}],
    });
    assert.equal(c.kind, "furniture");
    assert.equal(COLLIDING.has(c.kind), false, "a sofa must not stop the walk camera");
  });

  it("inherits the room and object ids too, so per-room isolation includes every part", () => {
    const c = classifyNode({ name: "x_Mesh", userData: {}, ancestorData: [group] });
    assert.equal(c.roomId, "r13");
    assert.equal(c.objectId, "sofa_0");
  });

  it("a mesh's own metadata still wins over its parent's", () => {
    const c = classifyNode({
      name: "Walls",
      userData: { archx3d_kind: "wall" },
      ancestorData: [group],
    });
    assert.equal(c.kind, "wall");
  });

  it("without ancestor data it still falls back as before — the old behaviour, documented", () => {
    const c = classifyNode({ name: "sofa_r13__proc_sofa_0Mesh_1", userData: {}, ancestors: ["sofa_r13__proc_sofa_0"] });
    assert.equal(c.kind, "unknown");
  });
});
