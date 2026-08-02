# CAD ↔ Image Registration

How ArchX3D works out where a reference image sits relative to the drawing,
and what it does when it cannot.

---

## 1. The problem

Two inputs describe the same building in two coordinate systems that nothing
relates.

| | Coordinate system | Authoritative about |
| --- | --- | --- |
| DXF | metres, origin-normalised, +Y up | structure, function, dimensions |
| Reference image | normalised pixels, origin top-left, +v down | appearance |

Everything that mixes them has to bridge that gap. Until this package existed,
both bridges were assumptions:

* **Plan views.** `grounding.ground_plan_view` stretched the whole image onto
  the plan's bounding box. Correct only when the image is one floor plan
  filling the frame.
* **Interior views.** `assignment` matched a photograph to a room by comparing
  floor areas — while the room type the drawing *states* sat unread on the
  region record.

Both are guesses, and the project's first principle is that a guess is only
acceptable where no reliable information exists. In both cases it did.

### The failure this was built from

A reported case, recorded in [`SEMANTIC_PIPELINE.md`](SEMANTIC_PIPELINE.md).
The upload was a composite sheet: an exterior render across the top ~45%, and
two floor plans side by side below it. It classified correctly and routed to
the plan-view path — and then lost all eleven detections. The full-frame
assumption was off by ~2.4× in x and ~1.9× in y, so every object landed
outside every room polygon and was discarded, one at a time, in silence.

The error message read *"11 of 11 rooms have no reference imagery"*.

---

## 2. The idea

Registering two views of a building means finding the same thing in both. The
obvious candidates — corners, wall junctions, door openings — are repeated
dozens of times per plan and are essentially featureless, so matching them is
a combinatorial problem with nothing to break ties.

Room labels are the opposite: sparse, textual, and **already extracted on both
sides**. `cad.text` parses them out of the DXF with plan coordinates; a vision
model reads them off the image with pixel coordinates. The architect printed
the correspondence into the drawing. The job is only to notice it.

```
   DXF                                    Reference sheet
   ───                                    ───────────────
   CadText "KITCHEN 12.00 SQ.M."          "KITCHEN"  at (u,v) = (0.71, 0.30)
     insert = (9.5, 6.5) m                "BATH"     at (0.71, 0.62)
   CadText "BATH 8.64 SQ.M."              "STUDY"    at (0.85, 0.30)  ← other floor
     insert = (9.5, 2.0) m
                    │                              │
                    └──────── candidates ──────────┘
                                  │
                          robust consensus
                                  │
                     x = a·u + b·v + tx
                     y = c·u + d·v + ty
```

Four parameters — scale, rotation, and two translations — separate the two
coordinate systems. Two correspondences determine them; three or more
corroborate them.

---

## 3. Package layout

`modules/registration/`, stdlib only and independent of both `cad` and
`vision` except for two adapter functions. The fitting machinery is therefore
exercisable from hand-built anchors, with no DXF, no imagery and no API key —
which is what makes a registration testable at all.

| Module | Responsibility |
| --- | --- |
| `schema` | `PlanTransform`, `Correspondence`, `SheetRegion`, `RegistrationResult`, `RoomRegistration` |
| `transform` | Closed-form weighted 2D similarity fit; residuals; inverse mapping |
| `labels` | Anchors from a `CadDocument` and from an `ImageObservation`; text matching; candidate generation |
| `consensus` | Exhaustive-sample RANSAC with a one-to-one constraint and local re-fitting |
| `plan` | Top-down sheets: the fallback ladder, sheet sub-region detection |
| `interior` | Perspective photographs: which region does this image show |

---

## 4. Plan-view registration

### 4.1 The ladder

Each rung is tried in turn, and the result records which one answered —
because *"we measured this"* and *"we assumed this"* must never look alike
downstream.

| Method | What it means | Registered? |
| --- | --- | --- |
| `label_consensus` | Two or more labels agreed on one transform | ✅ |
| `single_anchor` | Exactly one unambiguous label matched; scale assumed | ✅ (weakly) |
| `plan_bounds` | The legacy full-frame stretch. A guess, now labelled as one | ❌ |
| `none` | No transform. The caller drops the image's positions | ❌ |

`RegistrationResult.registered` is true only for the first two. Objects placed
through an assumed transform carry a `plan_registration_assumed` flag, so a
consumer can tell measured positions from inherited ones.

### 4.2 Why a similarity, not an affine

Six free parameters will always fit the data better than four. That is the
problem, not the point.

A floor plan is a uniformly scaled orthographic view of a floor. It is never
sheared and never stretched along one axis. Given four noisy correspondences,
an affine fit absorbs the noise into a plausible-looking shear and reports a
small residual — destroying the one number that would have warned us the match
is wrong. A similarity has nowhere to hide the error.

This is why the legacy fallback is detectable: stretching a non-square
building to fill a frame scales x and y independently, so
`PlanTransform.is_similarity` is `False` for exactly the transforms that were
assumed rather than measured.

