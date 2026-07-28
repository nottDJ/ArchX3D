# ArchX3D — Multi-image, multi-room reconstruction (v2.1)

How several reference images of one building become a single scene graph, and
how the user reviews it before anything is rendered.

---

## 0. What already existed

The brief opens with *"The current pipeline accepts only a single reference
image."* That was not the case. As of v2.0 the pipeline already accepted many
images and fused them:

| Requested | Status before this change |
| --- | --- |
| Multiple reference images | **Already worked** — `--images` takes `nargs="+"`, plus directories |
| Multi-image scene fusion | **Already worked** — [`fusion.py`](../modules/vision/fusion.py) |
| Object identity resolution / no duplicates | **Already worked** — attribute-similarity clustering, counts take the max across images, not the sum |
| Confidence fusion | **Already worked** — damped noisy-OR |
| Material + lighting fusion | **Already worked** — confidence-weighted merge |
| Cache, no duplicate AI requests | **Already worked** — content-addressed on image + prompt + model |
| Never invent objects | **Already worked** — three-band confidence policy |

The verified v2.0 run analysed two images and corroborated 13 objects across
both. So the work below is the genuinely new part:

* **Room segmentation** — the graph had exactly one room, which is why
  "do not place objects in the wrong room" had no meaning yet.
* **Image classification and per-type routing** — CAD, plan views, exteriors.
* **Image-to-room assignment and grouping.**
* **Parallel analysis.**
* **Review and edit before generation**, and the wizard around it.

---

## 1. Room segmentation

[`rooms.py`](../modules/vision/rooms.py)

The textbook approach — build a planar graph from the wall segments and
enumerate minimal cycles — is exact on clean input and fragile on real CAD,
where walls overshoot, stop short, or are drawn as two offset lines with a gap
at every doorway. One unclosed corner silently merges two rooms.

So segmentation is **raster flood-fill**:

1. Draw the wall segments onto a 5 cm grid and dilate to their real thickness.
2. Dilate again by `gap_closing_m` (default 1.10 m) to seal doorways, so
   adjacent rooms separate instead of flooding into one another.
3. Connected-component label the free space; components touching the grid
   border are the outside.
4. Undo the sealing dilation per component so each room reaches its true wall
   faces, then trace the boundary (Moore neighbourhood) and simplify it
   (Douglas–Peucker).

Resolution-limited boundaries are irrelevant here: rooms *assign* and *contain*
furniture. Geometry still comes from the exact wall segments.

Rooms also record which walls bound them and which rooms they connect to
through a doorway.

**Measured** on `test_complex.dxf`: 5 rooms — 45.5, 33.1, 27.6, 10.6 and
2.6 m² — with connectivity. On a plan whose walls enclose nothing, segmentation
reports failure and the pipeline falls back to a single whole-plan region
rather than dropping every object.

The one tuning knob is `gap_closing_m` (`--gap-closing`): it must exceed the
widest door opening and stay below the narrowest passage meant to read as open.

---

## 2. Image classification and routing

[`classify.py`](../modules/vision/classify.py)

Two signals, combined:

**Local heuristics** (no network) are genuinely reliable for exactly one thing:
telling line-art from photographic content. A CAD export has near-zero
saturation, a dominant flat background and high edge density. Both *strokes*
and *background* are necessary conditions — without that gate, a blank image or
desaturated noise scores as a drawing on low saturation alone, and everything
in it gets discarded.

**The model's own classification** arrives in the same analysis call it already
makes, so routing costs no extra request. Where the local line-art detector
fired, it wins: a model claiming a blueprint is a photograph must not be
believed, because that would inject invented wall colours into the building.

| Class | Mode | Contributes |
| --- | --- | --- |
| `interior_photograph`, `interior_render`, `room_render` | `full` | everything |
| `furnished_floorplan`, `top_down_layout` | `layout` | furniture layout; lighting dropped, palette halved |
| `cad_drawing`, `wireframe`, `architectural_elevation` | `geometry` | openings and structure **only** |
| `exterior_render`, `site_plan` | `skip` | nothing |

Routing is requested in the prompt *and* enforced in
[`observe.enforce_analysis_mode`](../modules/vision/observe.py). The prompt is
an optimisation; the enforcement is the guarantee. A single invented "light
grey wall, confidence 0.9" from a blueprint would otherwise propagate into the
fused finish for the whole building.

### On "AI-generated" detection

The brief asks for AI-generated images to be detected. **Reliably separating an
AI-generated interior from a conventionally-rendered CG interior is not
something this pipeline can honestly claim** — modern renderers and generators
produce overlapping artefacts and detector accuracy on unseen generators is
poor.

What *is* reliable, and what the requirement actually needs, is **photograph vs.
synthetic**. Both AI images and CG renders are synthetic and both warrant
identical treatment: trust for layout, palette, lighting and decoration; never
for metric geometry. So images are classified `photo | render | drawing` with a
`geometry_trust` weight, and **geometry always comes from the DXF**. A
misclassification here shifts confidence weighting; it can never alter the
building's shape.

---

## 3. Assignment: which room is this image?

[`assignment.py`](../modules/vision/assignment.py)

