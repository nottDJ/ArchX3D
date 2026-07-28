# ArchX3D — Object editor and incremental validation (v2.2)

How the review step became an authoring tool, and what stops a hand-edited
scene from reaching the renderer in an unbuildable state.

---

## 0. Where this sits

```
Upload DXF → Upload images → AI analysis → Validation
                                              │
                                              ▼
                                        ┌───────────┐
                                        │  Review   │  ← you are here
                                        │  + edit   │
                                        └─────┬─────┘
                                              │
                                   Incremental validation
                                              │
                                              ▼
                                         Generation
                                              │
                                              ▼
                                        Walkthrough
```

Geometry always comes from the DXF. Appearance comes from the reference
images. The editor changes neither of those sources — it edits the *scene
graph* that sits between them, which is the only artefact both the review UI
and the Blender generator read.

---

## 1. The editing model

### One document, not scattered state

Every uncommitted change lives in a single immutable `EditorDoc`
([`web/lib/editor.ts`](../web/lib/editor.ts)):

```ts
interface EditorDoc {
  removed: string[];                              // object ids
  kept: string[];                                 // confirmed low-confidence
  roomTypes: Record<string, string>;
  overrides: Record<string, ObjectOverride>;      // transform + appearance
  finishes: Record<string, RoomFinishEdit>;       // wall / floor / ceiling
  lights: Record<string, LightOverride>;
  removedLights: string[];
  added: AddObjectSpec[];                         // duplicate and paste
  addedLights: AddLightSpec[];
}
```

This is the central design decision, and it is what makes undo correct. A
batch rotate touches a dozen objects; deleting a table cascades to everything
standing on it. With each edit type in its own `useState` there is no single
"before" to return to. With one document, history is just a stack of
documents — and because a document holds ids and patches rather than the scene
graph itself, snapshotting one per action stays cheap.

History is `past: EditorDoc[]` / `future: EditorDoc[]`, capped by
`HISTORY_LIMIT` (200; raise or set to `Infinity` for unlimited). Selection
changes are deliberately *not* recorded — undo skips past them to the last real
edit, which is what every other editor does.

### Nothing is sent until Apply

`toEdits(doc)` translates the document into the server's `ReviewEdits` payload,
omitting empty collections. Until then the plan, the object list and the
inspector all read the same merged view via `resolveObjects`, so they cannot
show different states of the same object.

---

## 2. Editor capabilities

| Operation | Where | Notes |
|---|---|---|
| Move | drag, arrow keys, X/Y fields | Arrow = 5 cm, Alt+arrow = 1 cm |
| Rotate | rotation arm, field | Snaps to 15°; Alt for free |
| Resize | corner handles, W/D/H fields | Symmetric about the centre |
| Delete / restore | toolbar, `Del`, row button | Cascades to supported objects |
| Duplicate / copy / paste | `Ctrl+D` / `Ctrl+C` / `Ctrl+V` | Offset so the copy is visible |
| Lock / unlock | toolbar, inspector | Pins against automatic correction |
| Change category | inspector | Validated against the catalog |
| Reassign room | inspector | Repositions into the new polygon |
| Swap asset | inspector → Asset | Placement-preserving |
| Object material / colour | inspector → Material | 26 materials, colour picker |
| Wall / floor / ceiling finish | room panel → Finishes | Per-room, not global |
| Ceiling type | room panel → Finishes | 7 types |
| Light kind / power / CCT / height | room panel → Lighting | Clamped to renderable ranges |
| Add / remove light | room panel → Lighting | 10 luminaire types |

### Multi-selection

Shift-click extends, dragging empty space marquee-selects (by object centre —
requiring full enclosure is unusable at furniture scale in a room-sized view),
`Ctrl+A` selects everything or, with a room focused, everything in that room.

Dragging any selected object moves the whole selection by the same delta, so
the arrangement is preserved. Rotation and resize act on **one** object even
inside a multi-selection: a shared pivot is rarely what is wanted and never
what is expected.

The inspector works on one object or many. A field shows a value when the
selection agrees and `mixed` when it does not; editing writes to all of them.

### Snapping

[`web/lib/snapping.ts`](../web/lib/snapping.ts). Candidates are gathered from
every enabled source, scored by how far the pointer would have to move, and
the nearest within tolerance wins **per axis** — so an object can snap to a
wall on X while staying free on Y.

Sources, in priority order when two are equally near: walls, polygon corners,
room centre, neighbour centres (alignment guides), neighbour edges, grid.

Two details that matter:

* Snapping aligns an object's **edge** to a wall, not its centre — a 1 m sofa
  against the left wall sits at x = 0.5. The user is aligning the sofa's back.
* Extents are the object's **axis-aligned bounding box**, so a rotated object
  lines up by what is visible in the plan rather than by its untransformed
  width.

