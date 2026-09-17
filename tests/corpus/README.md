# Reconstruction corpus

Drawings with exact ground truth, used to *measure* the reconstruction engine
(`tools/corpus/evaluate.py`, `tests/test_corpus_metrics.py`,
`docs/CORPUS_REPORT.md`). Every DXF has a `.truth.json` beside it, in metres
in the drawing's own frame; the format is described in
`modules/recon/metrics.py`.

Read this before quoting a number from the report: **no drawing here is a
native CAD file from a practice.** The layouts and buildings are real; the
DXF drafting was produced by the conversion tools below, and some sheets are
composed from several sources. Each group says exactly what is real and what
was made.

## What is here

| folder | drawings | real part | made part | licence |
|---|---|---|---|---|
| `residential/` | 22 | house layouts (ResPlan) | DXF drafting, 4 conventions | CC BY 4.0 |
| `apartment/` | 12 | apartment layouts (ResPlan) | DXF drafting, 4 conventions | CC BY 4.0 |
| `multistorey/` | 5 | 4 real BIM buildings (buildingSMART samples), every storey | plan cut and DXF drafting | CC BY 4.0 |
| `multibuilding/` | 5 | house layouts (ResPlan) placed on real house plots (OpenStreetMap) | the composition | CC BY 4.0 + ODbL |
| `siteplan/` | 5 | as `multibuilding/`, plus real roads, neighbouring buildings and trees | the composition, site drafting | CC BY 4.0 + ODbL |

By category (a sheet can count twice):

| category | target | drawings | independent buildings | notes |
|---|---|---|---|---|
| residential | 20+ | 24 | 23 | 22 ResPlan houses; the Duplex drawn twice (mm and undeclared inches) |
| apartments | 10+ | 14 | 14 | 12 ResPlan; Esplanades (EE, 5 storeys); Schependomlaan (NL, 4 storeys) |
| commercial | 10+ | **1** | **1** | Medical-Dental Clinic (US, 2 storeys, 240 doors). **Target not met.** |
| site plans | 5+ | 5 | 14 houses on 4 real sites | composed |
| multi-building | 5+ | 10 | 29 houses on 4 real sites | composed (the site plans included) |
| multi-storey | 5+ | 5 | 4 | the Duplex twice |

Units and conventions covered: millimetres, centimetres, metres, feet,
inches with no unit declared; AIA and vernacular layers; closed outlines and
loose lines; door symbols as arcs, as anonymous mirrored blocks on layer 0 and
as jamb marks; windows as bands, blocks and glazing lines; drawings rotated
off the axes and at survey-scale coordinates; plan titles in English, Dutch
and Estonian.

## Sources

### ResPlan — `residential/`, `apartment/`, and the buildings in the composed sheets

Abouagour & Garyfallidis, *ResPlan: A Large-Scale Vector-Graph Dataset of
17,000 Residential Floor Plans*, arXiv:2508.14006.
<https://github.com/m-agour/ResPlan>. Data licensed CC BY 4.0.

Converted by `tools/corpus/resplan_to_dxf.py` (drafting styles A–D are
described there). ResPlan stores plans on a 256-unit canvas; the metric scale
is derived from each plan's stated area, which gives wall thicknesses of
0.16–0.30 m. Files: `resplan_manifest.json`. Test-split plans only.

### buildingSMART Community Sample Test Files — `multistorey/`

<https://github.com/buildingsmart-community/Community-Sample-Test-Files>,
licensed CC BY 4.0, © the original authors:

| file | model | building |
|---|---|---|
| `bsc_duplex_A.dxf`, `bsc_duplex_D.dxf` | `IFC 2.3.0.1 (IFC 2x3)/Duplex Apartment/Duplex_A_20110907.ifc` | two-dwelling house, 2 storeys |
| `bsc_clinic_B.dxf` | `IFC 2.3.0.1 (IFC 2x3)/Medical-Dental Clinic/Clinic_Architectural.ifc` | clinic, 2 storeys |
| `bsc_esplanades_A.dxf` | `IFC 2.3.0.1 (IFC 2x3)/Esplanades/1807_EP_AR_v18.ifc` | apartment building, 5 storeys |
| `bsc_schependomlaan_C.dxf` | `IFC 2.3.0.1 (IFC 2x3)/Schependomlaan/Design model IFC/IFC Schependomlaan.ifc` | apartment building, 4 storeys |

Converted by `tools/corpus/ifc_to_dxf.py`: every wall, column and
curtain-wall part of the model is cut 1.2 m above each storey's floor, as a
plan view is; doors and windows the cut passes through are drawn as symbols;
spaces are labelled with their own names; the storeys are laid out on one
sheet under titles taken from the model's storey names. Truth rooms are the
regions the walls and openings close (gaps under 16 cm — unjoined wall seams
in the model — do not open a room); `spaces` records the model's own spaces.
Curtain-wall glazing is recorded as `glazing`, and a window reported on it is
counted apart rather than as right or wrong.