A DXF gives geometry but almost never room labels. Images give labels but no
position.

1. **Cluster images by the room they depict.** Two `bedroom` images form one
   group — that grouping is what makes two views of one sofa fuse into one sofa.
2. **Match clusters to regions** by area plausibility (a 14 m² space is a
   plausible bedroom, an implausible bathroom), plus a size-rank prior (living
   rooms are usually the largest). Solved **globally** by enumerating
   permutations, not greedily — one bad early pick would otherwise strand a
   later cluster with nothing plausible left.
3. **Ground each group inside its own room frame.**

Step 3 is what makes "do not place objects in the wrong room" structural rather
than a check. [`grounding.frame_from_region`](../modules/vision/grounding.py)
builds a frame carrying only that room's polygon and bounding walls, so
back-projection, wall snapping and clamping all operate inside the correct
space. **An object detected in a bedroom render is geometrically unable to land
in the kitchen.** Object ids are namespaced (`room_0__bed_1`) so two bedrooms
cannot collide.

Rooms no image covers stay empty and are reported as a warning. Furnishing them
would mean inventing objects nobody observed.

### Plan views are different

A top-down furnished plan shows every room at once and needs no camera model:
the image *is* the floor plane, so a normalised box maps linearly onto plan
coordinates. That makes plan views the most positionally accurate input the
pipeline accepts — more so than a perspective photograph, where depth must be
inferred. Their objects are assigned to whichever region contains them;
anything landing outside every room (legends, title blocks, dimension strings)
is dropped.

---

## 4. Scene graph changes

```jsonc
{
  "schema_version": "2.0",
  "rooms": [                       // NEW — was a single `room` object
    { "id": "room_0", "room_type": "living_room", "style": "modern",
      "polygon": [[x, y], ...], "bounds_min": [...], "bounds_max": [...],
      "area": 45.5, "ceiling_height": 3.0, "confidence": 0.8,
      "connected_to": ["room_1"], "wall_ids": ["wall_3", ...],
      "source_images": ["img0", "img1"],
      "wall_finish": {...}, "floor_finish": {...}, "ceiling_finish": {...},
      "ceiling_type": "recessed" }
  ],
  "room": { ... },                 // mirrored: the largest room, for older consumers
  "objects": [
    { "id": "room_0__sectional_1",
      "room_id": "room_0",         // NEW
      "category": "sectional", "position": {...}, "rotation_z": 130.3,
      "dimensions": {...}, "confidence": 0.97,
      "source_images": ["img0", "img1"], "observation_count": 2, ... }
  ],
  "lights":   [ { "room_id": "room_0", ... } ],   // NEW field
  "openings": [ { "room_id": "room_0", ... } ],   // NEW field
  "diagnostics": {
    "segmentation": {...}, "images": [ /* per-image classification */ ],
    "assignment": {...}, "objects_per_room": {...}, ...
  }
}
```

`SceneGraph.room` is now a property returning the largest room, so the Blender
generator's camera framing keeps working unchanged. `from_dict` accepts both
the new `rooms` list and the older single `room`, so an existing scene graph
still loads.

---

## 5. Deduplication and confidence fusion

Unchanged from v2.0 and already verified, restated here because they are named
deliverables.

**Deduplication** is semantic, not geometric. Merging by 3D proximity would
require each camera's pose in a shared frame — a full SfM problem, and
interiors (textureless walls, repeated furniture, few overlapping features) are
where SfM struggles. Getting it slightly wrong duplicates furniture, which is
far more visually damaging than one slightly misplaced sofa.

So: object counts take the **maximum** seen in any single image, never the sum
— each viewpoint sees a subset of the same furniture, and summing is exactly
what manufactures phantom duplicates. Observations are matched across images by
colour (40%), material (20%), size bucket (20%) and label overlap (20%).

**Confidence fusion** is a damped noisy-OR: each corroborating observation
closes 50% of the remaining gap to certainty, capped at 0.99.

```
0.87, 0.94, 0.92  →  0.87 + 0.13·0.94·0.5 = 0.931
                  →  0.931 + 0.069·0.92·0.5 = 0.963
```

Deliberately damped: three mediocre looks should not manufacture near-certainty.
Verified by test — two images of one room yield **one** sofa and **three**
chairs, not two and six.

---

## 6. Review and edit

[`review.py`](../modules/vision/review.py)

`build_review` flattens the graph into the validation view: rooms and their
contents, per-object confidence and band, per-image classification and what
each image was allowed to contribute, warnings, validation issues, and —
importantly — **the detections that were discarded, with plain-English
reasons**. Showing what was thrown away is what makes this a review rather than
a progress bar.

`apply_edits` takes decisions back:

| Key | Effect |
| --- | --- |
| `remove_objects` | delete outright, **cascading** to anything resting on them |
| `keep_objects` | build an uncertain detection the user confirmed |
| `object_overrides` | change category, room or label |
| `room_types` | relabel a room (confidence set to 1.0 — a human said so) |
| `remove_lights` | delete a luminaire |

