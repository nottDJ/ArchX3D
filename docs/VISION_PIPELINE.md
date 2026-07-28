# ArchX3D — Vision Pipeline (v2.1)

Reference imagery → a validated multi-room scene graph → a furnished 3D building.

> **v2.1 adds** room segmentation from the DXF, image classification with
> per-type routing, image-to-room assignment, parallel analysis, a review/edit
> step and the generation wizard. See
> [MULTI_IMAGE.md](MULTI_IMAGE.md) for that layer; this document covers the
> per-room reconstruction it sits on.

---

## 0. Starting point: what actually existed

The brief asked to improve the accuracy of an existing image-analysis and
furnishing pipeline. **Neither existed.** Before this change:

| Claimed capability | Reality in the codebase |
| --- | --- |
| Image analysis | None. [`style_generator.py`](../modules/style_generator.py) sent **DXF wall coordinates as text** to Gemini. No image was ever loaded — no PIL, no image parts. |
| Object detection | None. |
| Furniture placement | None. Gemini returned a `furniture_assets` list of *names*, and `blender_generator.py` never read that field. |
| Lighting from reference | None. A fixed 3-point rig (key/fill/rim). |
| Materials | Two colours: `wall_color_hex`, plus a floor colour from keyword-matching a material *name*. |
| Prompt | Hardcoded the sentence *"The layout seems to be a 10x6m area divided into two 5x6m rooms by a wall at x=5"* regardless of the input file. |

Every generated scene was walls + floor plane + ceiling plane + 3 lights.
So this is a from-scratch build, not an accuracy tune-up. It is strictly
additive: a new `modules/vision/` package and a new `data/scene_graph.json`
contract. No existing module was restructured.

---

## 1. The upgraded pipeline

```
reference images ─┐
                  ├─→ observe ─→ fuse ─→ ground ─→ relate ─→ assets ─→ validate ─→ scene_graph.json
DXF geometry.json ┘                        ▲                                            │
                                           └── exact room extents                       ▼
                                                                            blender_generator.py
```

### 1.1 Observe — one multimodal call per image
[`vlm.py`](../modules/vision/vlm.py), [`prompts.py`](../modules/vision/prompts.py), [`observe.py`](../modules/vision/observe.py)

A single Gemini 2.5 call per image returns one document covering objects,
architectural elements, wall/floor/ceiling finishes, luminaires, openings,
relationships and a camera estimate. One call rather than several: splitting
detection / materials / lighting across calls triples latency and cost for no
accuracy gain, since they all read the same pixels.

Three prompt decisions carry most of the accuracy:

- **The controlled vocabulary is inlined in the prompt.** Listing the accepted
  category and material terms raises the rate at which labels resolve cleanly
  instead of relying on post-hoc synonym matching.
- **No metric estimates are requested.** The model reports a normalised 2D box
  and a coarse *size bucket* (`large` **for a sofa**, not "2.4 m"). VLMs are
  reliable at relative judgements and confidently wrong at absolute
  centimetres.
- **Abstention is made cheap.** The rule that omission beats invention is
  stated repeatedly, calibrated confidence is requested explicitly, and
  `partially_visible` / `base_occluded` flags let the model decline to guess
  hidden geometry.

Parsing is deliberately paranoid: nulls in numeric slots, out-of-vocabulary
categories, transposed box corners, and relationships referencing objects that
were dropped are all handled by discarding the offending entry and counting it,
never by aborting the run. In the live test 21 entries were rejected this way
across two images.

### 1.2 Fuse — several photos of one room
[`fusion.py`](../modules/vision/fusion.py)

The obvious approach — back-project each image separately then merge by 3D
proximity — needs each camera's pose in a shared frame. Recovering that from
uncalibrated interior photos is a full SfM problem, and interiors are precisely
where SfM struggles (textureless walls, repeated furniture, few overlapping
features). Getting it slightly wrong produces **duplicated furniture**, which is
far more visually damaging than one slightly misplaced sofa.

