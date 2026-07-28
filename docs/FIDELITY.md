# ArchX3D — Photorealistic reconstruction (v2.3)

Style, materials, colour and lighting understanding, and an objective measure
of how close the result came to the reference photographs.

---

## 0. The shape of this phase

The previous phases made the reconstruction *editable*. This one makes it
*measurable*, then improves the inputs to that measurement.

```
DXF ──────────────► geometry            (unchanged, still the only source)
                         │
reference images ──► style              ← new: controlled vocabulary
                     materials          ← new: species tier
                     palette            ← new
                     lighting env.      ← new
                     viewpoints         ← new: fitted cameras, now persisted
                         │
                         ▼
                    scene graph ──► generation ──► render at each viewpoint
                                                        │
                              reference image ◄─────────┤
                                                        ▼
                                                 similarity report
```

Measurement came first deliberately. "Improve the visual fidelity" is
unfalsifiable without a number: every change to materials or lighting is
otherwise argued from screenshots, and regressions are invisible.

---

## 1. Style recognition

`catalog.STYLES` — 15 styles, each with the materials, palette and lighting
warmth it implies:

modern · minimalist · industrial · luxury · scandinavian · contemporary ·
traditional · bohemian · japanese · mediterranean · classic · farmhouse ·
art_deco · mid_century · unknown

`catalog.normalise_style` maps model prose to the vocabulary, longest synonym
first — so `"warm mid-century modern living space"` resolves to `mid_century`
rather than to plain `modern`.

### Confidence combines two independent signals

`appearance.resolve_style` scores the label on whether it resolved *and*
whether the room's materials agree with what the style implies:

```
"industrial" + exposed brick, concrete   → industrial, 1.00
"industrial" + white marble, velvet      → industrial, 0.55
```

That distinction matters because style now drives asset selection. A
corroborated style should steer harder than a stray adjective.

Scene-wide style is area-weighted *and* confidence-weighted
(`appearance.dominant_style`), so a large confidently-modern living room
characterises the home more than a small uncertain bathroom.

---

## 2. Materials — a species tier

The pipeline answered `wood` where the photograph plainly showed walnut.
`MaterialPrior` now has two tiers:

| Tier | Examples | Count |
|---|---|---|
| Family | `wood`, `marble`, `fabric`, `concrete`, `tile` | 26 |
| Species | `walnut`, `light_oak`, `teak`, `white_marble`, `black_marble`, `velvet`, `linen`, `boucle`, `travertine`, `terrazzo`, `brass`, `polished_concrete` | 35 |

**61 materials total.** Each species declares its `base` family and inherits
the surfaces it may be applied to — a walnut floor is legal because a wood
floor is; a velvet wall is not, because fabric is object-only.

This is what keeps the taxonomy extensible without a flag day. Anything that
only understands families calls `catalog.material_family()` and keeps working.

Each material also carries a procedural `texture` recipe and `grain` strength
(`wood_grain`, `veined`, `weave`, `speckle`, `brick`, `tiled`, `brushed`,
`noise`, `flat`) for the Blender material builder to generate a similar
surface where no scanned texture exists.

### A bug this surfaced

Asset variants declare broad materials (`"wood"`, `"fabric"`). Species-level
observations would have scored **0.35 — a mismatch — against the very family
they belong to**, silently degrading every asset choice. `_material_score` now
compares at family level:

```python
_material_score("walnut", ("wood",))    # 0.95, was 0.35
_material_score("velvet", ("fabric",))  # 0.95, was 0.35
_material_score("walnut", ("fabric",))  # 0.35, correctly a mismatch
```

---

## 3. Colour palettes

`appearance.derive_palette` assigns six roles per room, each from what actually
determines it:

| Role | Source |
|---|---|
| `primary` | Wall finish — walls dominate the visual field |
| `secondary` | Floor finish — next by area |
| `accent` | Most saturated decor colour not already a surface colour |
| `lighting` | Power-weighted mean fixture CCT → Planckian locus |
| `furniture` | **Footprint-weighted** mean of furniture colours |
| `decor` | Mean of decor and appliance colours |

Two decisions worth recording:

- **Footprint weighting.** A sectional characterises a room more than a side
  table; an unweighted mean lets clutter outvote the sofa.
