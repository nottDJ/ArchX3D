# The DXF reconstruction engine

How ArchX3D turns an architectural DXF into a validated building model, and
why each stage is shaped the way it is.

The engine lives in `modules/recon/`. It is deterministic, runs on the CPU,
and needs no API key, no network and no model. Everything downstream — Blender,
the viewer, the vision enrichment — consumes its output.

---

## Why it was rewritten

A real desktop acceptance test on a conventional US residential plan
(`tests/fixtures/plans/residential_us.dxf` — three bedrooms, two baths, great
room, kitchen, dining, pantry, utility, two-car garage, covered porch, covered
deck) produced a model that was not the building. The old engine reported
success and wrote a valid GLB.

Four independent defects combined:

| # | Defect | Effect on the test plan |
|---|--------|-------------------------|
| 1 | `$INSUNITS` treated as authoritative | Declared millimetres; drawn in inches. The house came out **0.80 m × 0.56 m** — 25.4× too small. |
| 2 | Every line segment extruded into its own slab | A 100 mm wall is drawn as *two* lines, so it became two paper-thin sheets with a void between them. Nothing met anything. |
| 3 | One `wall_thickness` from `config.json` for everything | The drawing's own 2×4 and 2×6 framing (102 mm and 152 mm) both became 150 mm. |
| 4 | Floor as a bounding-box rectangle; rooms from text clustering | An L-shaped house with a garage wing and a deck got a rectangular slab, and no room topology at all. |

The decisive point is that **none of these could be detected downstream**. The
GLB was well-formed. The exporter was right to report success. Nothing in the
pipeline knew what a building was supposed to look like, so nothing was in a
position to object.

---

## The pipeline

```
DXF
 │
 ├─ read.py       parse, flatten blocks, apply transforms, classify,
 │                resolve units, normalise the frame       → Drawing
 ├─ openings.py   collect opening evidence (headers, swings,
 │                glazing, blocks)                         → Evidence[]
 ├─ walls.py      faces → pairing → centrelines → cleanup  → Wall[]
 ├─ structure.py  walls that hang together; fragments      → Structure[]
 ├─ openings.py   match evidence onto walls, classify      → Opening[]
 ├─ topology.py   planar graph → bounded faces → rooms     → Room[]
 │                rooms and openings back to structures;
 │                link each opening to the rooms it joins
 ├─ levels.py     which structures are buildings and which
 │                are storeys of one building              → Building[]
 ├─ evidence.py   the entities each wall, room and opening
 │                came from; the semantic CAD document     → evidence
 ├─ validate.py   refuse anything that is not a building
 │
 └─ ir.py         Drawing  →  building.json  →  Blender
```

Four orderings in that list are load-bearing.

**Opening evidence comes before walls.** A wall's line work stops at every door
and window — that is what an opening *is* on a plan. So the faces arrive
already cut into pieces, and without knowing where the openings are the engine
cannot tell a doorway from the end of a wall. But you cannot find openings from
the walls, because the walls are what you are building. The way out is that
openings are drawn *positively*: as headers, swing arcs, glazing lines and
blocks, all of which exist independently of the walls. That evidence is
gathered first and handed to the wall builder as **bridges**.

**Structures come before openings are classified.** Whether a wall is on a
building's envelope decides what a hole in it is: a 3 m gap in an exterior wall
is a garage door, and the same gap inside is a cased opening between two rooms.
Exterior is therefore settled per structure, from that structure's own walls.
Settled across the whole sheet, every building but the largest came out
"interior".

**Rooms come after walls.** A room is a bounded face of the planar subdivision
induced by the wall centrelines. It is therefore impossible for the engine to
invent a room where the drawing has no walls, and impossible to miss one that
the walls do enclose. Text only *names* a face that geometry has already found.

**Storeys come after rooms.** A plan title is only worth attaching to a plan
that turned out to exist, and a structure that encloses no room is not one. So
the house-or-storey question is answered last, on the structures that survived.

---

## Stage 1 — Reading (`read.py`)

Everything downstream sees a `Drawing`: flat lists of classified polylines,
labels, arcs, block references and dimension measurements, in metres, in a
local frame.