So fusion works on **semantic identity**:

- Object counts take the **maximum** seen in any single image, never the sum.
  Each viewpoint sees a subset of the same furniture; summing is exactly what
  manufactures phantom duplicates.
- Observations are matched across images by attribute similarity — colour
  (40%), material (20%), size bucket (20%), label overlap (20%).
- Confidence rises with independent corroboration via a **damped noisy-OR**
  (each extra look closes 50% of the remaining gap to certainty, capped at
  0.99), so three mediocre glances cannot manufacture near-certainty.

*Measured:* 45 raw detections from 2 images → 32 objects, 13 of them
corroborated across both views, zero duplicates.

### 1.3 Ground — where the accuracy comes from
[`grounding.py`](../modules/vision/grounding.py)

**ArchX3D already knows the room exactly.** Walls, extents and ceiling height
come from the DXF. So the vision layer is never asked to solve the hard problem
("how big is this room and where is everything in 3D?"). It solves the much
easier one: *given a room I already have, where inside it does each object sit?*

1. A pinhole camera is fitted to the room from the model's horizon and
   field-of-view estimate. It is a **fitting device**, not a claim about where
   the photographer stood — what matters is that every object in one image
   shares it, so relative placement is consistent.
2. The **bottom edge** of each box is back-projected onto the floor plane.
   Floor contact is the one image cue that maps directly to a plan position,
   and it needs no depth network.
3. Metric size = catalog prior × size-bucket multiplier, nudged (damped 50%) by
   the box's aspect ratio, then clamped to the category's plausible range *and*
   to the room.
4. Wall-mounted objects are intersected against actual wall planes instead.
5. Rays that pass above the horizon are mathematically un-projectable; those
   objects fall back to a wall-affine default and are flagged, not silently
   accepted.
6. Wall-affine categories snap flush to their nearest wall. A sofa floating
   30 cm off the wall reads as wrong immediately; the snap is gated on the
   category's `wall_affinity`, so a dining table in open space is left alone.

The catalog's mounting prior overrides the model for structurally fixed
categories (a split AC unit is never actually *resting on* the counter beneath
it), while `tv` and `monitor` stay model-driven because they genuinely can be
wall-mounted or stood on furniture.

### 1.4 Relate — objects are not placed independently
[`relations.py`](../modules/vision/relations.py)

Observed relationships are taken at face value. Where the model stayed silent,
`catalog.IMPLIED_RELATIONSHIPS` supplies near-universal arrangements (chairs
surround the dining table, bedside tables flank the bed) at reduced confidence
and clearly marked as inferred.

Constraints are then **solved**, not just recorded:

- Predicates run in dependency order — positional before orientation, or a
  later constraint undoes an earlier one.
- At most one *position-defining* constraint survives per subject. The model
  happily reports both "bowl on the coffee table" and "bowl on the side table";
  solving both just applies whichever ran last. The most confident wins.
- Chairs are laid along a table's long sides first, then its ends — a circular
  arrangement around a rectangular table reads as obviously synthetic. Seats
  that fall outside the room are skipped rather than clamped, because clamping
  a seat drags the chair back inside the table it is meant to sit at.
- A wall-backed sofa refuses a rotation greater than 75°: it cannot swivel to
  face a TV behind it, and tearing it off the wall would be worse.

### 1.5 Match assets
[`assets.py`](../modules/vision/assets.py)

57 procedural variants covering all 47 categories, built by 24 parameterised
builders. Selection scores proportion (45%), style (25%), material (20%) and
tone (10%). Variants are *parameterised*, so a
low wide sectional and a compact two-seater resolve to different geometry —
not the same box at two scales.

Procedural rather than an imported mesh library because the project has no
asset pack, and generated geometry keeps the GLB small and the pipeline
dependency-free.

### 1.6 Validate — physical plausibility
[`validate.py`](../modules/vision/validate.py)

