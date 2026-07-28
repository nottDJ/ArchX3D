# ArchX3D — Render evaluation pipeline (v1.0)

Deterministic, cacheable, incremental preview renders — one per stored
`ViewPoint` — produced so the reconstruction can be **measured** rather than
admired.

These images are **not deliverables**. They exist to be scored by
`vision.similarity` against the photographs their cameras were fitted to. That
makes them instruments, and instruments have requirements a product render does
not: reproducible to the byte, cheap enough to regenerate in a refinement loop,
and invalidated precisely when — and only when — something that could change
them changed.

**Generation is never repeated here.** The `.blend` written by
`blender_generator` is the input, loaded and rendered as-is.

---

## 1. Architecture

```
modules/
├── blender_generator.py       + render_previews() hook, nothing else changed
└── render/
    ├── __init__.py
    ├── cache.py               scene hashing + persistent cache   ← no bpy
    ├── manifest.py            the record similarity.py reads     ← no bpy
    ├── scheduler.py           batching + execution strategy      ← no bpy
    ├── preview.py             orchestration + CLI                ← no bpy
    ├── renderer.py            settings + Blender invocation      ← no bpy
    ├── passes.py              auxiliary pass codec               ← no bpy
    ├── _blender_render.py     beauty render, in Blender             bpy
    └── _blender_passes.py     auxiliary passes, in Blender          bpy
```

The same two-tier split the `blender` package uses, and for the same reason:
every decision worth testing — what to render, what to skip, what to record —
is made outside Blender, so the whole invalidation model is unit-testable in
under a second.

### Data flow

```
  data/scene_graph.json ──┐
  data/geometry.json ─────┼──► cache.compute() ──► scene_hash
  config.preview ─────────┘                       room_hash[room]
                                                  camera_hash[viewpoint]
                                                          │
                                                          ▼
                                              ┌───────────────────────┐
                                              │  cache/hash.json      │
                                              │  key match + image?   │
                                              └───────┬───────────────┘
                                            hit ◄─────┴─────► miss
                                             │                 │
                                             │                 ▼
                                             │        scheduler.partition()
                                             │                 │
                                             │                 ▼
                                             │        ┌────────────────────┐
                                             │        │ SequentialScheduler│
                                             │        │ ThreadedScheduler  │
                                             │        │  (→ pool → farm)   │
                                             │        └────────┬───────────┘
                                             │                 │ one batch
                                             │                 ▼
                                             │      blender --background \
                                             │        output/scene.blend  \
                                             │        --python _blender_render.py
                                             │                 │
                                             ▼                 ▼
                                        ┌──────────────────────────┐
                                        │  preview/<room>/*.png    │
                                        │  preview/manifest.json   │
                                        └────────────┬─────────────┘
                                                     ▼
                                            vision.similarity  (next phase)
```

### Output layout

```
output/preview/
├── room_a/
│   ├── viewpoint_01.png              the beauty render
│   ├── viewpoint_01_albedo.png       auxiliary passes, one per viewpoint
│   ├── viewpoint_01_depth.png
│   ├── viewpoint_01_normal.png
│   ├── viewpoint_01_material_id.png
│   ├── viewpoint_01_object_id.png
│   └── viewpoint_02*.png
├── room_b/
│   └── viewpoint_01*.png
└── manifest.json

.cache/render/hash.json          invalidation state, never a deliverable
```

`NN` is the viewpoint's position within its room when that room's viewpoints
are sorted **by image id** — not by graph order, which is not stable because
the vision pipeline analyses images concurrently. Unstable names would rename
files between runs and defeat the cache entirely.

---

## 2. `preview.py` — orchestration

Three entry points that differ only in *which* viewpoints they select:

```python
render_scene()                  # every viewpoint in the graph
render_room("room_a")           # one room's viewpoints
render_viewpoint("img_a1")      # exactly one
```

Everything downstream is shared, which is what guarantees
`render_room("room_a")` writes the same file, under the same name, with the
same content that `render_scene()` would have. The numbering is computed from
the *whole* graph even on a scoped run, for exactly that reason.