* **Flattening.** Architectural DXFs put the building inside blocks, blocks
  inside blocks, and blocks inside xrefs. Every `INSERT` is expanded
  recursively through its own transform (`virtual_entities`), with a depth cap
  so a self-referential block cannot hang the process. A reader that only walks
  modelspace sees the markers and none of the geometry.
* **Curves.** `ARC`, `CIRCLE`, `ELLIPSE`, `SPLINE` and polyline bulges are
  flattened to bounded-sagitta polylines. Door swing arcs are *also* kept as
  arcs, because the radius is the door width and the chord is the leaf
  direction — evidence flattening destroys.
* **Transforms.** OCS→WCS is applied. Skipping it is how a plan comes out
  mirrored.
* **Normalisation.** The origin is the minimum corner of the *building*, not of
  the sheet. The test plan's extents run to `(-725, -522)` because of notes and
  dimension strings while the building occupies a different box entirely; and
  another test plan sits near `(23000, 2900)`, where float32 — what a GLB
  stores — has spacing you can measure.

The frame is computed from a **robust** bound, not a min/max. One real fixture
carries a door block inserted 23 km from the building and another a furniture
block 2 km out; taken at face value the plan appears 40 km wide, every
candidate unit then looks absurd, and unit resolution collapses. The bound is
the 2nd–98th weighted percentile of wall line work, grown by 35% to re-include
genuine projections such as a porch.

## Stage 2 — Units (`units.py`)

**The header is a claim, and claims are falsifiable.**

The decisive evidence is wall thickness. Buildings are not self-similar: a wall
is roughly 60–500 mm thick no matter how large the building, and the common
thicknesses are a short list of conventions. The perpendicular distances
between overlapping parallel line pairs form a distribution whose mode is the
wall thickness *in drawing units*, and testing that mode under each candidate
unit picks the unit out:

```
residential_us.dxf modal pair distances: 4 and 6 drawing units
    as millimetres -> 4 mm and 6 mm walls     absurd
    as feet        -> 1.2 m and 1.8 m walls   absurd
    as inches      -> 102 mm and 152 mm       exactly 2x4 and 2x6 framing
```

Supporting evidence: whether `DIMENSION` measurements are whole numbers (an
integer drawing unit), `$MEASUREMENT`, and how typical a plan of the resulting
size would be — graded smoothly, not banded, because a flat in-band bonus made
a 10 m plan and a 121 m plan score identically.

How much it takes to overturn `$INSUNITS` scales with how much geometry there
is to overturn it *with*. A plan whose walls are hundreds of matched line pairs
states its thickness emphatically and the header loses; a drawing with a dozen
incidental parallel distances states nothing, and letting those outvote an
explicit declaration is how a 14 m flat became a 140 m one.

Every candidate's score and reasons are recorded in `units.json`, and a
disagreement with the header is reported as a `conflict` rather than resolved
silently.

## Stage 3 — Classification (`classify.py`)

An architectural drawing is mostly not a building: it is a building plus the
apparatus of describing one. On the test plan only about 10% of the drawable
segments are wall geometry.

Classification is a chain of evidence, strongest first, and **`UNKNOWN` is
never promoted to `WALL`**. A segment becomes a wall because something
positively identifies it as one, not because nothing ruled it out.

1. **Entity type.** A `DIMENSION` is documentation. Always — no layer name, no
   geometry, no context can make it a wall. Same for `LEADER`, `TEXT`/`MTEXT`,
   and anything on `Defpoints`.
2. **Layer role.** Survives xref binding (`xref-Bishop-Overland-08$0$A-WALL` →
   `A-WALL`), and covers the AIA convention, ISO 13567, and the vernacular
   (`WALLS`, `MUR`, `MURO`).
3. **Block semantics.** Geometry inside `P-Toilet` or `FIXT-SNGREF30` is a
   fixture wherever it sits.
4. **Geometry**, only as a fallback for drawings with no usable wall layers —
   and then restricted to the neighbourhood of the walls that *are* known, so a
   title block cannot become a building.

