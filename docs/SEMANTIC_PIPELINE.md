# Semantic Understanding Pipeline

How ArchX decides what a building *is*, before it generates anything.

## The problem this solves

Until this work, every semantic attribute in the system was downstream of
image observation. `room_type` was written in exactly two places, both fed by
the vision layer. The DXF contributed line segments and nothing else.

That inverted the project's own design philosophy. The trust hierarchy says
CAD metadata, blocks, layers and text (tiers 1–4) outrank image understanding
(tier 6) — but the extractor discarded tiers 1–4 before anything could read
them:

- `ENTITY_TYPES` covered five geometric types, so `INSERT`, `TEXT`, `MTEXT`,
  `ATTRIB`, `DIMENSION` and `HATCH` were never parsed at all.
- `LAYER_BLACKLIST` deleted `TEXT`, `DOOR`, `WINDOW`, `FURNITURE`, `DIM` and
  `ANNO` — precisely the layers that identify rooms.

So a project without usable reference photographs produced a building in which
every room was `unknown`, and no amount of vision work could have fixed it.
The information was being thrown away one step earlier.

## Architecture

Three packages, in dependency order. Each is independently testable and
imports nothing from the layer above it.

```
  DXF file
     │
     ▼
┌──────────────┐   modules/cad/        tiers 1–5
│    cad       │   Reads everything. Emits typed entities carrying
│              │   uid / source / confidence. No interpretation of
│              │   the building, only of the drawing.
└──────┬───────┘
       │  CadDocument
       ▼
┌──────────────┐   modules/semantic/   fusion
│  semantic    │   Scores every signal against one shared table of
│              │   priors, fuses them in log-odds space, and returns
│              │   a label with a confidence and its reasons.
└──────┬───────┘
       │  RoomClassification
       ▼
┌──────────────┐   modules/vision/     tier 6
│   vision     │   Reference imagery. One more evidence stream,
│              │   deliberately ranked below CAD.
└──────────────┘
```

`cad` owns the only `ezdxf` dependency. `semantic` is stdlib-only and knows
nothing about CAD or vision types — `semantic/bridge.py` is the single module
that joins them, which is what keeps the classifier usable from a scene graph,
a test fixture, or a DXF equally.

`geometry.json` keeps its exact previous shape. `metadata` and `walls` are
unchanged, so room segmentation, the Blender generator and the web viewer
needed no changes; the full CAD model is added under a new `cad` key.

## How classification works

Each signal emits `Evidence` carrying a **log-likelihood contribution per
candidate room type**. Contributions add; a softmax turns the totals into a
posterior. The scale is interpretable and lives in `semantic/taxonomy.py`:

```
+4.0  decisive       a toilet means bathroom
+2.5  strong         a sofa means living room
+1.0  supporting     this area suits a bedroom
+0.3  weak           this room has two windows
-2.0  contradicting  a toilet is not in a kitchen
```

Adding in log space is what gives two behaviours that neither "take the
highest-priority signal" nor "average the signals" provides at once:
corroboration compounds, and a single decisive observation cannot be
overturned by a pile of weak ones.

### Signals

| signal                 | tier | reads                                    |
|------------------------|------|------------------------------------------|
| `room_name_attribute`  | 1    | `ROOM_NAME` on a room-tag block          |
| `block_fixture`        | 2    | toilet / sink / cooktop / bed blocks     |
| `block_furniture`      | 2    | sofa, wardrobe, dining table blocks      |
| `layer_role`           | 3    | plumbing / casework layers in the room   |
| `room_label`           | 4    | `TEXT` / `MTEXT` naming the room         |
| `area`, `aspect`       | 5    | floor area and elongation against priors |
| `window_count`, `door_count` | 5 | openings, when actually surveyed       |
| `adjacency`            | 5    | an en-suite opens onto a bedroom         |
| `privacy_depth`        | 5    | graph distance from the entrance         |
| `vision`               | 6    | what the reference imagery reported      |

### The trust hierarchy is a veto, not a weight