Hold **Alt** to suspend snapping entirely.

### Alignment

Align left/right/top/bottom (by bounding-box edge), centre on X or Y (by mean),
distribute evenly on either axis, and rotate-to-wall.

Distribution equalises the **gaps**, not the centre spacing, and leaves the two
extremes where they are — equal gaps is what reads as evenly spaced when the
objects differ in size.

### Keyboard

| Key | Action |
|---|---|
| `Ctrl+Z` / `Ctrl+Shift+Z`, `Ctrl+Y` | Undo / redo |
| `Ctrl+A` | Select all (or all in the focused room) |
| `Ctrl+C` / `Ctrl+V` / `Ctrl+D` | Copy / paste / duplicate |
| `Del`, `Backspace` | Remove selection |
| `Esc` | Clear selection |
| Arrows | Nudge 5 cm (Alt: 1 cm) |
| `Alt` (held) | Suspend snapping and rotation detents |

---

## 3. The edit API

`POST /api/projects/{id}/edits` accepts:

```jsonc
{
  "remove_objects": ["sofa_1"],
  "keep_objects":   ["lamp_3"],
  "room_types":     { "room_0": "living_room" },
  "remove_lights":  ["pendant_2"],

  "object_overrides": {
    "sofa_1": {
      "category": "sectional", "room_id": "room_0", "label": "grey sectional",
      "position": { "x": 2.4, "y": 3.1 }, "rotation_z": 90,
      "dimensions": { "width": 2.6, "depth": 1.0, "height": 0.8 },
      "locked": true,
      "asset": "sectional_l_wide", "material": "fabric", "color_hex": "#5A6570"
    }
  },

  "add_objects": [
    { "source_id": "sofa_1", "position": { "x": 4.0, "y": 3.0 } },
    { "category": "armchair", "room_id": "room_0", "position": { "x": 2, "y": 2 } }
  ],

  "room_finishes": {
    "room_0": {
      "wall":    { "material": "wallpaper", "color_hex": "#E8DCC8" },
      "floor":   { "material": "marble" },
      "ceiling": { "material": "gypsum" },
      "ceiling_type": "recessed"
    }
  },

  "light_overrides": { "pendant_1": { "power_w": 120, "color_temperature_k": 2700 } },
  "add_lights":      [{ "kind": "floor_lamp", "room_id": "room_0" }]
}
```

Everything unrecognised — an unknown key, a bad colour, a material that does
not exist — is returned in `report.rejected_edits` rather than ignored, so a UI
bug surfaces instead of silently dropping a user's edit.

### Validation rules for a transform

The three transform keys are assembled into a candidate and checked **as a
whole** before the object is mutated, so a resize that only fits because of a
simultaneous move is judged on the result. A rejected edit leaves the object
exactly as it was.

**Refused** (the generator could not build it):

* placement outside the object's room (free-standing objects only — wall and
  ceiling fixtures sit on the boundary by definition)
* any dimension outside 0.05–20 m
* a footprint over 55% of the room

**Allowed, and reported:** deliberate overlaps. The human outranks the
validator and is told what they did.

### Separation of concerns

Two invariants the tests pin down:

* **Appearance never moves anything.** Swapping a three-seat sofa for a
  sectional keeps position and rotation exactly. The user chose where it goes
  and is only disagreeing about what it looks like.
* **Finishes are per-room.** Repainting the living room promotes that surface
  to a room-level override; the bedroom is untouched.

### Provenance

A duplicate does not inherit its source's observational evidence —
`observation_count` resets to 0 and `source_images` is cleared, because a copy
is the user's doing, not a second sighting. Nor is it born locked. A user-set
asset clears `asset_score`, which described the *matcher's* confidence in its
own pick and says nothing about a human's choice.

---

## 4. Incremental validation

`validate.py` runs once, during analysis, and never again. That was fine when
review could only delete and relabel. Once it can drag, rotate and resize, an
edited scene can violate checks the pipeline enforced and nothing would notice
before the render.

[`modules/vision/recheck.py`](../modules/vision/recheck.py) closes that gap.
**No model, no network** — every check is a geometric predicate over the graph,
so a re-check is cheap enough to run after every edit. It runs:

* automatically after every `POST .../edits` (report-only)
* on demand via `POST /api/projects/{id}/validate`
* once more immediately before generation, reported into the job log

### Reusing the checks rather than reimplementing them

The analysis-time checks run against a **deep copy**, and the difference
between the copy and the original *is* the set of proposed corrections. That
keeps one implementation of "what is a legal placement" instead of two that
drift apart — and checks added to `validate.py` later are picked up for free.

A subtlety worth recording: collision resolution moves *both* halves of a
colliding pair while reporting a single issue against one of them. The diff is
therefore taken over **every** object, not just issue subjects; otherwise the
partner's correction — and its protection — would be silently dropped. An
object moved as the far side of someone else's collision gets a `displaced`
issue of its own so nothing changes unexplained.