### 4.3 Why the fit is robust

Label matching is ambiguous by construction. A flat with three bedrooms
produces three candidate pairings for one `BEDROOM` printed on the sheet, and
only one is true. Fitting all three averages a correct answer with two wrong
ones and produces a third answer, wrong everywhere.

Wrong pairings do not agree with each other. Any two correct pairings imply
the same transform; a wrong one implies a different transform and is
contradicted by everything else. So the fit maximises agreement rather than
minimising total error.

Three departures from textbook RANSAC:

1. **Sampling is exhaustive, not random.** A floor plan has tens of labels,
   not thousands of feature points, so every pair is enumerable — faster than
   randomising and completely deterministic. Determinism matters more than it
   sounds: a registration that varied between runs would move furniture
   between two builds of the same project, and no cache or diff downstream
   could be trusted.
2. **Consensus is one-to-one.** A transform may not count one image label
   twice, nor claim two image labels are the same CAD entity. Without this, a
   degenerate transform collapsing the plan to a point scores perfectly by
   matching everything to everything.
3. **The winner is re-fitted locally.** Two points give an exact, noise-bound
   fit; re-solving over the whole consensus set is where the accuracy comes
   from.

### 4.4 Independent validation

Any two correspondences can be satisfied exactly, including two that are both
wrong. What a wrong pair cannot do is keep the *rest* of the drawing on the
page. So the accepted transform is checked by inverting it and mapping the
building's own extent back into image space — a quantity no correspondence
contributed to. A fit that puts less than 30% of the drawing inside the frame
is rejected.

That same inverse mapping produces `SheetRegion`: the rectangle of the image
the plan occupies. Coverage below ~55% means the sheet carries something else,
which is reported with the actual coordinates rather than as a suggestion to
crop.

### 4.5 Multi-floor sheets

Correspondences that registered to nothing are kept and named. On an otherwise
good fit, a populated `unmatched_image_labels` is the signature of a second
plan on the page:

```
[REGISTER]   img0: label_consensus 84%, 6/9 labels matched, residual 0.11 m mean
[REGISTER]   ! the drawing occupies only 18% of this image (u 0.04-0.47,
               v 0.51-0.94); it is a composite sheet
[REGISTER]   ! 3 label(s) read in the image are not in this drawing (GUEST BED,
               STUDY, TERRACE); they most likely belong to another plan on the
               same sheet, whose furniture must not be read into this one
```

### 4.6 Practical details that turned out to matter

**Room labels carry their area.** Real drawings write `KITCHEN\P12.00 SQ.M.`
as one piece of text, while the sheet may print only `KITCHEN`. Both reduce to
a common `match_key` before comparison. The strip is bounded to two numeric
tokens so `BEDROOM 2 16.00 SQ.M.` keeps its room number — which is the only
thing distinguishing it from `BEDROOM 3`.

**`"unknown"` is not a room type.** It is what both parsers emit when they
cannot resolve a string. Treating it as a value makes every unresolved label
agree with every other one, manufacturing a candidate correspondence between
every pair of labels on the sheet.

**Title-block text is excluded.** `SCALE 1:100`, `DRAWN BY`, `GROUND FLOOR
PLAN` are printed on the sheet and do have positions — but the title block is
laid out per sheet, not per building, and in the test fixture it sits far
outside the plan extent entirely.

**Rotation is snapped.** Drawings are printed square to the sheet. A fit
returning 1.3° is reporting label-centroid noise, and keeping that tilt swings
placements at the far end of a long building by more than the error it came
from. The snap re-solves scale and translation with the angle held fixed, and
is discarded if it costs any agreement — a drawing genuinely printed at an
angle exists.

---

## 5. Interior-view registration

A perspective photograph shares no coordinate system with the plan, so there
is nothing to fit. But the question is the same question — which part of the
authoritative model does this picture describe? — and getting it wrong has the
same consequence.

### What was wrong

The pipeline classifies every room from the drawing first (labels, blocks,
layers) and stamps the answer onto each region. The code comment said this is
what lets a bedroom photo be matched to the region the drawing already calls a
bedroom. Then the matcher scored regions on floor area alone and never read
it — and the winning image *overwrote* the drawing's room type with its own.

That inverts the trust hierarchy at the point it matters most. CAD text is
tier 4; a vision impression is tier 6.

### The rule now

* A region the drawing names is matched on that name.
* Area plausibility is a **prior** — used where the drawing is silent, and to
  break ties. Never to overrule a stated fact.
* A CAD room type below 45% confidence is a hint, not a statement.
* When image and drawing disagree, **the drawing wins and the disagreement is
  recorded**, on `RoomRegistration.conflicts_with_cad` and as a warning:

  ```
  room_2: the imagery reads as a bedroom but the drawing names it a study
  (88%); keeping the drawing's answer and taking only appearance from the
  imagery
  ```

* When they agree, that is independent corroboration and the confidence rises
  above what either signal carried alone.

