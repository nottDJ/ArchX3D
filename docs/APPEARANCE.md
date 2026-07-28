# ArchX3D — Blender appearance layer (v2.4)

How the scene graph's appearance information reaches the render.

Style, species-level materials, colour palettes, lighting environments and
fitted camera poses had all been extracted and persisted, and Blender ignored
every one of them — building flat colours, a generic light rig and an estimated
camera. This phase closes that gap.

**Geometry is untouched.** The DXF remains the single source of truth for
shape. Everything here decides only how that shape is shaded, lit and framed.

---

## 1. Architecture

```
modules/
├── blender_generator.py      orchestrator — loads, sequences, exports
├── blender_furniture.py      procedural furniture geometry (unchanged)
└── blender/
    ├── __init__.py
    ├── colour.py             colour maths                    ← no bpy
    ├── palette.py            palette application + realism   ← no bpy
    ├── styles.py             style → material/lighting policy← no bpy
    ├── materials.py          procedural shader node graphs      bpy
    ├── lighting.py           sun · sky · fixtures               bpy
    └── camera.py             ViewPoint → cameras                bpy
```

### The split that matters

Two tiers, divided on whether `bpy` is required:

**Decision tier** (`colour`, `palette`, `styles`) — *what should this look
like?* Is that tint believable for walnut? Which species does an industrial
room use for generic metal? No Blender import, so it runs and is **tested**
outside Blender. 112 tests cover it.

**Construction tier** (`materials`, `lighting`, `camera`) — node graphs, light
datablocks, camera objects. Consumes the decisions above.

That split is what makes the appearance rules testable at all. The interesting
judgements are all in the first tier, and none of them need a running Blender.

### Data flow

```
scene_graph.json
   │
   ├── room.style ─────────────► styles.profile_for ──┐
   ├── room.palette ───────────► palette.for_*        ├──► materials.MaterialLibrary
   ├── object.material ────────► styles.resolve ──────┘         │
   │   (species from catalog)                                   ▼
   │                                                    procedural node graph
   ├── room.lighting ──────────► lighting.build ──► sun + sky + fixtures
   └── graph.viewpoints ───────► camera.build ────► one camera per reference
```

---

## 2. `colour.py` — colour maths

Three spaces, and mixing them up is the usual cause of a washed-out render:

* **sRGB hex** — what the scene graph stores.
* **Linear RGB** — what Blender's shader inputs expect. Feeding them sRGB is
  the single most common way to get a subtly over-bright render.
* **HLS** — used by the tinting rules, because "shift the hue a little but keep
  the lightness" is not expressible in RGB.

`mix()` blends in **linear** space: mixing two mid-tones in sRGB darkens the
result, the classic "blend of two greys is not the grey between them" artefact.

`kelvin_to_rgb()` is **normalised so the brightest channel is 1.0**. A light's
brightness is its energy, not its colour; letting colour carry brightness makes
warm lights silently dimmer than cool ones at the same wattage.

---

## 3. `palette.py` — bounded tinting

The problem: applying a palette naively repaints everything and you get blue
walnut. Applying it not at all wastes the information.

So tinting is **bounded by material realism**. Each family declares a
`TintBudget` — how far its hue, lightness and saturation may move:

| Family | Hue budget | Rationale |
|---|---|---|
| `paint_*` | replaceable | A wall colour *is* a choice. Set outright. |
| `wallpaper`, `tile`, `plastic` | 0.30–0.40 | Manufactured in any colour |
| `fabric` | 0.30 | Upholstery genuinely varies |
| `leather`, `laminate` | 0.05–0.06 | Limited real range |
| `wood`, `marble`, `granite`, `concrete` | 0.02–0.03 | Warmth only |
| `metal` | 0.015 | A metal's colour is its alloy |

Species inherit their family's budget: `walnut` gets timber's physics because
walnut *is* timber, with no per-species entry needed.

Measured behaviour against a warm terracotta palette:

| Material | Base | Tinted | Moved |
|---|---|---|---|
| paint_matte | `#EFEDE8` | `#D9CDC4` | 0.119 |
| velvet | `#4A3B52` | `#6A4D51` | 0.080 |
| linen | `#9C978E` | `#9E8F82` | 0.032 |
| walnut | `#5C4033` | `#674737` | 0.030 |
| white_marble | `#F1EEE8` | `#EDE9E3` | 0.018 *clamped* |
| brass | `#B08D45` | `#AF8C46` | 0.004 |

### Two subtleties worth recording

**Clamping happens in HLS, not RGB.** The constraint is perceptual — what must
survive is *which material this looks like*, and that lives almost entirely in
hue. Clamping RGB channels independently would let a colour drift across the
hue wheel with every channel inside its numeric limit.

**The budget is enforced on the emitted 8-bit colour, not an intermediate.**
The HLS clamp is exact in continuous space, but quantising to `#RRGGBB` nudges
components back outside budget by a thousandth or so. `_overshoot` measures the
quantised result and walks the deltas back until it is genuinely inside.

**Hue is exempt for near-neutral colours.** For `#22201F` a one-bit change
swings the reported hue across a large fraction of the wheel while the colour
stays visually identical; enforcing a hue budget there chases rounding noise
and never converges. What protects a neutral material is the *saturation*
budget — black marble must not become blue marble — and that is enforced
regardless. Threshold: `NEUTRAL_SATURATION = 0.12`.

### Surface rules

Walls follow the palette fully, floors at 0.55, ceilings at 0.30 — a ceiling
painted the full wall colour reads as a cave. A ceiling that would end up
darker than it started is left alone entirely.

Furniture follows weakly (0.35): its colour was usually *observed*, and an
observation outranks a scheme derived from the same room. Decor follows more
closely (0.60), because small accent objects are what an accent role describes.

---

## 4. `styles.py` — style policy

A style is allowed to do exactly one thing: **resolve ambiguity**. It never
overrides an observation.

```
resolve_material(observed, style, surface, confidence)

  observed species ("walnut")  → walnut          [observed]  ← always wins
  observed family  ("wood")    → white_oak       [style]     ← refined
  observed family, weak style  → wood            [observed]  ← not refined
  nothing observed             → exposed_brick   [style]     ← style default
  nothing, no style            → paint_matte     [default]
```

Refinement requires `confidence >= 0.5` — a stray adjective should not restyle
a room.

14 profiles. Each declares family→species substitutions, per-surface fallbacks,
interior colour temperature, light gain, shadow-softness bias and trim colour:

| Style | Substitutions | Interior | Gain |
|---|---|---|---|
| industrial | metal→blackened_steel, concrete→grey_concrete, wood→walnut | 2600 K | 0.85 |
| mid_century | wood→teak, fabric→wool, metal→brass | 2750 K | 0.95 |
| scandinavian | wood→white_oak, fabric→wool, metal→brushed_steel | 3600 K | 1.15 |
| luxury | marble→white_marble, fabric→velvet, metal→brass | 2700 K | 0.90 |
| japanese | wood→ash, fabric→linen, paint→limewash | 3000 K | 0.85 |

`decor_density` governs how much *low-confidence* clutter a style tolerates —
a minimalist reconstruction full of uncertain vases reads wrong even when every
vase was genuinely detected. It never removes anything the pipeline is
confident about.

---

## 5. `materials.py` — procedural node graphs

No downloaded textures. A scanned pack is hundreds of megabytes,
licence-encumbered, and still wrong for the species you need. Procedural graphs
are kilobytes, resolution-independent and **parameterisable**, so one wood
recipe covers oak, walnut, teak, mahogany and ebony by moving colours and grain
contrast.

Nine recipes cover all 61 materials. `MaterialPrior.texture` names the recipe,
`grain` scales its strength:

| Recipe | Technique | Covers |
|---|---|---|
| `wood_grain` | Distorted wave bands + fibre noise, two-tone ramp, roughness varies with growth ring | oak, walnut, teak, maple/ash, mahogany, ebony, birch |
| `veined` | Sparse high-contrast noise veins over a mottled field | white/black/green marble, travertine |
| `speckle` | Voronoi chips in a matrix | terrazzo, granite |
| `weave` | Crossed waves + fuzz, **sheen** | linen, velvet, bouclé, wool, cotton, carpet, jute |
| `brick` | Brick node with mortar | exposed brick |
| `tiled` | Brick node squared up, grout drives roughness | porcelain, ceramic, subway, mosaic |
| `brushed` | Directional noise → **roughness only** | brushed steel, brass, blackened steel, chrome |
| `noise` | Two-scale blotching | concrete, polished concrete, stone, slate |
| `grain` | Fine pebbled bump | leather |
| `flat` | Plain, with faint roller tooth | paint, gypsum, glass, plastic |

### Design notes

**Object-space coordinates, not Generated or UV.** Procedural furniture is
built without UVs, and Generated coordinates normalise to each object's
bounding box — which would make wood grain finer on a stool than on a table.
Object space keeps grain at a consistent real-world size across the room.

**Brushed metal varies roughness, not colour.** A metal's colour is its alloy;
what a brushed finish changes is how the highlight stretches.

**Sheen is what makes fabric look like fabric.** Textiles scatter at grazing
angles; without sheen, velvet renders as painted cardboard.

**Flat is not literally flat.** Even matte emulsion has roller texture, and a
perfectly uniform wall is one of the strongest cues that a render is CG.

### Version tolerance

Blender renames Principled sockets between releases and removed the Musgrave
node in 4.1. Every write goes through `_set_input(node, [names...], value)`,
which tries a list and skips silently; `_new_mix` prefers the legacy
`ShaderNodeMixRGB` for its stable named sockets and falls back to the generic
`ShaderNodeMix`. Musgrave is avoided entirely.

Verified on Blender 5.0 — all seven renamed sockets resolve:

```
Sheen Weight -> Sheen Weight          Transmission Weight -> Transmission Weight
Specular IOR Level -> Specular IOR Level   Coat Weight -> Coat Weight
Anisotropic -> Anisotropic            Sheen Tint -> Sheen Tint
```

---

## 6. `lighting.py` — three contributions

Interior lighting is dominated by what *bounces*, not by what emits. A room lit
only by the lamps visible in a photograph renders far darker and harder than
the photograph, which also contains daylight, light from the next room, and
inter-reflection the fixture list never mentions. The environment terms stand
in for all of that.

**1. Sun** — aimed through the room's *actual* glazing using the recorded plan
heading and elevation. Shadow softness maps to the sun's angular diameter
(0.53° sharp → 11° diffuse), which is the physically correct control: a bigger
apparent disc gives a wider penumbra.

**Skipped entirely when `daylight_direction == -1`.** The pipeline is saying it
does not know where the windows face; inventing a sun would throw hard shadows
across the room in a direction nothing supports.

**2. Sky** — world background from ambient level and time of day. This is what
fills shadow; without it interiors render with black corners regardless of how
many lamps are placed. Overcast is *brighter* than clear day, because the sky
is then the light source.

**3. Fixtures** — observed luminaires with catalog photometry, scaled by the
style's gain and **damped by daylight**: lamps carry a room after dark and are
largely invisible at noon.

One environment per scene, taken from the largest room that has one. Sun
direction and time of day are properties of the *building*; a walkthrough that
changed time of day between rooms would look broken.

Units: point and area lights are in watts, matching the catalog photometry, so
fixture power passes through directly. Sun strength is W/m² — a different
quantity — and is derived from the environment, never from a fixture.

---

## 7. `camera.py` — exact viewpoint reproduction

`grounding.estimate_camera` fitted a camera to each reference photograph to
back-project furniture. That pose is now persisted, and rebuilt here rather
than re-estimated. **Nothing in this module estimates anything.**

Three conventions meet, and none agree:

| | Convention |
|---|---|
| ArchX3D | `yaw` 0 = looking along **+Y**; positive `pitch_deg` is up |
| Blender | camera looks along local **−Z**, up is +Y |
| FOV | graph stores **vertical**; Blender defaults to horizontal fit |