A scoped run still rewrites the manifest, and carries the records it did not
touch through untouched: the similarity pass reads the manifest as the complete
picture of a build, so a run that quietly narrowed it would be
indistinguishable from a build that had lost half its previews.

The executor is injected. Production passes a `SubprocessRenderer`,
`blender_generator` passes an `InlineRenderer` because it is already inside
Blender, and the tests pass a function. Nothing in `PreviewPipeline` knows
Blender exists.

### CLI

```bash
python modules/render/preview.py                      # whole building
python modules/render/preview.py --room room_a
python modules/render/preview.py --viewpoint img_a1
python modules/render/preview.py --force              # ignore cache hits
python modules/render/preview.py --width 1280 --samples 64
```

Standalone so a preview pass can be re-run against an existing `.blend` without
re-running generation — the normal case while tuning materials or lighting.

---

## 3. `cache.py` — scene hashing

The heart of the pipeline. Three independent digests are combined per preview:

```
key = H( pipeline + settings , scene_hash , room_hash[room] , camera_hash , image_path )
```

| Digest | Covers |
|---|---|
| `scene_hash` | schema version, `geometry.json` bytes, graph-level floor/ceiling finishes, dominant style, architecture elements, the room roster, and **everything unattributable to a room** |
| `room_hash` | one room's record (finishes, palette, lighting environment, style, bounds, ceiling), its walls, objects, luminaires and openings |
| `camera_hash` | one viewpoint's position, yaw, pitch, vertical FOV, aspect and pixel grid |

### Attribution

Objects, lights and openings are bucketed by `room_id`; walls by the rooms
whose `wall_ids` claim them, so a party wall correctly invalidates both rooms
and only those two.

Anything the graph does **not** attribute to a room — an object with no
`room_id`, an architectural element, a wall no room claims — folds into
`scene_hash` and therefore invalidates the whole building. Conservative on
purpose: an unattributable change could be visible from anywhere, a needless
re-render costs a few hundred milliseconds, and a stale evaluation image costs
a wrong score.

### Why hash the inputs and not the artefact

Hashing the `.blend` would give a cache that never hits: Blender embeds paths
and timestamps, so regenerating an identical scene produces a different file.
Hashing timestamps is worse for the same reason. So the digest is taken over
what *produces* the image — the graph, the DXF geometry, the settings and the
pipeline version. `--force` is the escape hatch for the rare case where the
`.blend` changed in a way the graph does not describe.

### The known gap

A room's hash covers that room only, so repainting the kitchen does not
re-render a living-room view that sees the kitchen through a doorway. This is
accepted by default: these are per-viewpoint evaluation renders scored against
a photograph of *that* room, and invalidating neighbours transitively degrades
toward "one room invalidates the building" in an open-plan layout — the exact
failure this design exists to avoid.

`include_neighbours: true` trades it back, one hop, no transitivity.

### Stability

Records are serialised in id order and floats rounded to six decimals, so
concurrent analysis order and JSON round-trip noise cannot produce a spurious
miss. `canonical()` sorts keys; NaN and ±inf are made representable rather than
allowed to poison a key.

---

## 4. `cache.py` — the persistent store

`.cache/render/hash.json`, one entry per viewpoint, rewritten atomically
(temp file + `os.replace`) because several batches may finish at once and a
torn file would read back as a total cache loss.

A hit requires **both** a matching key and a readable, non-empty image.
Deleting a PNG to force a re-render is a thing people do, and it works. A
corrupt or older-format cache reads as empty — the worst case is a slow run,
and that is always recoverable. A failed render actively *forgets* its entry,
so a failure can never register as a hit on the next pass.

---

## 5. `manifest.py` — the interface to similarity

`preview/manifest.json` is how the next phase learns which PNG belongs to
which photograph:

```json
{
  "viewpoint_id": "img_a2",
  "room": "room_a",
  "image": "room_a/viewpoint_02.png",
  "source_image": "img_a2.jpg",
  "camera_hash": "5f3c…",
  "scene_hash": "821a…",
  "room_hash": "ebb0…",
  "width": 640, "height": 360,
  "timestamp": "2026-07-26T16:58:24Z",
  "render_ms": 241,
  "status": "rendered",
  "camera_source": "blend"
}
```