- **Mixing in linear RGB, not hue.** Averaging a red and a cyan by hue gives
  green, which is in neither input. A dull average is the honest answer for
  "the overall colour of this furniture".

A room with nothing observed gets **no palette at all** — `None`, not a
plausible-looking default. `palette_from_style()` exists for unphotographed
rooms and is labelled `source: "style_prior"`, `confidence: 0.25`, so no
consumer can mistake a guess for a measurement.

---

## 4. Lighting reconstruction

Luminaires say what is *emitting*. `LightingEnvironment` says what the room
*looks like* — which is what a render has to match. Two rooms with identical
fixtures photographed at noon and midnight need different environments, and
nothing in the fixture list expresses that.

| Field | Derived from |
|---|---|
| `ambient` | Fixture W/m² plus window contribution |
| `window_contribution` | Glazed area ÷ floor area, saturating |
| `daylight_direction` | Heading through the **largest actual window**; `-1` when there is none |
| `daylight_elevation` | Time of day |
| `color_temperature_k` | Power-weighted fixture CCT, blended toward 6200 K by daylight share |
| `shadow_softness` | Window contribution and overcast |
| `time_of_day` | Model-reported when available, else inferred |

**Division of labour.** Numbers are computed from geometry — that is evidence.
The one judgement arithmetic cannot recover is whether it is dark outside, so
`time_of_day` is asked of the model and its answer always wins; the derived
value is marked `inferred` and the observed one `observed`.

With no windows recorded, `daylight_direction` is `-1`: there is no defensible
answer, so the renderer falls back to uniform ambient rather than inventing a
sun in an arbitrary place.

---

## 5. Camera viewpoints — the keystone

`grounding.estimate_camera` already fitted a pinhole camera to every reference
image in order to back-project furniture onto the floor. **That pose was
discarded once grounding finished.**

Persisting it as `schema.ViewPoint` is what makes objective comparison
possible: the generated scene can be rendered from the *same vantage* as the
photograph, so the two images are of the same view and any difference is a
difference in the reconstruction rather than in where the camera stood.

```json
{ "image_id": "img0", "room_id": "room_0", "source_image": "ref.jpg",
  "position": {"x": 0.8, "y": 2.0, "z": 1.55}, "yaw": 270.0,
  "pitch_deg": -6.5, "vertical_fov_deg": 62.0, "confidence": 0.7 }
```

The pose is a fitting device, not a claim about where the photographer stood —
`confidence` carries how much to trust it.

---

## 6. Asset matching

Matching was already appearance-aware (proportion 0.45, style 0.25, material
0.20, tone 0.10). What was missing was **honesty about the result**.

- Scores below `POOR_MATCH_THRESHOLD` (0.62) flag the object
  `"asset: closest available match, 55% similar"`.
- `assets.match_quality()` reports mean score, the weakest categories, and
  categories with no variant at all.
- The review payload carries `asset_score` and `asset_quality`
  (`close` / `fair` / `approximate` / `none`) per object.

`none` is distinguished deliberately: it means no variant exists for that
category and the object is built as a proportioned block — a gap in the
library, not a bad choice.

---

## 7. Similarity evaluation

`modules/vision/similarity.py`. Five axes:

| Axis | Weight | Measured from | Question asked |
|---|---|---|---|
| colour | 0.26 | pixels | Coarse RGB histogram intersection + mean colour distance |
| layout | 0.24 | pixels | Correlation of local-contrast mass on an 8×6 grid |
| lighting | 0.22 | pixels | Brightness, contrast, red−blue warmth |
| objects | 0.20 | **the graph** | Observed objects vs objects actually built |
| material | 0.08 | pixels | Detail energy across three scales |

### Why the object axis is not measured from pixels

Detecting furniture in the render would need another model call, and would
compare one model's opinion against another's rather than against the truth.
The graph already records what each image contributed and what survived to the
build; the difference is *exact*. That is also where the useful findings live —
an object detected but dropped below the confidence floor is precisely the
"missing plant" the user can act on:

```
plant was detected at 35% confidence, below the floor —
keep it in the review step to build it
```

### Why comparison happens at 128×96

