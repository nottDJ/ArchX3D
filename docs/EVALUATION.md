# ArchX3D — Reconstruction evaluation engine (v1.0)

Measures how far the generated reconstruction is from the photographs it was
built from, and — the part that matters — says **why** and **which subsystem
should change**.

```
reference photograph ─┐
generated preview ────┼──► five axes ──► findings ──► subsystem to change
scene graph ──────────┘
```

**The engine never modifies the scene graph.** It only measures. Every number
is derived from inputs that already exist, so two runs over unchanged inputs
produce identical documents — which is what makes an evaluation usable as a
regression baseline.

---

## 1. Findings, not scores

A score says a reconstruction is 0.71 similar. That is unactionable: it does
not say which colour, in which room, decided by which subsystem, or what to do
about it.

A **finding** is the unit this engine exists to produce:

```json
{
  "axis": "material",
  "summary": "wood light appears too desaturated",
  "subsystem": "MaterialSpecies",
  "difference": 0.62, "unit": "", "severity": 0.71, "confidence": 0.6,
  "why": "over the 34% of the frame the material-ID pass assigns to
          M_wood_light_D8B98C, the reference averages 0.412 saturation and the
          render 0.157",
  "evidence": { "coverage": 0.34, "reference_saturation": 0.412,
                "render_saturation": 0.157, "ratio": 0.381 },
  "remedy": "the wood light species is rendering too flat; check its base
             colour saturation and procedural tint",
  "room": "room_a", "viewpoint": "img_a1",
  "objects": ["coffee_table_1"], "materials": ["M_wood_light_D8B98C"]
}
```

Scores remain — comparing one run against the previous needs a number — but
they are a summary of the findings rather than the product.

### Explainability contract

Every finding carries all seven of: `why`, `evidence`, `objects`, `materials`,
`room`, `subsystem`, `remedy`. Every axis score carries the measurements behind
it in `detail`. A number that cannot be checked is a number nobody can act on.

### Subsystems

Each names a real, addressable part of ArchX3D — inventing categories that do
not correspond to code would make the whole exercise decorative.

| Subsystem | Owns |
|---|---|
| `LightingEnvironment` | room-scale brightness, warmth, daylight |
| `LightSource` | individual luminaires |
| `ColourPalette` | the room's colour roles |
| `MaterialSpecies` | walnut vs oak vs laminate, and their procedural tint |
| `SurfaceFinish` | the `Finish` on a wall, floor or ceiling |
| `AssetPlacement` | asset choice and procedural construction |
| `SceneGraphTransform` | object position, rotation, dimensions |
| `ObjectDetection` | the vision pass's confidence in a detection |
| `CameraFit` | the fitted `ViewPoint` itself |
| `Geometry` | DXF extraction and wall construction |
| `RenderSettings` | the preview pipeline's own settings |

`building_summary.json` reports **subsystem pressure** — the sum of
`severity × confidence` per subsystem — which answers the only question a
refinement pass actually has: *what should I change first?*

---

## 2. Architecture

```
modules/
├── render/                       Phase 2, extended with the passes below
│   ├── passes.py                 the pass codec        ← no bpy, shared
│   └── _blender_passes.py        pass rendering           bpy
└── evaluation/
    ├── schema.py                 findings, axis scores, the four documents
    ├── imaging.py                loading, colour maths, masks  ← only numpy user
    ├── projection.py             the stored ViewPoint as a camera
    ├── context.py                everything one axis needs
    ├── axes/
    │   ├── colour.py             palette and cast, localised per material
    │   ├── material.py           saturation and texture, from albedo
    │   ├── lighting.py           exposure, contrast, warmth
    │   ├── layout.py             visual mass + per-object displacement
    │   └── objects.py            scene-graph comparison — never detection
    ├── scoring.py                aggregation, confidence, subsystem pressure
    ├── report.py                 HTML and difference overlays
    └── engine.py                 orchestration, the four documents, CLI
```

### Data flow