`image` is relative to the manifest's own directory and forward-slashed, so a
manifest written on Windows resolves on a Linux farm node. The hashes are in
there for debugging as much as for provenance: when a preview unexpectedly did
or did not re-render, comparing them against a freshly computed set localises
the change immediately — building-wide, one room, or one camera.

A cache hit keeps its original `timestamp` and `render_ms`: they describe when
the *image* was made, and overwriting them with this run's clock would make a
cached preview look freshly rendered.

Records are stored sorted by `(room, viewpoint_id)`, so two runs of an
unchanged scene differ only in `generated_at`.

---

## 6. `scheduler.py` — execution strategy

The expensive constant is process startup: Blender takes ~1–3 s to start and
load a furnished scene, against ~250 ms to render a 640×360 EEVEE frame from
it. So the default is **not** one process per viewpoint — it is one process per
*batch*, with every viewpoint in it.

```python
partition(tasks, workers=1, group_by_room=False, max_per_batch=None) -> [Batch]

class Scheduler:
    def run(self, batches, execute) -> [RenderOutcome]

SequentialScheduler   one batch after another (default)
ThreadedScheduler     batches concurrently — each is an external process, so
                      the threads spend their lives blocked in wait()
```

`run` takes batches and an executor callable rather than individual tasks: a
process pool or a render farm changes how batches are *dispatched* without
changing what a batch is, and a test can substitute a fake executor and assert
on scheduling with no Blender anywhere.

Worth knowing before turning `workers` up: each worker pays the scene-load and
shader-compilation cost again, so past two or three the wall-clock gain
flattens and can reverse.

**Failure isolation.** An executor that raises fails its own batch and nothing
else; every task in it gets an outcome carrying the error, and tasks the
executor forgot to report are filled in as failures. A silent gap in the
manifest would be worse than a recorded failure.

---

## 7. `renderer.py` + `_blender_render.py` — the Blender boundary

`renderer.py` holds `RenderSettings` (a plain dataclass, fingerprinted into
every cache key) and `SubprocessRenderer`, which writes the batch to a job file
and runs:

```bash
blender --background --factory-startup output/scene.blend \
        --python modules/render/_blender_render.py -- --job job.json
```

`--factory-startup` because a user's preferences, add-ons or colour-management
overrides must not be able to reach an evaluation render. The batch goes in a
file rather than argv because twenty cameras is more than argv quoting rules
survive across platforms, and results come back in a file rather than on stdout
because Blender prints freely to stdout and parsing structure out of that is a
losing game.

`InlineRenderer` is the same executor interface for the in-process case, used
by `blender_generator` — the file it just wrote and the scene in memory are the
same scene, and a second launch would roughly double the cost of a pass.

### Cameras are never estimated

Two acceptable sources, in order:

1. the `Ref_<image_id>` camera the generator already built into the `.blend`;
2. a camera rebuilt from the stored `ViewPoint` by `blender.camera` — the same
   function the generator used.

If neither is available the task fails and says so. Which path ran is recorded
as `camera_source`. Guessing a camera would produce an image that looks fine
and scores meaningless.

---

## 7b. Auxiliary passes

The evaluation engine (Phase 3) needs more than a beauty render, so each
viewpoint also emits five data passes. They are configured by
`preview.passes` and cost one extra render each.

| Pass | Encoding | Read by |
|---|---|---|
| `albedo` | sRGB, unlit | material axis; colour and lighting attribution |
| `depth` | `byte / 255 × depth_range` metres | layout evidence |
| `normal` | `byte / 255 × 2 − 1`, world space | geometry consistency |
| `material_id` | index across R (low byte) and G (high) | material localisation |
| `object_id` | index across R and G | object localisation |

**PNG, not EXR.** Blender 5.0's File Output node emits only multilayer OpenEXR,
which Python cannot read without a binary dependency. Instead each quantity is
rendered through the ordinary path with the value carried in emission colour
and the `Raw` view transform on, so the linear value reaches the byte
unaltered — verified: linear `7/255` decodes back to exactly `7`.