### Report-only by default

```python
recheck(graph)                                     # nothing is mutated
recheck(graph, apply_corrections=True)             # locked + user edits spared
recheck(graph, apply_corrections=True,
        respect_user_edits=False)                  # only locks are spared
```

The precedence is deliberate and total:

| | moved by auto-correction? |
|---|---|
| untouched detection | yes |
| carries a `*_set_by_user` flag | only with `respect_user_edits=False` |
| `locked` | **never**, under any flag combination |

Locking is the strongest statement a user can make about a placement, and
`validate.py` honours it too: `_shift` returns early on a locked object, and a
colliding pair gives the whole correction to the unlocked partner.

### Checks

Replayed from `validate.py`: scale, room containment, support heights, wall
intersection, collision, ceiling clearance, light sanity.

New, and living in `recheck.py` because they concern *habitability* rather than
physical possibility — a scene can be perfectly well-formed and still have a
wardrobe blocking the only door:

| Check | Rule | Severity |
|---|---|---|
| `blocked_door` | Nothing inside a 0.9 m keep-clear zone projected into the room from each door | error |
| `tight_circulation` | No gap between two obstacles narrower than 0.6 m — a person's shoulder width plus clothing | warning |
| `unreachable_floor` | ≥60% of free floor reachable from the doorways | warning |
| `room_full` | Furniture covers the entire floor | error |

Objects that are walked *over* rather than around — rugs, carpets, and anything
under 12 cm tall — are excluded from all three, so a rug between two chairs is
not a barrier.

Reachability rasterises the free floor at 10 cm and flood-fills from the
doorways: the same technique room segmentation uses on walls, chosen because it
degrades gracefully. A slightly wrong cell costs 10 cm of accuracy rather than
a wrong answer.

The door clearance zone is projected from the opening toward the room's
interior, which avoids needing to know which way the door swings — the approach
side is inside this room either way.

### Advisory, never blocking

Generation is **never** blocked. Reaching the generate button means the user
looked at the scene and chose it; an error says "this will render badly", not
"this is forbidden". The pre-build check reports into the job log and builds as
reviewed.

User-created overlaps are recomputed from the graph on each `build_review`
rather than replayed from the edit report, so the warning disappears as soon as
the user moves clear instead of lingering as a stale complaint.

---

## 5. Vocabularies come from the server

Dropdowns are driven by `review.vocabulary`, served with every review payload:

| Field | Count | Source |
|---|---|---|
| `room_types` | 10 | `catalog.ROOM_TYPES` |
| `categories` | 47 | `catalog.OBJECT_CATALOG` |
| `materials` | 26 | `catalog.MATERIALS` (with `applies_to`) |
| `ceiling_types` | 7 | `catalog.CEILING_TYPES` |
| `light_kinds` | 10 | `catalog.LIGHT_TYPES` |
| `assets` | 57 | `assets.ASSET_VARIANTS` |

The UI can therefore only offer what the server validates against. The
alternative — a hardcoded list in the frontend — was already drifting before
this change.

`get_review` rebuilds a cached `review.json` that predates a schema addition,
so a project analysed before an upgrade does not serve a payload the UI cannot
render.

---

## 6. Testing

```bash
python -m pytest tests/ -q     # 148 passing
cd web && npm test             # 39 passing
cd web && npm run typecheck
```

| Suite | Covers |
|---|---|
| `tests/test_object_editor.py` | Transforms, limits, support cascades, locking |
| `tests/test_editor_operations.py` | Assets, materials, finishes, lights, duplication |
| `tests/test_incremental_validation.py` | Report-only, protection precedence, habitability |
| `web/tests/editor.test.cjs` | Document, history, selection, snapping, alignment |

The web tests use Node's built-in `node:test` against a CommonJS build of
`lib/` (`npm run pretest`), so the app gains no test dependency.

---

## 7. Limitations

**No 3D viewer.** The app links `model.glb` for download; it does not render
it. Plan ↔ 3D bidirectional selection is therefore not implemented, and cannot
be built incrementally on what exists — it needs a viewer first, plus per-object
names emitted into the GLB so objects are addressable.

**Whole-scene regeneration.** Generation rebuilds everything through `main.py`;
there is no per-room rebuild.

**Analysis-time validation still runs once.** `recheck` covers the gap after
editing, but the relationship solver (`relations.py`) does not re-run, so
moving a sofa does not re-aim the TV it faces. Relationships are preserved as
recorded, not re-solved.

**Snapping is 2D.** Vertical stacking is handled by the support system, not by
snapping; there is no snap-to-shelf-height.

**Marquee selects by centre.** An object whose centre falls outside the
rectangle is not selected even if most of its footprint is inside.