The question is whether the room *reads* the same, not whether the pixels
match. At full resolution a correct reconstruction would score badly for having
different wood grain.

Layout compares **local contrast**, not brightness: contrast marks where things
are — furniture edges, window frames — while brightness mostly measures how the
room is lit, which the lighting axis already covers. Correlation rather than
absolute difference, because the question is whether mass is in the same
*places*, not whether the render is uniformly busier.

### Findings carry remedies

Every finding names which lever would address it — `materials`, `lighting`,
`assets` or `decor` — and `report.remedies()` returns them in order. That is
the input a bounded refinement pass consumes.

Demonstrated behaviour on controlled pairs:

| Render differs by | Axis that drops | Finding produced |
|---|---|---|
| Nothing much | — | 88% overall |
| Too dark | lighting 0.43 | "render is darker than the reference (0.23 vs 0.75)" |
| Too warm | lighting 0.61 | "lighting too warm compared with the reference" |
| Moved furniture | layout 0.29 | "layout differs — less detail in the lower centre of the frame" |
| Flat surfaces | material | "surfaces read flatter; textures may be missing grain" |

### Honest degradation

Pillow and numpy are optional. Without them the pixel axes report
`available: false` rather than a fabricated score, and the overall score is
renormalised over available axes — **unmeasured is not the same as bad**. The
object axis needs no image and still works.

---

## 8. Testing

```bash
python -m pytest tests/ -q     # 216 passing (was 148)
```

| Suite | Tests | Covers |
|---|---|---|
| `test_appearance.py` | 47 | Species tier, style resolution and confidence, palette roles, lighting derivation |
| `test_similarity.py` | 21 | Axis discrimination on controlled pairs, object diffing, report shape, degradation |

Similarity is tested against synthetic image pairs rather than photographs: the
point is that a *known* difference produces the right axis drop and the right
finding. A photograph pair would test the metric's taste rather than its
behaviour.

---

## 9. Expected improvement

Where the gain actually comes from, in rough order:

1. **Species materials** — walnut renders as walnut, not as generic timber.
   Combined with the family-scoring fix, this also repairs asset selection that
   would otherwise have degraded as the taxonomy grew.
2. **Lighting environment** — a night scene stops being rendered as a day
   scene with the lamps on.
3. **Style-guided assets** — a scandinavian room selects the light-wood
   variants rather than whatever proportion matched first.
4. **Palette** — gives the generator a coherent colour scheme per room instead
   of per-object colours with no relationship to each other.

Honest caveat: **the gains are not yet measured end to end.** The similarity
system can score a render against a reference, and it is tested against
controlled pairs, but the preview-render step that would feed it real renders
is not built (below). The numbers above are mechanism, not results.

---

## 10. Limitations

**No preview render pass.** The similarity module takes
`(viewpoint, reference, rendered)` triples; nothing yet produces the rendered
half. Blender must be asked to render a still at each stored `ViewPoint`. This
is the single missing piece that would close the loop, and everything it needs
is now in the graph.

**No automatic refinement loop.** `report.remedies()` names which levers to
pull, and the bounded-3-iteration workflow is specified, but the loop that
re-runs generation is not implemented — it depends on the preview render.

**No comparison UI.** The split-screen reference-versus-generated view is not
built. The data it needs (`ViewPoint`, similarity report) now exists.

**Blender does not yet consume the new fields.** `texture`/`grain`,
`LightingEnvironment` and `ColourPalette` are populated and persisted, but
`blender_generator.py` still builds materials and lighting the old way. Until
that is wired, the understanding improvements are recorded rather than
rendered. This is the highest-value next step after the preview pass.

**Style priors are not used as fallbacks yet.** `palette_from_style()` exists
and is tested but is not called for unphotographed rooms.

**The lighting inference is weak.** Guessing day/evening/night from the ratio
of fixture power to glazing is a heuristic; it is marked `inferred` and any
model answer overrides it, but with no model answer it can be wrong.

**Camera fitting is approximate.** Viewpoints are fitted from a horizon
estimate and a field-of-view bucket, not recovered by structure-from-motion. A
render from a fitted pose will not align pixel-for-pixel with the photograph,
which is why the layout axis compares a coarse 8×6 mass grid rather than
attempting registration.