```
data/scene_graph.json ─────┐
output/preview/manifest.json ──┼──► ViewContext ──► 5 axes ──► ViewpointEvaluation
reference_images/*.jpg ────┘         (per viewpoint)              │
                                                                  ▼
                                              RoomEvaluation ──► BuildingSummary
                                                                  │
                                                                  ▼
                             evaluation.json · per_viewpoint.json
                             per_room.json   · building_summary.json · report.html
```

### Output layout

```
output/evaluation/
├── evaluation.json          everything, one document
├── per_viewpoint.json       one entry per reference/render pair
├── per_room.json            one entry per room
├── building_summary.json    the top-level verdict alone
├── report.html              reference · generated · difference · scores · findings
└── overlays/
    └── <viewpoint>.png      CIELAB difference heat maps
```

Four files rather than one because they have different readers: a refinement
pass wants `per_viewpoint`, a dashboard wants `building_summary`, a human
debugging one room wants `per_room`.

---

## 3. The render passes (Phase 2 extension)

The pixel analysis needs more than a beauty render, so Phase 2 now emits five
auxiliary passes per viewpoint.

| Pass | Feeds | Encoding |
|---|---|---|
| `albedo` | material, colour attribution, lighting attribution | sRGB, unlit |
| `depth` | layout evidence, metric scale | `byte / 255 × depth_range` metres |
| `normal` | geometry consistency | `byte / 255 × 2 − 1` per axis, world space |
| `material_id` | material localisation | index across R (low byte) and G (high) |
| `object_id` | object localisation | index across R and G |

### Why they are PNGs, not EXR

Blender's native route is a multilayer OpenEXR, and in Blender 5.0 the File
Output node emits nothing else. Reading multilayer EXR from Python needs
OpenEXR or OpenImageIO — a binary dependency for a diagnostic.

Instead each quantity is *rendered*: the scene is temporarily re-shaded so
emission colour carries the value, with the `Raw` view transform so the linear
value reaches the byte unaltered. **Verified: a linear `7/255` decodes back to
exactly `7`.** The result is readable by any Pillow install, inspectable by eye,
and costs one extra render (~250 ms) instead of a new dependency.

### How each pass is produced

Two mechanisms, chosen by what varies:

**View-layer override** (`depth`, `normal`, `object_id`) — one material replaces
every surface. Correct when the value belongs to the geometry or the object.
Object identity reaches the shader through `ShaderNodeObjectInfo`'s colour
output, reading the per-object `object.color` set beforehand.

**Node rewiring** (`albedo`, `material_id`) — each material's own output link is
redirected through an emission node. For albedo this has a second virtue:
feeding the Principled node's *existing* Base Color input into the emission
preserves the procedural texture, so the material axis measures grain rather
than a flat average.

Everything is restored afterwards; the `.blend` is never saved.

### Data passes are point-sampled

Anti-aliasing averages neighbouring samples, and at a silhouette that averages
*indices*: material 4 beside material 27 yields a pixel claiming to be material
15, which would be masked as a third material that does not exist. Depth and
normals suffer identically — the mean of 2 m and 6 m is a surface that is not
there. So the data passes render at one sample with a degenerate filter width.
Albedo keeps its anti-aliasing: it is a colour image compared statistically,
where averaging is exactly right.

Measured effect: 45 material indices in a frame before the fix, 5 after — and
zero unresolvable indices.

---

## 4. The five axes

Each is a pure function of a `ViewContext` returning an `AxisScore` and the
findings that justify it. **They deliberately do not consult each other**: a
render that is too dark should lose points on lighting and nothing else, or one
problem shows up as two and a refinement pass gets told to fix the palette when
the lamps are at fault. Where an axis *could* claim ground another owns, it
names the other subsystem in the finding rather than scoring it itself.

### colour — `RGB`, `albedo`, `material_id`

Mean cast in CIELAB plus histogram intersection, then the same comparison **per
material region**. CIE76 rather than CIEDE2000: it is a plain Euclidean
distance a reader can verify, and the extra accuracy would be false precision
against a photograph of unknown white balance.