`RoomGroup.observed_room_type` keeps what the imagery alone concluded, so a
disagreement stays visible in review instead of being resolved into silence.

---

## 6. What the model is asked for

Plan views and technical drawings get a `labels` section:

```json
"labels": [
  {"id": "label_1", "text": "MASTER BEDROOM",
   "bbox": [0.21, 0.34, 0.33, 0.38], "room_type": "bedroom",
   "confidence": 0.92}
]
```

The prompt asks for text transcribed **exactly as printed** — `BR 2` is not to
be expanded to `BEDROOM 2`, because abbreviation matching happens on our side
where it can be checked, and an expansion the model made is a guess we cannot
audit. It asks for labels from *every* plan on a composite sheet, rather than
asking the model to pick one.

Interior photographs are told to leave `labels` empty, and the parser clears
the field for `full` mode regardless — text on a book spine locates nothing,
and offering it to the engine would propose correspondences that cannot be
real. The prompt is an optimisation; the parser is the guarantee.

The prompt text is part of the response cache key, so this change invalidates
cleanly rather than serving responses that predate it.

### Tiled sheets

`vision/tiling.py` splits large, dense images into overlapping tiles and
analyses each — which is triggered by exactly the sheets registration exists
for. The two compose, and the composition is load-bearing in both directions:

* **Tiling improves registration.** A room name six millimetres tall on an A1
  sheet is a handful of pixels in one downscaled pass. Tiled, it is legible,
  so more labels resolve and the fit gets more correspondences.
* **Registration is what makes tiling positional.** Tiling alone recovers
  appearance evidence only; it says what is on the sheet, not where the plan
  sits inside it. This module supplies the second half.

That only works because `merge_payloads` remaps label boxes into whole-image
coordinates along with objects, lights and openings. It is worth being
explicit about why: a label's only value is positional, so one left in
tile-local coordinates does not weaken the fit, it **corrupts** it — the
consensus would be handed a real room name at a confidently wrong position.
Dropping them silently is barely better, since it would make the two features
fail to compose in precisely the case both were built for.

A label cut by a seam is the residual gap. The model may read `MASTER` in one
tile and `BED` in the next; neither fragment matches anything, so the label is
lost rather than mismatched. That is a degradation — one fewer correspondence
— not an error.

---

## 7. Diagnostics

`graph.diagnostics["registration"]` carries a serialised result per plan view:
the method, the transform, every candidate correspondence with its residual
and inlier verdict, the sheet region, and both unmatched-label lists.

`RegistrationResult.explain()` gives the one-line form:

```
img0: label_consensus 84%, 6/9 labels matched, residual 0.11 m mean /
0.28 m worst, plan covers 18% of the sheet
```

A failure explains itself in the same place, which is the substantive
improvement over the old behaviour — the previous message named the symptom
(detections lost) and never the cause:

```
img0: not registered — none of the 4 label(s) read in the image match any of
the 7 label(s) in the drawing
```

---

## 8. Testing

```
pytest tests/test_registration.py
```

91 tests: the closed-form arithmetic (including that a fitted transform is
always a similarity, and that the legacy stretch is reproduced bit-for-bit as
the fallback), label matching and normalisation, consensus behaviour under
ambiguity and outliers, the ladder end to end, the pipeline path against the
committed `tests/fixtures/apartment.dxf`, tile-merge composition, and the
review surface's reporting.

Most build their anchors by hand, which is the point of the package being
stdlib-only and type-agnostic. Two tests are worth knowing about:

* `test_agrees_with_the_cad_normaliser` — the normalisation rules are
  duplicated from `cad.text` rather than imported, so that neither package
  depends on the other. This asserts they have not drifted, over a corpus of
  real label spellings. It has already caught one divergence.
* `test_the_legacy_stretch_is_corrected_where_it_was_wrong` — pins the
  behaviour that a registered plan does *not* inherit the fallback's
  anisotropic distortion.

---

## 9. Limitations

**Registration depends on legible labels.** No labels read, or none matching,
means the fallback — correctly marked, but still a guess. An unlabelled plan
cannot be registered by this method at all.

**Only room labels are used as anchors.** Grid bubbles, door tags and level
markers are also printed on both sides and are also positional. They are
harder to match reliably and are not yet used.

**Interior views register to a room, not to a pose.** Recovering a camera pose
against the CAD — structure-from-motion, or vanishing-point fitting against
known wall planes — remains open. See [`FIDELITY.md`](FIDELITY.md) §10:
viewpoints are still fitted from a horizon estimate and a field-of-view
bucket, which is why the layout axis compares a coarse mass grid rather than
attempting pixel registration.

**One transform per image.** A sheet showing the same floor at two scales (a
plan plus an enlarged detail) registers to whichever the labels favour. The
detail's contents are then reported as unmatched rather than read.

**Multi-floor sheets are detected, not separated.** The engine identifies that
other labels exist and names them, but does not fit a second transform for the
second floor — the DXF describes one floor, so there is nothing to register it
against.