Foundation layers (`S-FOOTER`, `S-STEM-WALL`, `S-SLAB`) are real construction
but are not the walls of the storey being modelled; extruding them produces a
second, slightly larger ghost building interleaved with the first. They are
classified `structure_below` and kept as envelope evidence only.

## Stage 4 — Walls (`walls.py`)

A wall is a solid with two faces, so the reconstruction runs backwards from the
faces.

1. **Support lines.** Every segment joins a maximal collinear run — its *face*.
   Eleven separate `LINE` entities along one side of a corridor are one face.
2. **Pairing.** Two parallel faces, a plausible thickness apart, overlapping
   along their shared direction, bound a wall: the overlap span is the wall,
   the midline its centreline, the separation its thickness. Greedy,
   best-scoring-first, with each span of each face consumable once — which is
   what lets one long exterior face pair with a 100 mm partition over part of
   its length and a 150 mm wall over the rest.
3. **Single-line fallback.** Some drawings draw walls as bare centrelines. If
   pairing explains too little of the line work, the runs are taken as
   centrelines at the drawing's modal thickness. The decision is made
   **globally** and recorded — mixing the two readings produces walls that are
   half real and half doubled.
4. **Cleanup.** Centrelines are extended to their true intersections and their
   ends snapped. This is the step that closes rooms: pairing stops each wall
   where its faces stop overlapping, which at an L corner is half a thickness
   short and at a T junction a full thickness short. Without it every corner
   has a gap and no room polygon can ever be found.
5. **Doorway repair.** Two collinear centrelines a doorway apart are one wall
   with a door in it — there is no other construction they could be. And a
   partition that stops a metre short of the corridor wall it runs into has a
   doorway there, not an open side. Both are completed **and the gap is
   reported**, so it becomes an opening rather than being quietly made solid.

Thickness is measured, never configured. Conventions only break ties.

## Stage 5 — Structures (`structure.py`)

A structure is a set of walls that physically hang together, and the test is
geometric: two walls are in the same structure if they touch, and two groups of
touching walls are one structure if the gap between them is under 0.6 m —
wide enough to absorb a joint the wall repair could not close, narrower than
any real separation between buildings, since fire separation, access paths and
setbacks are all wider.

A structure is **not yet a building**. Two structures may be a house and its
detached garage, or a ground-floor plan and a first-floor plan drawn side by
side; that question needs text, and `levels.py` answers it. This stage
guarantees only that it never welds two separate pieces of construction into
one and never splits one piece into two.

**Substance and fragments.** Real sheets carry wall-layer geometry that
encloses nothing: a boundary-wall stub, a freestanding screen, a planter, a
length of fence, a deck post. Treating each as a building reports sixty
buildings on a site plan. So a group of walls founds a structure only when it
encloses at least 4 m² — a guard booth clears that, a fence line does not. A
group that encloses nothing is a **fragment**: it joins the structure it sits
against when one is within 3 m, and is otherwise reported as unassigned wall.
Neither silently extruded as a building nor silently deleted.

Once rooms exist the partition is settled again: rooms, openings and footprint
parts are distributed to the structure that owns them, a structure left with no
room is demoted, and each structure gets the part of the drawing's envelope
that is actually its own. Exterior walls are labelled twice — once from the
structure's own walls before openings are classified, and again from its real
footprint once rooms exist.

## Stage 6 — Openings (`openings.py`)

Evidence, strongest first:

| Evidence | What it gives |
|---|---|
| Header / lintel band (`A-HEADER`, `R-BEAM`, `S-LINTEL`) | Exact position, width and host thickness |
| Opening band on an openings layer | The same rectangle by another name |
| Door swing arc | Radius = leaf width, centre = hinge, and the swing |
| Glazing lines (2–3 parallel lines across the opening) | A window |
| Door/window block | Position and kind |
| An unexplained gap in a wall the pairing already built | Last resort, low confidence |

Geometry is **clustered before it is measured**: the same opening is drawn
three different ways depending on the office. A header is one closed rectangle;
a window is commonly two or three parallel glazing lines with a jamb at each
end — no rectangle anywhere, but the *group* of them is exactly the rectangle.