An explicit room label or `ROOM_NAME` attribute is marked **authoritative**:
it *states* the answer rather than evidencing it. Fusion still runs, but only
to decide whether the rest of the evidence corroborates or contradicts the
stated answer, which adjusts confidence rather than the outcome.

Contradictions are surfaced, never silently resolved:

```
room_2: store 71%
  text label "STORE" inside the room
  against: TOILET block 0.4 m from the centroid (decisive)
  !! room_label states 'store', but the independent evidence
     favours 'bathroom' (59%)
```

A room labelled STORE containing a toilet is a real drawing error. The useful
behaviour is to report both readings and let a person decide.

### Nothing is guessed

Below 30% posterior the answer is `unknown` with zero confidence, not a
low-confidence guess. A plan with no labels, no blocks and no distinguishing
geometry comes back honestly empty.

## Cross-checks

The drawing states its own room sizes (`BED ROOM 16'0" X 15'9"`, `14.2
SQ.M.`). Comparing that against the area segmentation measured is the only
independent check available on whether segmentation worked, and it catches
failures that are otherwise invisible because the resulting geometry still
looks plausible:

```
!! the drawing states 23.4 m2 but segmentation recovered 4.6 m2
   (5.1x smaller); the room boundary is probably not closed
```

Reported, never corrected — resizing a room to match its label would invent
geometry that is not in the file.

## Notable correctness fixes

**OCS transforms.** Most planar DXF entities store points in an Object
Coordinate System defined by the entity's extrusion vector. A mirrored block
is routinely written with extrusion `(0,0,-1)` rather than a negative scale,
which flips the OCS x-axis. Reading those vertices as world coordinates
reflects them through `x = 0`. On a real project plan this put every mirrored
door at `x = -23.8 m` while the building sat at `x = +22.5 m`, reporting the
footprint as 47.8 × 0.78 m instead of 34.8 × 19.9 m. Mirrored doors are
near-universal, so this is not an edge case.

**Unit detection.** Ranked by evidence rather than a fall-through chain of
`elif`s (whose feet branch sat after a millimetre branch that had already
claimed part of its range). A header claim that would make the plan 1218 m
across is overridden rather than trusted.

**Absent data is not evidence.** `window_count == 0` because the drawing has
no glazing layer is not the claim "this room has no window". Conflating them
made every room on a legacy geometry file look windowless, handing rooms to
the types that expect to be (garage, store, shaft).

**Exclusive attribution.** A label names exactly one room and a block sits in
exactly one room, assigned globally before per-room work. Per-room proximity
matching let a bathroom label reach the bedroom next door and assert itself
there just as strongly, classifying a room containing a bed as a bathroom.

## Results

On a generated 6-room apartment fixture with AIA layers, MTEXT labels and
named blocks: **6/6 rooms identified, mean confidence 0.98, zero conflicts.**
With every label stripped, leaving only blocks and geometry: **5/6**, the
sixth being an unfurnished hall correctly identified from door count and
adjacency alone.

On a real project plan (`final_plan_19th_may.dxf`), which previously produced
11 rooms all `unknown` at confidence 0.0: **8/11 identified**, and the three
that remain unknown are flagged with the segmentation defects that caused
them.

## Stage 7 — procedural furnishing

Once rooms are typed, layout no longer needs an image. `modules/furnish/`
consumes the scene graph and nothing else:

| module | responsibility |
|---|---|
| `programme` | what a room of a given type and size should contain, ordered by importance with a minimum area each — plus the lighting scheme |
| `placement` | where each item goes: candidate generation and scoring, not constraint solving |
| `furnisher` | orchestration, palette application, and reporting every item that would not fit |

**Observation always beats convention.** A room with observed objects is
skipped entirely. Everything generated is flagged `procedural` with
`observation_count = 0`, so a design decision is never mistaken for a claim
about the building.