Overlap is judged in **3D, not plan**: a rug under a coffee table and a lamp on
a side table both overlap in plan and both are correct. Two objects only
conflict when their footprints *and* their height ranges intersect.

| Check | Correction |
| --- | --- |
| Overlapping footprints | Relaxation passes; the **less confident** object absorbs most of the movement |
| Floating / sunken objects | Dropped to the floor or onto their support surface |
| Footprint crossing a wall | Pushed clear along the wall normal |
| Object outside the room | Clamped back inside |
| Taller than the ceiling | Trimmed |
| Larger than 55% of the room | **Withheld** — not reshaped |

Corrections are limited to *unambiguous* fixes. Where a fix would require
inventing information, the object is flagged and withheld from the build
instead of being quietly resized into something the reference never showed.

Because collision resolution moves objects, orientation constraints are
**re-solved afterwards** — otherwise a sofa ends up facing where the TV used
to be.

---

## 2. Scene graph format

`data/scene_graph.json` — the single source of truth for Blender. Full types in
[`schema.py`](../modules/vision/schema.py).

```jsonc
{
  "schema_version": "2.0",
  "room":    { "room_type", "style", "polygon", "bounds_min", "bounds_max",
               "ceiling_height", "confidence" },
  "walls":   [{ "id", "start", "end", "height", "thickness", "finish", "observed" }],
  "floor":   { "material", "color_hex", "roughness", "metallic", "finish", "confidence" },
  "ceiling": { ... }, "ceiling_type": "plain|gypsum|recessed|...",
  "openings":     [{ "id", "kind", "wall_id", "position", "width", "height", "sill_height", ... }],
  "architecture": [{ "id", "kind", "position", "dimensions", "rotation_z", "finish", ... }],
  "lights":       [{ "id", "kind", "position", "mounting", "color_temperature_k",
                     "power_w", "size", "confidence", "uncertain" }],
  "objects":      [{ "id", "category", "label", "group",
                     "position": {"x","y","z"}, "rotation_z", "dimensions": {"width","depth","height"},
                     "support": "floor|wall|ceiling|on_object", "support_id", "wall_id",
                     "color_hex", "material", "asset", "asset_score",
                     "confidence", "uncertain", "flags": [],
                     "bbox_2d", "source_images", "observation_count",
                     "distance_to_nearest_wall", "distance_to_room_center" }],
  "relationships": [{ "subject", "predicate", "object", "confidence", "satisfied" }],
  "provenance":  { "generated_at", "images", "vision": {...}, "elapsed_s" },
  "diagnostics": { "fusion", "relationships_applied", "asset_builders",
                   "validation", "rejections", "confidence", "errors" }
}
```

Conventions: metres throughout, same frame as `geometry.json`, +Z up.
`rotation_z` is degrees CCW with 0 facing +Y. `position` is the centre of the
footprint at its **base** height, not the volumetric centre.

Every entity carries a confidence. `flags` records what happened to an object —
`snapped_to_wall_3`, `depth_beyond_room_clamped`, `arranged_around_dining_table_1`,
`support corrected on_object -> wall from catalog prior` — so any placement can
be traced back to the decision that produced it.

**Confidence bands** ([`ConfidencePolicy`](../modules/vision/schema.py)):

| Band | Range | Behaviour |
| --- | --- | --- |
| Accept | ≥ 0.65 | Built without qualification |
| Uncertain | 0.40 – 0.65 | Kept in the graph, flagged `uncertain`, **not built** by default (`--include-uncertain` overrides) |
| Discard | < 0.40 | Dropped; counted in `diagnostics.rejections` |

---

## 3. Models and techniques