```python
rotation_euler = (radians(90 + pitch_deg), 0, radians(yaw))
data.sensor_fit = "VERTICAL"
data.angle_y = radians(vertical_fov_deg)
```

Leaving `sensor_fit` on `AUTO` silently reinterprets a vertical FOV as
horizontal on any landscape render, cropping to a much narrower view than the
photograph had. `render_resolution_for()` matches the viewpoint's aspect, so a
4:3 photograph's viewpoint is not rendered into a 16:9 frame with scene at the
sides the photograph never contained.

Verified in Blender 5.0:

```
yaw=  0 -> (+0.000,+1.000,+0.000)  OK      pitch=-25 -> z=-0.423 (down)  OK
yaw= 90 -> (-1.000,+0.000,+0.000)  OK      pitch=  0 -> z=+0.000 (level) OK
yaw=180 -> (+0.000,-1.000,+0.000)  OK      pitch= 25 -> z=+0.423 (up)    OK
yaw=270 -> (+1.000,+0.000,+0.000)  OK      sensor_fit=VERTICAL angle_y=62.00
```

These cameras are **not** made active. They exist so a preview can be rendered
from each and compared against its reference by `vision.similarity`.

---

## 8. Integration points

| Scene graph field | Consumed by | Effect |
|---|---|---|
| `room.style`, `style_confidence` | `styles`, `materials` | Species substitution, lighting bias, trim |
| `room.palette` | `palette`, `materials` | Bounded tinting of every surface and object |
| `room.lighting` | `lighting` | Sun direction/elevation, sky strength, CCT, softness |
| `room.*_finish` | `materials` | Per-room surfaces, ahead of graph-level |
| `object.material` | `styles`, `materials` | Species → recipe |
| `object.color_hex` | `palette`, `materials` | Base colour before tinting |
| `object.asset` | `blender_furniture` | Variant geometry + params (unchanged) |
| `graph.viewpoints` | `camera` | One exact camera per reference image |

Every path degrades: a graph with no palette, no lighting environment and no
viewpoints — the format written before this phase — still builds. Verified
against the repository's existing 26-object graph.

---

## 9. Testing

```bash
python -m pytest tests/ -q          # 328 passing, 5 skipped
```

`tests/test_blender_appearance.py` — 112 tests over the bpy-free tier. The
central one is exhaustive: **every one of the 61 materials against four
deliberately hostile palettes** (cold blue, saturated red, acid green,
black/white/magenta), across five surface roles, asserting hue and saturation
stay inside budget.

The construction tier is verified by running Blender 5.0 headless:

```bash
blender --background --factory-startup --python modules/blender_generator.py
```

Verified this way: all 61 materials build with zero failures and the correct
recipe; all renamed Principled sockets resolve; physical traits land (velvet
sheen 0.44, glass transmission 0.92, brass metallic 0.90 + anisotropic 0.55,
marble coat 0.25); camera yaw/pitch/FOV exact; full pipeline runs on both a
legacy graph and an enriched one.

---

## 10. Limitations

**The construction tier has no automated test.** It is verified by running
Blender manually. Wiring `blender --background` into pytest would need Blender
on `PATH` in CI, which the project does not currently assume.

**Materials are not visually validated.** The graphs build and their parameters
are correct, but nothing checks that the walnut recipe *looks* like walnut. The
similarity engine could close this loop once preview rendering exists.

**No preview render pass.** `camera.build_viewpoint_cameras` creates the
cameras; nothing yet renders from them. This remains the missing piece between
this phase and an automatic refinement loop.

**`decor_density` is computed but unused.** The generator does not yet thin
low-confidence clutter in minimal interiors.

**One lighting environment per scene.** Per-room time of day is deliberately
not supported, but per-room *ambient* would be reasonable and is not done.

**Object-space texture coordinates assume sane object scale.** An object with a
non-uniform scale applied to its transform rather than its mesh would show
stretched grain. The furniture builders apply scale to geometry, so this does
not currently arise.