The solver enforces what makes a layout habitable rather than merely
non-overlapping: door swings stay clear, tall items don't block windows, each
item keeps usable clearance in front of it, and circulation width survives.
Floor coverings are exempt — a rug is *meant* to lie under a coffee table.
Dependent items are arranged by the existing `vision.relations` predicates, so
bedside tables flank the bed and dining chairs surround the table rather than
being placed independently against whatever wall was free.

Lighting is not optional: a furnished room with no luminaire renders black, so
every room gets a scheme — including circulation cores, which get a fitting
but no furniture.

## Why a furnished-floorplan upload produced nothing

A reported case worth recording, because three separate faults compounded and
the error message pointed at none of them.

The image was a composite sheet: an exterior render across the top ~45%, and
*two* floor plans side by side below it. It parsed fine — classified
`furnished_floorplan`, routed to the plan-view path.

1. **The model resolved almost nothing.** 11 objects for a whole house, every
   one from a single balcony corner. At sheet resolution the rest was a few
   pixels per item. This is what `vision/tiling.py` addresses.
2. **All 11 were then dropped.** `grounding.ground_plan_view` maps the *whole
   image* linearly onto the plan's bounding box, which assumes the image is
   one plan filling the frame. Off by ~2.4x in x and ~1.9x in y, every
   detection landed outside every room polygon and was discarded in silence.
3. **The DXF is one floor; the sheet showed two.** Even correct registration
   would have put first-floor furniture into ground-floor rooms.

And the message — "11 of 11 rooms have no reference imagery" — was false.
`assignment` counted only *interior* views, so a project whose only upload was
a floor plan was told it had supplied nothing.

Fixed: the warning now distinguishes interior views from plan and technical
views; a plan view losing most of its detections reports a registration
failure loudly instead of silently; and layout no longer depends on any of it,
because Stage 7 furnishes from the room type.

**Registration closed this.** Positional plan-view reading used to assume one
cropped plan per image. It no longer does: `modules/registration` matches the
room labels the DXF carries *with plan coordinates* against the same labels
printed in the image, and fits a similarity transform image→plan from the
correspondences. Inverting that transform and mapping the building's own
extent through it says which sub-rectangle of a sheet the drawing occupies, so
the composite case above now registers instead of failing.

See [`REGISTRATION.md`](REGISTRATION.md). The parts that matter here:

* The transform is **fitted, not assumed**, and the result says which. A
  `label_consensus` fit is a measurement; the old full-frame stretch survives
  as the labelled fallback `plan_bounds`, and every object placed through it
  carries a `plan_registration_assumed` flag.
* Fitting is **robust**. Three bedrooms produce three readings of one printed
  `BEDROOM`; the fit keeps the largest self-consistent set and discards the
  rest, so an ambiguous label costs nothing.
* A **multi-floor sheet is reported, not absorbed**. Labels that registered to
  nothing are named in the result — those are the other floor's rooms, and
  their furniture stays out of this one.

Point 3 of the failure above is therefore diagnosed rather than silently
wrong. Point 1 (resolution) is still `vision/tiling.py`'s problem.

**What is still assumed.** Registration needs the model to report the labels
it reads. A cached response predating that prompt, or a sheet whose text is
illegible at upload resolution, yields no correspondences and falls back —
correctly labelled, but still a guess. Interior photographs are registered to
a *room*, not to a pose; recovering a real camera pose against the CAD is
still open, and is what [`FIDELITY.md`](FIDELITY.md) §10 refers to.

## Testing

```
pytest tests/test_cad_semantics.py        # layer/block/text name classification
pytest tests/test_cad_reader.py           # DXF reading, units, OCS, origin
pytest tests/test_semantic_classifier.py  # fusion mechanics and each signal
pytest tests/test_semantic_integration.py # DXF in, classified rooms out
pytest tests/test_furnish.py              # programme, placement, furnishing
pytest tests/test_tiling.py               # tile grids, remapping, seam merging
pytest tests/test_registration.py         # CAD ↔ image transform fitting
```

The fixture is generated by `tests/fixtures/make_apartment.py` and committed,
so tests do not depend on ezdxf's writer. Regenerate with:

```
python tests/fixtures/make_apartment.py
```