| Concern | Choice | Why |
| --- | --- | --- |
| Scene understanding | **Gemini 2.5 Pro** (fallback 2.5 Flash) | Natively multimodal; **open-vocabulary**, so new furniture types need no retraining; returns 2D grounding boxes; and — uniquely — reasons about *materials* and *relationships*, which a detector cannot |
| Metric scale | Catalog priors + DXF room extents | The reliable path; see §1.3 |
| Object identity across views | Attribute-similarity fusion | Avoids SfM failure modes in interiors |
| Placement | Deterministic back-projection + constraint solver | Explainable and debuggable; no learned component to drift |
| Collision resolution | SAT penetration + confidence-weighted relaxation | Cheap, stable, order-independent |

### Alternatives evaluated and deliberately not integrated

The brief asked whether the vision model is sufficient and which additions
would help. Assessment:

- **Closed-vocabulary detectors (YOLO/DETR)** — *rejected.* COCO has `couch`,
  `chair`, `tv`, `potted plant` and essentially nothing else in this domain: no
  `tv_unit`, `wardrobe`, `kitchen_island`, no materials, no relationships. It
  would be a strict downgrade.
- **Open-vocabulary detection (GroundingDINO / OWL-ViT)** — *useful, deferred.*
  Would give tighter, better-calibrated boxes than a VLM, which directly
  improves back-projection accuracy. Costs a torch dependency and a GPU.
  The right place for it is as a **box refiner**: keep Gemini for semantics,
  let the detector sharpen each box.
- **Monocular depth (Depth Anything V2 / Metric3D)** — *the highest-value
  addition, deferred.* Would replace the floor-contact heuristic with true
  per-pixel depth and fix the pipeline's main weakness: objects whose base is
  occluded. Needs metric alignment against the known room to be useful, which
  the DXF makes tractable.
- **Segmentation (SAM 2)** — *moderate value.* Precise masks would improve
  colour sampling and footprint estimation for L-shaped items. Largely
  subsumed by better boxes.
- **Multi-view SfM** — *rejected for now*, per §1.2.

The pipeline is structured so each of these slots in behind an existing
interface without touching the stages around it. They were not integrated
because each adds a multi-gigabyte GPU dependency to a currently
CPU-only, dependency-light project — that is a decision worth making
explicitly rather than by default.

**SDK note:** `google-generativeai` has reached end of support upstream
("all support has ended"). [`vlm.py`](../modules/vision/vlm.py) prefers the
successor `google-genai` when installed and falls back to the legacy SDK, so
migration is a dependency bump, not a code change.

---

## 4. Accuracy

There is no previous version to measure against — the baseline produced zero
furniture. The honest comparison is capability plus measured behaviour on the
live test (2 reference photographs, a 14.0 × 10.5 m DXF plan).

| | Before | After (measured) |
| --- | --- | --- |
| Objects placed | **0** | 32 |
| Object categories recognised | 0 | 47 (+126 synonyms) |
| Lights from reference | 0 (fixed 3-point rig) | 7 across 3 fixture types, with CCT and power |
| Openings cut | 0 | 3 windows, distributed along the wall |
| Structural elements | 0 | 2 |
| Distinct materials | 2 colours | 66 material datablocks |
| Ceiling treatment | flat plane | classified (`plain`/`gypsum`/`recessed`/…) |
| Relationships enforced | 0 | 16 constraints solved |
| Confidence reporting | none | per entity; mean 0.916 |
| Plausibility checking | none | 46 auto-corrections, 3 unresolved (reported) |

**Guardrails, verified by test:**
- 0.44-confidence detections are kept but flagged, not built.
- 0.15-confidence detections are discarded entirely.
- An out-of-vocabulary category is dropped *despite* a stated confidence of 0.9.
- A sofa 5.5 × 3.8 m in a 6 × 4 m room is withheld, not shrunk.
- Two images of the same room yield one sofa and three chairs, not two and six.

40 unit tests cover parsing, fusion, grounding, relationships, validation,
asset matching and full-pipeline behaviour, all driven by a fake backend so
they run with no API key. **All 40 pass.**

### Known accuracy limits

- The room polygon is the **bounding box** of the wall segments. Exact for the
  rectangular rooms this targets; over-covers an L-shaped plan.