**Point-sampled.** Data passes render at one sample with a degenerate filter
width. Anti-aliasing at a silhouette averages *indices*, producing pixels that
claim to be a material which does not exist; it likewise averages 2 m and 6 m
into a surface that is not there. Measured effect on a furnished room: 45
material indices in the frame before the fix, 5 after, none unresolvable.

**Index maps.** An ID pass is an anonymous blob without the mapping from index
to object or material name. It is a property of the build rather than of a
viewpoint, so it is written once into `manifest.stats.pass_index`.

Cache correctness: the pass list is part of the settings fingerprint, so
changing it re-renders everything once, and a preview whose pass files are
missing is treated as a miss — an evaluation input that has lost its depth pass
is not usable however current the beauty render is.

See [`docs/EVALUATION.md`](EVALUATION.md) for what each pass is used for.

---

## 8. Determinism

Two identical scenes must produce identical preview images, or a similarity
score cannot distinguish "the material changed" from "the sampler rolled
differently". Every setting below is pinned by `_blender_render.apply_settings`.

| Setting | Value | Why |
|---|---|---|
| engine | `BLENDER_EEVEE[_NEXT]` | Rasterised — no path-tracing noise at all |
| `taa_render_samples` | 16 | Fixed count; TAA is deterministic given one |
| TAA reprojection | off | Carries state between frames |
| EEVEE raytracing | off | Screen-space tracing is accumulated *and* denoised |
| shadow / bokeh jitter | off | Stochastic by construction |
| motion blur | off | Samples across time; irrelevant to a still |
| frame | 1, fixed | The walkthrough orbit is keyframed — the frame changes the scene |
| `view_transform` | `Standard` | Filmic/AgX tone-map, and *which* is default changed between Blender versions |
| look / exposure / gamma | `None` / 0 / 1 | Each silently rescales every pixel |
| curve mapping | off | Per-file state that survives in a `.blend` |
| white balance | off | Introduced in 4.5; defaults could drift |
| display device | sRGB | The manifest promises sRGB PNGs |
| `dither_intensity` | 0.0 | Blender dithers 8-bit output by default, putting ±1 LSB noise on exactly the flat surfaces the colour axis reads |
| `filter_size` | 1.5 | Explicit — it is per-scene state the `.blend` could carry |
| compositing / sequencer | off | A stray node tree must not post-process an evaluation image |
| `resolution_percentage` | 100 | Per-scene state; 50% would halve the output |
| stamp metadata | all off | `Date` and `RenderTime` are wall-clock and are written into PNG text chunks even with burn-in off — enough to defeat a byte comparison |
| Cycles seed / denoising | 0 / off | For the optional Cycles path |

Verified: two separate Blender processes rendering the same scene produce
**byte-identical** PNGs (`tests/test_render_blender.py`).

**Scope of the guarantee.** Same machine, same Blender build, same inputs, same
pixels. A different GPU driver or platform may still differ in a
least-significant bit; that is not something a renderer can promise, and it is
not what a regression test or a refinement loop needs. `RENDER_PIPELINE_VERSION`
is folded into the settings fingerprint, so changing anything in this table
re-renders everything exactly once.

---

## 9. Performance

Measured on the reference machine (Blender 5.0, EEVEE, 640×360, 16 samples, a
26-object furnished scene with 39 materials):

| Case | Measured | Target |
|---|---|---|
| Second and later viewpoints in a batch | **228–313 ms** each | < 300 ms per room |
| Whole building, warm shader cache | **~5.2 s** for 2 viewpoints incl. Blender startup | < 5 s medium house |
| Whole building, cached | **7 ms** | < 500 ms |
| First render in a cold process | 3–16 s | — |
| Each auxiliary pass, after the first | **195–280 ms** | — |

With all five passes enabled a viewpoint costs roughly 1.3 s instead of 250 ms.
They are configurable per run for that reason: a pass nobody reads is a pass
worth switching off.

The first render in any process pays EEVEE's shader compilation for every
material in the scene (~3 s warm, up to ~16 s on a cold on-disk shader cache).
It is per-process, which is the whole argument for batching: a five-viewpoint
building costs `startup + compile + 5 × 250 ms`, not five times that.