**Attribution via albedo** — if the render's *unlit* colour already differs from
the photograph, no relighting will fix it and the finding names
`SurfaceFinish`. If albedo agrees and only the lit render differs, it names
`LightingEnvironment`. Without an albedo pass it says so and drops its
confidence to 0.5 rather than guessing.

### material — `albedo`, `RGB`, `material_id`

A photograph has no albedo channel, so a like-for-like comparison is impossible
in principle. Two measures survive that:

* **Saturation** — illumination scales luminance far more than saturation, so
  comparing an unlit albedo's saturation against a photograph's is defensible
  where comparing lightness is not. This catches "walnut floor rendered grey".
* **Texture energy** — fine detail across two frequency bands. Asking "does this
  surface carry about the right amount of visible structure" is answerable;
  asking "is this the same oak" is not, and this axis does not ask it.

Carries the **lowest weight** because it makes the largest inference.

### lighting — `RGB`, `albedo`

Exposure, contrast and warmth, with the same albedo-based attribution: dividing
the render's luminance by its own albedo luminance recovers the *shading*, so
"the lamps are underpowered" separates from "the walls are painted charcoal".
Those need opposite fixes.

Warmth is explicitly relative — a photograph's white balance is unknown, so no
colour temperature is claimed, only a difference against the same measure taken
from the render.

### layout — `RGB`, `depth`, scene graph

Two measurements:

* **Visual mass agreement** on a coarse contrast grid — contrast, not
  brightness, so it does not re-measure the exposure difference the lighting
  axis owns.
* **Per-object displacement in metres**, needing no detection at all. Both
  halves are already stored: `SceneObject.bbox_2d` is where the vision pass saw
  the object, and `ViewPoint` is the camera fitted to that photograph.
  Back-projecting the box's floor contact gives the position the photograph
  implies; the graph holds what was built; the distance is how far the
  placement solver moved it.

**Systematic offsets are blamed on the camera, not the furniture.** If most
objects share one offset, the objects did not all move — the camera did. One
`CameraFit` finding replaces fifteen `SceneGraphTransform` ones, and per-object
displacements are then reported *net* of the shared error. The test uses the
median offset and the share of objects agreeing with it, not the mean: a mean
is dragged a long way by exactly the single misplaced object the axis is trying
to distinguish.

Depth is reported as evidence, never scored — the reference has no depth
channel to compare against.

### objects — scene graph only

**Never image detection.** Running a detector over the preview would compare one
model's opinion of the render with another model's opinion of the photograph —
two sources of error, neither observable. The graph already records what was
seen and what was built; the difference is exact and comes with its reason.

| Outcome | Meaning |
|---|---|
| `missing` | observed, not built — reason recoverable |
| `extra` | built with no observation (room scope only) |
| `replaced` | built from a stand-in asset; scored as half a failure |
| omitted | withheld by the confidence policy — the threshold is the fix |

Per viewpoint, only objects recorded *in that photograph* count: an object
behind the camera is not missing from the shot. Per room, everything counts,
which is where an object nobody photographed surfaces.

---

## 5. Scoring

```
score = Σ(weight × axis_score) / Σ(weight)      over measured axes only
```

| Axis | Weight | Why |
|---|---|---|
| objects | 0.25 | a missing sofa is a different room; a warm wall is the same room mis-tinted |
| colour | 0.20 | |
| lighting | 0.20 | |
| layout | 0.20 | |
| material | 0.15 | measured most indirectly — a photograph has no albedo |

### Unmeasured is excluded, not zero

The rule the engine exists to keep. An axis whose inputs were unavailable
contributes to **neither side** of the average. Scoring it zero would assert a
failure that was never observed; averaging it as 1.0 would assert the opposite.

The cost is carried by two figures travelling with every score:

* `weight_used` — the share of axis weight that was measurable. A 0.9 over five
  axes and a 0.9 over two are different claims, and this stops the second
  passing as the first.
* `confidence` — the axes' own confidence, discounted by coverage.

