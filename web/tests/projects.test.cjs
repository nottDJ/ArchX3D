/**
 * The project list is rebuilt from disk; the browser only adds preferences (P2).
 *
 * Before: a project was listed only if this browser's localStorage remembered
 * creating it. Wiping the WebView profile left every project folder on disk and
 * none of them reachable. These tests pin the merge that fixes it.
 */

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");

const {
  adoptServerProjects,
  defaultName,
  recordFromManifest,
} = require("../.test-build/viewer/lib/projectIndex.js");

const NOW = "2026-09-14T20:00:00.000Z";

function manifest(id, filename = `${id}.dxf`, extra = {}) {
  return {
    project_id: id,
    created_at: "2026-09-10T10:00:00",
    updated_at: "2026-09-12T10:00:00",
    dxf: { filename, bytes: 1000 },
    images: [],
    stage: "generated",
    ...extra,
  };
}

describe("adopting projects found on disk", () => {
  it("lists every on-disk project when the browser remembers nothing — the wiped-profile case", () => {
    const merged = adoptServerProjects(
      [],
      [manifest("a1", "final_plan_19th_may.dxf"), manifest("b2", "clinic.dxf")],
      new Set(),
      NOW,
    );
    assert.deepEqual(merged.map((r) => r.id), ["a1", "b2"]);
    assert.equal(merged[0].name, "final plan 19th may");
    assert.equal(merged[0].stage, "generated");
  });

  it("keeps the user's name, pin and order for projects it already knows", () => {
    const known = [{ id: "a1", name: "Mum's house", createdAt: "x", openedAt: "y", pinned: true }];
    const merged = adoptServerProjects(known, [manifest("b2"), manifest("a1")], new Set(), NOW);
    assert.deepEqual(merged.map((r) => r.id), ["a1", "b2"]);
    assert.equal(merged[0].name, "Mum's house");
    assert.equal(merged[0].pinned, true);
  });

  it("does not bring back a project the user hid, even though its folder is still there", () => {
    const merged = adoptServerProjects([], [manifest("a1"), manifest("b2")], new Set(["a1"]), NOW);
    assert.deepEqual(merged.map((r) => r.id), ["b2"]);
  });

  it("returns the same array when nothing was adopted, so the caller can skip a write", () => {
    const known = [{ id: "a1", name: "n", createdAt: "x", openedAt: "y", pinned: false }];
    assert.equal(adoptServerProjects(known, [manifest("a1")], new Set(), NOW), known);
    assert.equal(adoptServerProjects(known, [], new Set(), NOW), known);
  });

  it("ignores malformed entries and duplicates rather than listing them", () => {
    const merged = adoptServerProjects(
      [],
      [manifest("a1"), manifest("a1"), { project_id: "" }, null, { stage: "x" }],
      new Set(),
      NOW,
    );
    assert.deepEqual(merged.map((r) => r.id), ["a1"]);
  });
});

describe("a rediscovered project's record", () => {
  it("is named from its drawing and dated from the server, never from now", () => {
    const record = recordFromManifest(manifest("a1", "sba_block-B.dxf"), NOW);
    assert.equal(record.name, "sba block B");
    assert.equal(record.createdAt, "2026-09-10T10:00:00");
    assert.equal(record.openedAt, "2026-09-12T10:00:00", "last change on disk, not an invented open");
    assert.equal(record.pinned, false);
    assert.equal(record.bytes, 1000);
  });

  it("falls back sensibly for a project with no drawing yet", () => {
    const record = recordFromManifest({ project_id: "z9" }, NOW);
    assert.equal(record.name, "Untitled project");
    assert.equal(record.createdAt, NOW);
    assert.equal(defaultName({ project_id: "z9", dxf: { filename: ".dxf" } }), "Untitled project");
  });
});