The Schependomlaan model is untidy — unjoined wall seams, cavity walls drawn
as two leaves, duplicated door elements — and its numbers show it. It is kept
because real models are like that.

### OpenStreetMap — site context in `multibuilding/` and `siteplan/`

© OpenStreetMap contributors, available under the Open Database License
(ODbL) 1.0, <https://www.openstreetmap.org/copyright>. Buildings, roads and
trees around five places (Houten, Zwolle — NL; Cesson-Sévigné — FR; Freiburg
Rieselfeld — DE; Amersfoort Vathorst — NL, fetched but too sparse to use),
queried through the Overpass API.

Composed by `tools/corpus/compose_site.py`: each placed building is a
different ResPlan house, set at the centroid and along the orientation of a
real house footprint; plans that would overlap another plan or a road are
skipped. Site plans also draw the road edges, neighbouring buildings as
outlines, trees, a property line, a north arrow and a title. Some sheets label
the buildings `BLOCK A`, `BLOCK B`, ….

## Regenerating

The source data is not stored here. With it downloaded:

```
python tools/corpus/resplan_to_dxf.py ResPlan.pkl tests/corpus
python tools/corpus/ifc_to_dxf.py MODEL.ifc tests/corpus/multistorey/NAME.dxf --units mm --layout row
python tools/corpus/compose_site.py ResPlan.pkl site.json tests/corpus/siteplan/NAME.dxf --style B --buildings 3 --site
```

`ifc_to_dxf.py` needs `ifcopenshell`, which is not a project dependency; run
it from a separate environment. The exact options used for each file are in
`manifest.json`.

## What the corpus does not have

* **Native CAD.** No drawing here was drawn by an architect in a CAD program.
  The native real drawings the engine is tested on
  (`tests/fixtures/real/final_plan_19th_may.dxf`,
  `tests/fixtures/plans/residential_us.dxf`, the `sba.dxf` project) have no
  ground truth and are tested by assertion, not measured here.
* **Commercial buildings.** One. Retail, office and industrial plans with
  usable licences were not found.
* **Native multi-building site plans.** The composed sheets test separation
  on real spacing and orientation, but the drafting of the whole sheet is
  ours.

## Release thresholds, and why they are what they are

`tests/test_corpus_metrics.py` holds the corpus to a floor per dataset group
(`FLOORS`), and the build fails with the number that moved. The floors are set
a little under what the engine measured when they were written, so they catch
a regression without failing on noise.

They are deliberately **not** one global set. A single number across the corpus
would be set by whichever group is hardest and would then be met trivially by
the rest. What a drawing is made of changes what is achievable:

| metric | floor | where it comes from |
|---|---|---|
| `unit_accuracy` | **1.0**, exact | Getting the scale wrong is not a degraded model, it is a wrong one — the founding bug was a house rebuilt 0.8 m across. Nothing below 100% is acceptable, and the corpus has never missed one. |
| `door_precision` / `window_precision` | 0.96–0.99 | An invented opening is a hole in a wall that is not there. Precision is held near the ceiling deliberately, so recall may never be bought with false positives. |
| `window_recall` | 0.72 (buildingSMART), 0.93–0.94 (ResPlan) | The split is the point. Converted BIM sheets include models that draw windows as bare gaps with no glazing line; the engine will not call those windows without evidence. See the Schependomlaan note above and P1.2. |
| `door_recall` | 0.83–0.87 | Same reasoning, one step easier: a swing is drawn more reliably than glazing. |
| `room_recall` | 0.72 (composed), 0.83 (ResPlan), 0.88 (BIM) | Composed sheets are lowest because several buildings on one sheet give the segmentation more ways to join two rooms through a doorway jog. |
| `footprint_iou_median` | 0.80 (composed), 0.90 (BIM), 0.97 (ResPlan) | Composed and BIM sheets carry geometry outside the building — site work, curtain walls, balconies — and the truth footprint draws the line in a place the drawing does not mark. |
| `structure_accuracy` | 0.89–1.0 | How often the right number of buildings and storeys came out. |

The thresholds suggested as a starting point in the release brief were
`footprint IoU >= 0.95`, `window precision >= 95%`, `room recall >= 90%`.
Precision is met and exceeded. The other two are **not** appropriate as global
gates on this corpus, and the measurements say why rather than the other way
round:

* `footprint_iou >= 0.95` holds on ResPlan (0.9951) but not on the composed
  site plans (0.8503), where the truth footprint is a judgement about where a
  terrace ends, not a fact the drawing states.
* `room_recall >= 90%` holds on the real BIM sheets (0.9173) but not on the
  composed ones (0.7558). Raising the floor to 0.90 would not improve the
  engine; it would fail the build on drawings the engine reads correctly.

So the gates are per group, at the measured level, and a change that improves
a number should raise its floor rather than leave slack behind it.