The same rule applies one level up: **a room where nothing was measurable is
excluded from the building score**, with a note saying so. Averaging in a 0.0
for an unphotographed cupboard would say the reconstruction is wrong there.

Rooms are area-weighted into the building score; viewpoints are
confidence-weighted into their room.

---

## 6. Reporting

`report.html` — one self-contained file, inline CSS, no scripts, no fonts, no
CDN. It gets opened from disk, mailed and attached to bug reports; anything
needing a network would fail in exactly those situations.

Per viewpoint it shows **reference · generated · difference overlay**, then the
axis scores with the measurement behind each, then the findings with their
reasoning and remedy. The overlay is a CIELAB difference heat map normalised
against a fixed 40 dE — per-image normalisation would make a nearly perfect
render look as broken as a badly wrong one, since both would saturate their own
range.

Everything printed is HTML-escaped: material names come from the `.blend` and
are not trusted input.

---

## 7. Using it

```bash
# after a build that produced previews
python modules/evaluation/engine.py

# or as a pipeline step
python main.py plan.dxf --images reference_images/ --evaluate

# targeted
python modules/evaluation/engine.py --manifest output/preview/manifest.json \
    --scene-graph data/scene_graph.json --images reference_images --top 20
```

```python
from vision import similarity
result = similarity.evaluate()          # -> EvaluationResult

result.score                            # building score
result.findings                         # deduplicated, ranked
result.room("room_a").totals            # per-room ScoreSet
result.viewpoint("img0").axes["colour"] # the measurement and its detail
```

`similarity.evaluate()` is a deferred re-export of `evaluation.evaluate()` —
the pipeline expects the entry point there, and the engine is large enough to
deserve its own package.

### Configuration — `config.json`

```json
"preview": {
    "passes": ["albedo", "depth", "normal", "material_id", "object_id"],
    "depth_range": 20.0
},
"evaluation": {
    "weights": {"colour": 0.20, "material": 0.15, "lighting": 0.20,
                "layout": 0.20, "objects": 0.25},
    "html": true,
    "overlays": true
}
```

Passes can be reduced (`"passes": ["albedo"]`) or switched off (`[]`) — each
costs one extra render. Turning them off does not break the engine; it makes
axes weaker and they say so.

---

## 8. Testing

```
tests/test_render_passes.py         31 tests   the pass codec
tests/test_evaluation_scoring.py    22 tests   aggregation, unmeasured exclusion
tests/test_evaluation_projection.py 19 tests   camera conventions
tests/test_evaluation_axes.py       28 tests   each axis against a known difference
tests/test_evaluation_engine.py     33 tests   end to end, four documents, report
tests/test_render_blender.py         9 tests   real Blender, incl. passes (opt-in)
```

Every axis test constructs a *known* difference and asserts that the right axis
notices it, quantifies it, and nominates the right subsystem. A test that only
checked "the score went down" would pass for an engine that blamed the lighting
for a missing sofa.

The engine tests pin the three properties the phase rests on: the scene graph
is byte-identical before and after; two runs agree; a missing input leaves the
axis unmeasured rather than zero.

```bash
ARCHX3D_RENDER_INTEGRATION=1 python -m pytest tests/test_render_blender.py -v
```

---

## 9. Limitations

* **Masks come from the render.** Region comparisons apply the render's
  material mask to the reference. That holds for large architectural surfaces
  and degrades where the reconstruction is grossly misplaced — which the layout
  axis reports, so the failure is visible rather than silent.
* **8-bit passes.** Depth quantises to `depth_range / 255` — 8 cm at the
  default. Fine for distributions; the per-object displacement figure comes
  from analytic projection instead, where the precision is the graph's.
* **Warmth is relative.** A photograph's white balance is unknown, so no
  absolute colour temperature is inferred.
* **Normals are one-sided.** A photograph has no normal channel, so the pass is
  a self-consistency descriptor rather than a scored comparison.
* **No refinement.** This phase measures only. Nothing here writes to the scene
  graph, and nothing here decides what to change — it only says what should.