A header bears on the piers either side of the hole it spans, so it is never
narrower than the opening and is often wider. Where two pieces of evidence
disagree about width, the narrower, more specific one is the opening.

Kind is settled from evidence, not from size alone: a swing means a door,
glazing means a window, and a wide hole means a garage door only when the wall
is an exterior one — a wide hole in an interior wall is a cased opening between
two rooms.

## Stage 7 — Topology and rooms (`topology.py`)

Wall centrelines are noded and polygonised; each bounded face is a candidate
room. The face runs down the middle of the walls, so it is half a thickness too
big on every side; the interior is recovered by subtracting the wall solids —
which keeps a room bounded by a 100 mm partition on one side and a 200 mm
exterior wall on the other correct on *both* sides.

A doorway is a hole in a wall, not a wall, so it never appears in the graph and
never splits a room. A cased opening between a dining room and a great room
likewise leaves one face, which is architecturally correct. Where such a face
carries more than one *recognised room name*, it is partitioned between them
and every part is flagged `open_plan`, so "a wall divides these" and "a drafter
named two parts of one space" stay distinguishable.

**Labels name; they never place.** Candidate room names are short strings that
are not construction notes, drawn at the sheet's room-name text height. Two
lines of one name are joined (`MASTER` / `BEDROOM`). AutoCAD formatting codes
are stripped, so nothing is called `%%uKITCHEN`.

**The envelope.** When the drawing has a footprint layer, that is the
drafter's own answer to where the building stops — and it includes the parts
walls do not enclose: the covered porch, the deck, the carport. Envelope area
that no wall encloses becomes an exterior room when it carries a porch/deck
label, and is otherwise *measured and reported* as unenclosed rather than
quietly absorbed.

## Stage 8 — Buildings and storeys (`levels.py`)

A sheet with two disconnected floor plans on it is one of three things: two
buildings on one site, two storeys of one building drawn side by side because
paper is flat, or genuinely unclear. **Geometry cannot tell the first two
apart** — a house beside its garage and a ground floor beside its first floor
are both "two closed wall networks a few metres apart". What tells them apart
is what the drafter wrote: a title under each plan (`GROUND FLOOR PLAN`), a
level token in the layer names (`GF-WALL`), a building designation (`BLOCK A`).

So storeys are only ever inferred from **positive evidence**, and disconnected
geometry is never stacked because it happens to be disconnected:

* Structures carrying distinct level designations become storeys of one
  building, ordered by those designations and registered over each other by
  their walls.
* Structures with no level evidence are separate buildings.
* Structures with no level evidence whose footprints are *congruent* — the same
  floor plate repeated — are `LEVELS_AMBIGUOUS`: kept apart exactly as drawn
  and flagged for a person to decide. Stacking them is a guess and merging them
  is wrong.

Text is evidence only when it is **placed like a title**, within 6 m of the
plan it names (or 75% of the plan's own size, whichever is more — dimension
strings on a large plan run several metres out), and only when no second plan
is nearly as close. A floor-area schedule lists `FLOOR - FIRST` and
`FLOOR - SECOND` one above the other in a table and a stair is labelled `UP TO
FIRST FLOOR`; neither names a plan, both are rejected, and the reason is
recorded.

Storey elevations are measured from the drawing where it states them and are
otherwise placed at multiples of the storey height — and when they are
estimated, validation says so in as many words, because an estimated height
must not read as a measured one.

## Stage 9 — The evidence bridge (`evidence.py`)

The wall model does not keep everything the downstream stages need: which block
is a toilet, what a room label says and where it sits, what a block's
`ROOM_NAME` attribute holds, which hatch pattern fills a space. That evidence
used to come from `cad.reader`, a second DXF parser with its own unit detection
and its own origin — and **two readers of one file eventually disagree**, about
units first and then about where everything is, at which point a room label in
one frame names the wrong room in the other.

So there is one reader, and this stage *projects* its records into the two
shapes the later stages consume:

* `to_cad_document` — the `CadDocument` the semantic tier was written against,
  in the reconstruction's own frame and units. Only the geometry and the
  parsing moved; the interpretation of strings, block names and layer names is
  still `cad.text`, `cad.blocks` and `cad.layers`.
* `source_evidence` — an id-addressed record of every entity the model was
  built from (`entity_id`, `entity_type`, `layer`, `block`, `transform`,
  `geometry`) and, for each wall, room and opening, the entity ids that are its
  evidence. A consumer that wants to know *why* there is a door somewhere
  follows the ids rather than re-deriving them.

The bridge is enrichment, so it may never cost the geometry: a failure here is
a warning on a model that is otherwise complete.

## Stage 10 — Validation (`validate.py`)

A `Drawing` is only ever returned if it passed here. A reconstruction that
cannot pass is raised as a `ReconstructionError` carrying the diagnostics.
**"No building, and here is why" is a supported outcome; "a wrong building" is
not.**

Every storey is validated in its own right, then every building, then the
drawing as a whole; a multi-storey drawing's report prefixes each finding with
where it came from, so one list says everything.

Errors (refuse the build): implausible overall size; no walls; no rooms; a wall
longer than the whole plan; an impossible wall thickness; overlapping rooms; an
opening that does not fit its host wall; a large share of wall length that
encloses nothing; long free-floating walls outside the envelope that bound no
room (the signature of a dimension layer that got through); a large share of
wall length belonging to no building at all.

Warnings (build anyway, but say so): a header/geometry unit conflict; more than
one structure on the sheet; rooms partly outside the envelope; low area
coverage; no openings found; an upper storey that does not stand on the one
below; storey elevations that were estimated rather than measured; part of the
sheet that took no part in the reconstruction.

Three of the checks are measurements of whether the *reconstruction itself* is
sound rather than of the building:

| Measure | What a bad value means |
|---|---|
| `repair_ratio` — reconstructed wall length over wall line work actually drawn | Most of the wall was invented by gap repair rather than drawn |
| `orientation_coherence` — share of wall length on the plan's main directions | The lines read as walls are unrelated strokes, not construction |
| `scale_confidence` — how a building's proportions should measure | With an uncertain unit, the scale is almost certainly wrong |

Two checks are deliberately *not* errors. **Multiple wall islands** are normal —
a house and its detached garage are two islands and both are real; what matters
is wall length that encloses *nothing*. And **long walls outside the envelope**
are only an error when they also account for a real share of total wall length;
one of them is a fence, a third of them is the annotation layer.

## Stage 11 — 3D (`modules/blender_build.py`)

A direct extrusion of the validated model, and nothing more. It decides
nothing: every question was answered and validated before it was called.

A wall with openings is built as the solid parts that **remain** — the piers
between openings, the lintel over each one, the sill block under each window.
That is exact, manifold and fast. A boolean modifier per opening on a
hundred-wall building is neither reliable nor quick, and it is the usual reason
a "3D floor plan" ships with doors painted on.

Floors follow the actual footprint, including each disjoint part, so an
L-shaped plan gets an L-shaped floor and a detached garage gets its own slab.
Ceilings cover interior rooms only — roofing a covered deck would seal the
model's daylight out and misrepresent the building.

Blender's interpreter has `bpy` but not `shapely` or `ezdxf`, so this module
imports nothing from `modules/recon` and reads plain JSON. The seam is the file.

## Stage 12 — Checking the GLB (`modules/glb_validate.py`)

The failure this engine was written to fix shipped a **valid GLB** of a 0.8 m
building. "The exporter reported success" and "the file loads" were both true
and neither meant anything. So the export is checked back against the
`building.json` it was built from: that the file is well-formed glTF 2.0, and
that the geometry in it is the building — every storey present, at its
elevation, over its footprint.

```bash
python modules/glb_validate.py output/model.glb data/building.json
```

---

## Diagnostics

Every run can write a bundle (`output/diagnostics/` by default), on failure as
well as on success:

```
entities.json   every layer, its classification, and the counts
units.json      every candidate unit, its score and its reasons
walls.json      the wall list plus face/pairing statistics
rooms.json      room polygons, areas, labels, boundary walls
doors.json      openings classified as doors, garage doors, cased openings
windows.json    openings classified as windows
levels.json     the buildings, their storeys, and the evidence for each
validation.json every check, its result, and the measured values
building.json   the complete IR
error.json      on refusal: the stage, the failures, the partial diagnostics

debug_raw.svg          everything the DXF contains, coloured by role
debug_normalized.svg   what survived as building geometry
debug_walls.svg        centrelines over the line work they came from
debug_rooms.svg        room polygons, labelled, with areas
debug_openings.svg     doors and windows on their host walls
debug_topology.svg     nodes by degree — degree 1 is a free end
debug_structures.svg   which walls the partition put in which structure
reconstruction.svg     everything together; look at this one first
```

The failure that motivated the rewrite was invisible in the GLB. A per-stage
drawing makes it obvious, and it costs milliseconds.

---

## Performance

Measured on the repository's own fixtures (Python 3.10, Windows, single core):

| Plan | Size | Primitives | Walls | Rooms | Total |
|---|---|---|---|---|---|
| `t01_simple_rect.dxf` | 0.1 MB | 60 | 6 | 3 | 0.05 s |
| `apartment.dxf` | 0.1 MB | 39 | 21 | 6 | 0.16 s |
| `residential_us.dxf` | 1.1 MB | 939 | 32 | 22 | 1.3 s |
| `final_plan_19th_may.dxf` | 1.2 MB | 792 | 55 | 23 | 1.2 s |
| `sba.dxf` (site plan, two blocks) | 6.0 MB | 9,970 | 442 | 202 | 18.7 s |

Peak traced memory is 6 MB for the residential plan and 42 MB for the site
plan. The large-sheet figure is dominated by `ezdxf` parsing a 6 MB file with
423 nested `INSERT`s.

Across the 79-drawing corpus the slowest reconstruction is 10.5 s — a five-storey
apartment building converted from BIM, with 210 doors and 263 windows. A
typical single-storey ResPlan house takes about 0.1 s. Every figure in
`docs/CORPUS_REPORT.md` is timed, so a change that makes the engine slower
shows up there without anyone having to look for it.

## Testing, and measuring

```bash
python -m pytest tests/test_recon_*.py tests/test_blender_build.py -q
python -m pytest tests/test_corpus_metrics.py -q   # the release gates
python tools/corpus/evaluate.py                    # remeasure, rewrite the report
python tests/fixtures/make_plans.py                # regenerate the synthetic fixtures
```

Three kinds of fixture, and none substitutes for the others.

The **generated** set (`tests/fixtures/plans/t*.dxf`, `multi/`, `doors/`,
`failures/`) isolates one construction each — blocks, a lying header, dimension
clutter, a rotated sheet, each unit, a doorway drawn only as a gap — so the
expected geometry is known exactly.

The **real** set says whether the engine handles what people actually send;
`residential_us.dxf` is the drawing that failed the desktop acceptance test and
is now a permanent regression fixture.

The **corpus** (`tests/corpus/`, 79 drawings) is the one that produces numbers
rather than verdicts. Every drawing has a `.truth.json` beside it in metres, so
`modules/recon/metrics.py` can measure the reconstruction against it: where the
openings are and what they are, where the walls run and how thick, how many
rooms and how big, what the footprint covers, whether the scale is right.
`tests/test_corpus_metrics.py` holds each dataset group to a floor and fails
the build with the number that moved; `docs/CORPUS_REPORT.md` is the current
measurement and `tests/corpus/README.md` says where each drawing came from,
what is real in it and what was drafted for it.

**Assertions are geometric, and the gates are measurements**, because "the
tests pass" is exactly what the old engine could say while producing a 0.8 m
building. A metric is a number a wrong reconstruction cannot accidentally hit.
The floors are per group rather than global: a single threshold across the
corpus would be set by whichever group is hardest and then met trivially by the
rest. The reasoning for each one is in the corpus README, and a change that
improves a number should raise its floor rather than leave slack behind it.