Applied to a deep copy, with every change reported and every unrecognised edit
**rejected explicitly** rather than silently ignored, so a UI bug surfaces
instead of quietly dropping a user's decision. Moving an object between rooms
repositions it inside the target polygon — leaving it at its old coordinates
would put it physically in the previous room.

Cascade matters: verified live, deleting a sectional and a TV unit removed the
10 cushions and 2 objects resting on them (40 → 26 objects). A cushion whose
sofa was deleted would otherwise float.

---

## 7. Generation wizard

[`web/app/new/page.tsx`](../web/app/new/page.tsx),
[`components/wizard/`](../web/components/wizard/)

| Step | Screen |
| --- | --- |
| 1 Floor plan | DXF drag-and-drop; states plainly that geometry comes only from here |
| 2 Reference images | **Multi-select and drag-and-drop**, thumbnails, per-file removal, rejected files listed with reasons |
| 3 AI analysis | The validation page — see below |
| 4 Generate | Live job log |
| 5 Walkthrough | Download; **the in-browser first-person viewer is not built** |

The validation page shows a **plan map**: room polygons with every object drawn
at its true oriented footprint, colour-coded by confidence, with a tick marking
each object's facing direction. A bed in the kitchen is obvious there and
invisible in a list. Rooms without imagery are drawn dashed. Every object can
be removed or (if uncertain) confirmed, and every room relabelled, before
anything is generated.

### API

```
POST   /api/projects                     create + upload DXF
POST   /api/projects/{id}/images         upload one or many images
DELETE /api/projects/{id}/images/{name}
POST   /api/projects/{id}/analyse        background job
GET    /api/jobs/{job_id}                poll
GET    /api/projects/{id}/review         validation payload
POST   /api/projects/{id}/edits          apply decisions
POST   /api/projects/{id}/generate       build from the reviewed graph
GET    /api/projects/{id}/model.glb
```

Jobs live in an **in-process registry** ([`project_api.py`](../modules/project_api.py)):
lost on restart, single-worker only. That is the right size for a local desktop
tool, and `JobRegistry` is the only seam a Celery/Redis backend would replace.

---

## 8. Performance

| | Cold | Cached |
| --- | --- | --- |
| 2 images, Gemini 2.5 Pro | ~147 s sequential → **~78 s wall clock** | 0 s |
| Segmentation + assignment + solve + validate | ~2–10 s | same |
| Blender build + GLB | ~4 s | same |
| **Full wizard, analyse → model** | ~2.5 min | **~35 s** |

* **Parallel analysis** via a thread pool (`max_workers`, default 4). Model
  calls are I/O-bound, so threads are the right tool. `parallel_speedup` is
  recorded in `provenance.vision`. Verified: parallel and sequential runs
  produce byte-identical placements.
* The cache is now **thread-safe** — atomic temp-file writes, locked counters.
* Segmentation is cheap: a 377×307 grid for a 14×10.5 m plan.
* Validation runs **per room**, so a collision in one room cannot displace
  furniture in another, and cost stays linear in room size rather than
  quadratic in total object count.

---

## 9. Known limits

* **Room labels are inferred, not read.** DXF room-name text is not parsed, so
  assignment relies on area plausibility. Two same-sized bedrooms may be
  swapped — the review step exists partly for this.
* **Room polygons are raster-derived.** Boundaries are accurate to ~5 cm and
  doorway thresholds are approximate.
* **Multi-floor is not implemented.** The brief asks for "different floors";
  segmentation is single-level and there is no Z separation. A multi-storey DXF
  would produce overlapping rooms on one plane.
* **Multi-image fusion resolves identity, not pose.** A second view raises
  confidence and sharpens attributes; it does not triangulate position.
* **The first-person walkthrough (step 5) is not built.** The wizard ends at a
  GLB download and says so.
* Dense rooms still produce unresolved overlaps (14 in the live run, mostly
  cushions) — reported rather than hidden.

---

## 10. Verification

64 tests, all passing, no API key required (model calls are faked).

New coverage in [`tests/test_multiroom.py`](../tests/test_multiroom.py):
segmentation through a doorway; unenclosed-plan fallback; polygon containment;
line-art detection; blank and noisy images *not* misread as drawings; the local
CAD signal overriding the model; per-mode enforcement for all four modes;
images of different rooms mapping to different regions; two views of one room
sharing a region; rooms without imagery left unfurnished; **objects confined to
their assigned room**; plan-view coordinate mapping; parallel and sequential
agreement; and a CAD upload furnishing nothing end to end.

Live, against Gemini and Blender:

```
5 rooms segmented (45.5 / 33.1 / 27.6 / 10.6 / 2.6 m²)
2 images classified interior_render → full analysis, grouped into one room
40 objects placed in room_0; 4 rooms correctly left unfurnished
Blender: 40 objects, 9 luminaires, 5 openings, 82 materials → 262 KB GLB
```

Full wizard API driven end to end: project created, two images uploaded in one
request, a `.json` correctly rejected, analysis job polled to
`READY_FOR_REVIEW`, review fetched, edits applied (2 removals cascading to 12
dependents, one room relabelled), generation completed, `model.glb` downloaded,
and the reviewed graph confirmed intact afterwards.