- Camera fitting assumes the canonical interior shot (standing back along the
  long axis). An unusual viewpoint degrades depth ordering, though wall
  snapping and validation still keep the result plausible.
- Objects whose floor contact is hidden rely on the model's estimate of where
  the base *would* be; these are flagged `base_occluded`.
- Multi-image fusion resolves *identity*, not *pose*. A second view improves
  confidence and attributes; it does not triangulate position.
- 3 unresolved overlaps in the live run were cushions on a sofa — cosmetic, and
  reported rather than hidden.

---

## 5. Performance

Measured on the live 2-image run:

| Stage | Cold | Cached |
| --- | --- | --- |
| Vision calls (2 images, Gemini 2.5 Pro) | 124 s | **0 s** |
| Fuse → ground → relate → assets → validate | 2.1 s | 2.1 s |
| Blender build + GLB export | 4 s | 4 s |
| **Full `main.py` run** (`--skip-render`) | ~2.5 min | **8 s** |

- **Caching** is content-addressed on `sha256(image bytes) + prompt + model id`.
  Editing the prompt or switching models invalidates cleanly; re-running after
  a placement or Blender change costs nothing. This is the common case during
  iteration, and it is why accuracy could be raised without a proportional
  time cost.
- **One call per image**, never several. Cost scales linearly with image count
  and is capped by `max_images` (default 6).
- Images are downscaled to a 1536 px long edge before upload — beyond that
  Gemini gains no accuracy on room-scale scenes while latency and tokens keep
  climbing. It also makes the cache key stable across source resolutions.
- Geometry is accumulated into **one mesh per object** via a single bmesh, so
  32 objects cost 32 draw calls, not several hundred. The furnished GLB is
  237 KB.
- Materials are deduplicated by (colour, roughness, metallic): 32 objects × 3
  slots collapsed to 66 datablocks.
- `gemini-2.5-flash` roughly halves vision latency at some cost in recall; set
  `vision.model` in `config.json`.

---

## 6. Future improvements

Ordered by expected realism gain per unit of effort:

1. **Metric depth estimation** (Depth Anything V2, aligned to the known room
   scale). Replaces floor-contact back-projection; fixes occluded bases —
   the single largest remaining source of placement error.
2. **Open-vocabulary box refinement** (GroundingDINO). Tighter boxes feed
   straight through to better positions and dimensions.
3. **True room polygons** from the DXF instead of bounding boxes, enabling
   L-shaped and multi-room plans.
4. **Per-object texture synthesis** rather than flat base colours — the largest
   remaining gap between the render and a photograph.
5. **Learned camera pose** per image, which would upgrade multi-image fusion
   from identity-matching to genuine triangulation.
6. **Window-aware daylighting**: drive a sun lamp from the observed openings
   and the reference image's apparent time of day.
7. **A feedback loop**: render the reconstruction from the fitted camera, ask
   the VLM to compare it against the reference, and iterate on the largest
   discrepancy. This closes the loop on "does it actually look like the photo?"
8. **Human-in-the-loop review** of the `uncertain` band, so flagged detections
   can be confirmed rather than discarded.

---

## Usage

```bash
# Furnished rebuild from reference photographs
python main.py plan.dxf --images reference_images/

# Or point at specific files
python main.py plan.dxf --images living_a.jpg living_b.jpg --skip-render

# Unfurnished architectural shell
python main.py plan.dxf --skip-vision

# Analyse only
python modules/scene_analyzer.py data/geometry.json data/scene_graph.json \
    --images reference_images/ --wall-height 3.0

# Force a re-query instead of using cached responses
python main.py plan.dxf --images reference_images/ --no-vision-cache

# Build the low-confidence detections too
python main.py plan.dxf --images reference_images/ --include-uncertain
```

Requires `GEMINI_API_KEY`. Without it the pipeline still produces the
architectural shell. Configuration lives under `vision` in `config.json`.