Blender 5.0's `SUBPROCESS` shader compilation was measured at ~10× *worse* in
background mode (32 s vs 3.3 s), so the default `THREAD` method is left alone.

---

## 10. Integration

### From the generator (automatic)

`blender_generator.main()` calls `render_previews(graph, config)` after export.
It is the only change to that file. In-process, so no second Blender starts;
skipped entirely when `ARCHX3D_SKIP_PREVIEW=1` or when the graph carries no
viewpoints (no reference photographs were supplied — a legitimate state, not a
failure). A preview failure never fails the build.

### Standalone (incremental)

```bash
python modules/render/preview.py --room room_a
```

Reads `data/scene_graph.json` and `output/scene.blend`, renders only what
changed, updates the manifest and cache.

### Configuration — `config.json`

```json
"preview": {
    "width": 640,
    "height": 360,
    "match_aspect": true,
    "engine": "EEVEE",
    "samples": 16,
    "transparent": false,
    "view_transform": "Standard",
    "scheduler": "sequential",
    "workers": 1,
    "group_by_room": false,
    "cache_enabled": true,
    "include_neighbours": false,
    "timeout": 600,
    "passes": ["albedo", "depth", "normal", "material_id", "object_id"],
    "depth_range": 20.0
}
```

Unknown keys are ignored rather than fatal — this file is hand-edited, and a
typo should not stop an evaluation pass. Blender is located via
`preview.blender_executable`, then `ARCHX3D_BLENDER`, then `main.py`'s path,
then `PATH`, then the usual install locations.

`match_aspect` is on by default: a 4:3 photograph's viewpoint rendered into a
16:9 frame shows scene the photograph never contained, and the similarity
engine's layout axis would score that as a difference. Width is held fixed so
every preview costs the same; only the height moves.

### What the next phase consumes

`vision.similarity.compare()` takes `(viewpoint, reference_path,
rendered_path)` triples. The manifest supplies the third element and the
pairing: `record.source_image` is the reference, `manifest.resolve(record)` is
the render. Nothing in this phase scores anything.

---

## 11. Testing

```
tests/test_render_hashing.py     31 tests   invalidation model
tests/test_render_cache.py       14 tests   hit/miss, persistence, pruning
tests/test_render_manifest.py    14 tests   records, ordering, paths
tests/test_render_scheduler.py   19 tests   batching, isolation, concurrency
tests/test_render_pipeline.py    36 tests   integration with a fake executor
tests/test_render_passes.py      31 tests   the auxiliary pass codec
tests/test_render_blender.py      9 tests   real Blender, incl. passes (opt-in)
```

The first five run in under a second and need no Blender. The regression cases
the whole design exists for are in `test_render_pipeline.py`:

| Change | Re-renders |
|---|---|
| an object's material | that room's viewpoints only |
| a room's lighting environment | that room's viewpoints only |
| a camera's yaw | that viewpoint only |
| a graph-level floor finish | everything |
| render settings | everything |
| deleting a preview file | that preview only |
| deleting one auxiliary pass | that preview only |
| changing the pass list | everything |

The Blender smoke test is opt-in because it costs ~26 s:

```bash
ARCHX3D_RENDER_INTEGRATION=1 python -m pytest tests/test_render_blender.py -v
```

It checks the one thing a fake cannot: that the settings in §8 are real
properties on a real Blender, that a camera rebuilt from a stored `ViewPoint`
renders, and that the output is reproducible to the byte.

---

## 12. Limitations

* **Cross-room visibility.** A view through a doorway does not invalidate when
  the room it sees into changes (§3). `include_neighbours` trades this for
  broader invalidation.
* **Cross-machine determinism.** Guaranteed per machine and Blender build, not
  across them (§8).
* **The `.blend` is trusted.** The hash is over the inputs that produce it. A
  hand-edited `.blend` needs `--force`.
* **No similarity scoring.** Deliberately out of scope for this phase; this
  pipeline only produces what `vision.similarity` will consume.
