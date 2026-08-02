"""
ArchX3D — Vision prompts
========================
One prompt, one call per image, one complete observation document.

Design notes
------------
* **Controlled vocabulary is inlined.** Listing the accepted category and
  material terms in the prompt raises the rate at which
  ``catalog.normalise_*`` resolves a label cleanly, instead of relying on
  post-hoc synonym matching for everything.

* **No metric estimates are requested.** The model reports normalised image
  boxes and a coarse size bucket. Metres come from ``catalog`` priors combined
  with the room's true DXF dimensions. Asking a VLM for centimetres produces
  confident nonsense; asking it whether a sofa is large *for a sofa* does not.

* **Abstention is made cheap and explicit.** The brief is emphatic that
  omission beats invention, so the prompt states the rule several times, asks
  for calibrated confidence, and provides an ``occluded``/``partially_visible``
  escape hatch so the model is not pushed into guessing hidden geometry.

* **One call, not several.** Splitting detection / materials / lighting across
  calls would triple latency and cost for no accuracy gain, since they all
  depend on the same image evidence.
"""

from __future__ import annotations

from typing import List, Optional

from . import catalog

# ---------------------------------------------------------------------------
# Vocabulary blocks (rendered into the prompt)
# ---------------------------------------------------------------------------


def _vocab(items) -> str:
    return ", ".join(sorted(items))


OBJECT_VOCAB = _vocab(catalog.OBJECT_CATALOG.keys())
MATERIAL_VOCAB = _vocab(catalog.MATERIALS.keys())
LIGHT_VOCAB = _vocab(catalog.LIGHT_TYPES.keys())
CEILING_VOCAB = _vocab(catalog.CEILING_TYPES)
ROOM_VOCAB = _vocab(catalog.ROOM_TYPES)
PREDICATE_VOCAB = _vocab(catalog.ENFORCED_PREDICATES)


SYSTEM_ROLE = (
    "You are an architectural scene analyst. You examine photographs of "
    "interiors and produce precise, calibrated, machine-readable descriptions "
    "for a procedural 3D reconstruction system. You are rewarded for accuracy "
    "and for admitting uncertainty, and penalised for inventing detail."
)


# ---------------------------------------------------------------------------
# The observation prompt
# ---------------------------------------------------------------------------

OBSERVATION_PROMPT = f"""{SYSTEM_ROLE}

Analyse the supplied interior photograph and return ONE JSON object describing
everything you can actually see. This will drive an automatic 3D rebuild of the
room, so structural correctness matters more than richness.

════════════════════════════════════════════════════════════════════
ABSOLUTE RULES
════════════════════════════════════════════════════════════════════
1. NEVER invent an object, light, material or relationship that is not
   visible in this image. An omitted object costs far less than a wrong one.
2. If you cannot identify something with reasonable certainty, either leave it
   out entirely or include it with a low `confidence` and `"uncertain": true`.
   Do not upgrade a guess into a confident claim.
3. Report CALIBRATED confidence in [0,1]. Use 0.95+ only when the object is
   unmistakable and fully visible. Use 0.5-0.7 when it is partly occluded,
   small, blurry or ambiguous. Be honest: systematic overconfidence corrupts
   the reconstruction.
4. Do NOT estimate real-world measurements in metres. Report the size bucket
   instead. Metric dimensions are derived downstream from known room geometry.
5. Do not describe anything outside the room (views through windows, reflected
   scenes in mirrors, artwork contents).
6. Prefer the controlled vocabulary terms listed below. If nothing fits, use a
   short plain-English noun and expect it may be dropped.

════════════════════════════════════════════════════════════════════
CONTROLLED VOCABULARY
════════════════════════════════════════════════════════════════════
object category : {OBJECT_VOCAB}
material        : {MATERIAL_VOCAB}
light kind      : {LIGHT_VOCAB}
ceiling type    : {CEILING_VOCAB}
room type       : {ROOM_VOCAB}
relationship    : {PREDICATE_VOCAB}

size_bucket     : very_small | small | medium | large | very_large
                  (relative to a TYPICAL example of that same category —
                   a "large" sofa is large for a sofa, not large in the room)
support         : floor | wall | ceiling | on_object
mounting        : floor | wall | ceiling | table

════════════════════════════════════════════════════════════════════
COORDINATES
════════════════════════════════════════════════════════════════════
`bbox` is [x0, y0, x1, y1] normalised to [0,1], origin at the TOP-LEFT of the
image, x rightwards, y downwards. Be as tight as you can — these boxes are
back-projected onto the floor plan, so a sloppy bottom edge moves the object.

For floor-standing objects the BOTTOM edge of the box must sit where the
object meets the floor. If the contact point is hidden behind another object,
set `"base_occluded": true` and put the bottom edge at your best estimate.

════════════════════════════════════════════════════════════════════
OUTPUT SCHEMA
════════════════════════════════════════════════════════════════════
Return raw JSON only — no markdown fences, no commentary.

{{
  "image_class": {{
    "comment": "Classify this image FIRST, then fill only the sections your class allows (see ROUTING below).",
    "type": "interior_photograph | interior_render | room_render | furnished_floorplan | top_down_layout | cad_drawing | wireframe | architectural_elevation | exterior_render | site_plan",
    "medium": "photo | render | drawing",
    "room_type": "<the single room shown, or 'unknown' if it shows several>",
    "confidence": 0.0
  }},

  "room": {{
    "room_type": "<room type>",
    "style": "<modern | minimalist | industrial | luxury | scandinavian |
                contemporary | traditional | bohemian | japanese |
                mediterranean | classic | farmhouse | art_deco | mid_century>",
    "time_of_day": "day | evening | night | overcast",
    "confidence": 0.0,
    "notes": "<one short sentence>"
  }},

  "camera": {{
    "comment": "Your viewpoint. Used to back-project objects onto the plan.",
    "height_bucket": "low | eye_level | high",
    "horizon_y": 0.0,
    "field_of_view": "narrow | normal | wide | very_wide",
    "facing_wall": "<which wall you face: 'long' | 'short' | 'corner' | 'unknown'>",
    "confidence": 0.0
  }},

  "finishes": {{
    "wall":    {{ "material": "<material>", "color_hex": "#RRGGBB",
                  "finish": "matte|satin|gloss", "description": "",
                  "confidence": 0.0 }},
    "floor":   {{ "material": "<material>", "color_hex": "#RRGGBB",
                  "finish": "matte|satin|gloss", "description": "",
                  "confidence": 0.0 }},
    "ceiling": {{ "material": "<material>", "color_hex": "#RRGGBB",
                  "ceiling_type": "<ceiling type>", "description": "",
                  "confidence": 0.0 }}
  }},

  "openings": [
    {{
      "kind": "door | window | archway | niche",
      "bbox": [0,0,0,0],
      "on_wall": "left | back | right | front | unknown",
      "size_bucket": "medium",
      "sill_bucket": "floor | low | mid | high",
      "confidence": 0.0,
      "uncertain": false
    }}
  ],

  "architecture": [
    {{
      "kind": "column | beam | staircase | partition | false_ceiling | niche",
      "bbox": [0,0,0,0],
      "material": "<material>",
      "color_hex": "#RRGGBB",
      "size_bucket": "medium",
      "confidence": 0.0,
      "uncertain": false
    }}
  ],

  "lights": [
    {{
      "kind": "<light kind>",
      "bbox": [0,0,0,0],
      "mounting": "<mounting>",
      "count": 1,
      "color_temperature": "warm | neutral | cool",
      "brightness": "dim | moderate | bright",
      "is_on": true,
      "confidence": 0.0,
      "uncertain": false
    }}
  ],

  "objects": [
    {{
      "id": "<short unique slug, e.g. 'sofa_1'>",
      "category": "<object category>",
      "label": "<short natural description incl. colour and material>",
      "bbox": [0,0,0,0],
      "size_bucket": "medium",
      "support": "<support>",
      "support_target": "<id of the object it rests on, else ''>",
      "on_wall": "left | back | right | front | none | unknown",
      "facing": "toward_camera | away_from_camera | left | right | unknown",
      "material": "<material>",
      "color_hex": "#RRGGBB",
      "partially_visible": false,
      "base_occluded": false,
      "confidence": 0.0,
      "uncertain": false
    }}
  ],

  "relationships": [
    {{
      "subject": "<object id>",
      "predicate": "<relationship>",
      "object": "<object id>",
      "confidence": 0.0
    }}
  ],

  "labels": [
    {{
      "id": "<short unique slug, e.g. 'label_1'>",
      "text": "<the text EXACTLY as printed, e.g. 'MASTER BEDROOM'>",
      "bbox": [0,0,0,0],
      "room_type": "<room type it names, or 'unknown'>",
      "confidence": 0.0
    }}
  ]
}}

════════════════════════════════════════════════════════════════════
ROUTING — what to fill in, based on the class you assigned
════════════════════════════════════════════════════════════════════
• interior_photograph / interior_render / room_render
  Fill everything. This is a normal interior view.

• furnished_floorplan / top_down_layout (a plan seen from above)
  Fill `objects` and `relationships` — the layout is exactly what this view is
  good for. Boxes are in PLAN space here, so `bbox` maps to floor position
  directly; state `"facing"` from the direction furniture points on the page.
  Leave `lights` empty unless fixtures are genuinely drawn.
  Leave `finishes` empty unless surfaces are clearly coloured — a plan's fill
  colours are usually diagrammatic, not real materials.
  **Fill `labels`.** See below — on a plan this is the most useful thing you
  can report.

• cad_drawing / wireframe / architectural_elevation
  This is a technical drawing, NOT a picture of a real room.
  Fill ONLY `openings`, `architecture` and `labels`.
  Leave `objects`, `lights` and `finishes` COMPLETELY EMPTY.
  A CAD drawing has no wall colour, no floor material and no furniture — line
  weights and hatching are notation, not appearance. Reporting them would
  inject invented materials into the model.

• exterior_render / site_plan
  Return the classification and nothing else. Leave every other section empty.

════════════════════════════════════════════════════════════════════
GUIDANCE
════════════════════════════════════════════════════════════════════
• List EVERY distinct piece of furniture you can see, including partly
  occluded ones (mark `partially_visible`). Give each a unique `id`.
• Group repeated small items sensibly: four matching dining chairs are four
  entries (dining_chair_1..4), but a scatter of books can be one entry.
• `relationships` is important — the rebuild uses it to orient furniture.
  State the obvious ones you can SEE (sofa faces tv_unit, chairs surround the
  dining table, rug centered_under coffee table, lamp on_top_of side table).
  Do not state a relationship you cannot observe.
• For `color_hex`, give the object's dominant surface colour as it would look
  under neutral white light — discount strong colour casts from the lighting.
• Lights that are switched off are still fixtures: report them with
  `"is_on": false`.
• If the image shows more than one room (e.g. through an opening), describe
  ONLY the room the camera is in, and note the opening.

════════════════════════════════════════════════════════════════════
LABELS — plans and technical drawings only
════════════════════════════════════════════════════════════════════
On a plan, the printed room names are the single most valuable thing in the
image. We hold the same names with their true coordinates, so reporting where
each one sits on the page lets us work out exactly how this image lines up
with the building — which floor it is, where on the sheet it sits, and at what
scale. Without them we have to assume the plan fills the frame, and every
piece of furniture read from a sheet that holds anything else lands in the
wrong room or nowhere at all.

• Report EVERY room name printed on the plan: `MASTER BEDROOM`, `KITCHEN`,
  `W.C.`, `LIVING/DINING`, and so on.
• Transcribe `text` EXACTLY as printed — keep the punctuation and the
  abbreviation. Do not expand `BR 2` to `BEDROOM 2`; we match abbreviations
  ourselves, and an expansion we did not make is a guess we cannot check.
• `bbox` must tightly enclose the printed words themselves, not the room.
• If the sheet shows SEVERAL plans (two floors side by side, a key plan in a
  corner), report the labels from ALL of them. Do not try to pick one — say
  what is printed and where, and we will work out which plan is which.
• Skip dimensions, area figures, north arrows, title blocks, drawing numbers
  and scale bars. Room names only.
• On an interior photograph, leave `labels` empty. Text on a book spine or a
  cereal packet tells us nothing about where the camera is.
"""


def build_observation_prompt(hint: Optional[str] = None) -> str:
    """Return the extraction prompt, optionally with caller-supplied context.

    ``hint`` carries facts we already know for certain — chiefly the room's
    true dimensions from the DXF — so the model can calibrate relative sizes
    against real geometry instead of guessing the room's scale.
    """
    if not hint:
        return OBSERVATION_PROMPT
    return (
        OBSERVATION_PROMPT
        + "\n\n════════════════════════════════════════════════════════════════════\n"
        "KNOWN FACTS ABOUT THIS ROOM (from the CAD floor plan — trust these)\n"
        "════════════════════════════════════════════════════════════════════\n"
        + hint
        + "\nUse these to calibrate relative sizes. Do not contradict them.\n"
    )


#: Sent instead of the full prompt when the local classifier has already
#: established the image is line-art. Asking a model about the "wall colour" of
#: a CAD export invites it to answer, so the question is simply not put.
GEOMETRY_ONLY_PROMPT = f"""{SYSTEM_ROLE}

This image is a TECHNICAL DRAWING — a CAD export, wireframe, blueprint or
elevation. It depicts notation, not a photographed room.

Extract ONLY verifiable geometry. Do NOT report wall colours, floor materials,
furniture, decor or lighting: a drawing has none of those, and line weights,
hatching and fill patterns are notation rather than appearance.

Return raw JSON only:

{{
  "image_class": {{
    "type": "cad_drawing | wireframe | architectural_elevation",
    "medium": "drawing",
    "room_type": "unknown",
    "confidence": 0.0
  }},
  "openings": [
    {{
      "kind": "door | window | archway | niche",
      "bbox": [0,0,0,0],
      "on_wall": "left | back | right | front | unknown",
      "size_bucket": "very_small | small | medium | large | very_large",
      "sill_bucket": "floor | low | mid | high",
      "confidence": 0.0
    }}
  ],
  "architecture": [
    {{
      "kind": "column | beam | staircase | partition | false_ceiling | niche",
      "bbox": [0,0,0,0],
      "size_bucket": "medium",
      "confidence": 0.0
    }}
  ],
  "geometry_notes": {{
    "room_count_visible": 0,
    "has_dimensions_annotated": false,
    "comment": "<one sentence on what this drawing shows>"
  }},
  "objects": [],
  "lights": [],
  "relationships": []
}}

`bbox` is [x0, y0, x1, y1] normalised to [0,1] from the TOP-LEFT.
Report only what is drawn. If you cannot identify an opening confidently,
leave it out.
"""


def build_geometry_prompt(hint: Optional[str] = None) -> str:
    """Prompt for technical drawings: geometry verification only."""
    if not hint:
        return GEOMETRY_ONLY_PROMPT
    return (
        GEOMETRY_ONLY_PROMPT
        + "\n\nKNOWN FACTS FROM THE CAD FLOOR PLAN (trust these):\n"
        + hint
        + "\nUse them to sanity-check what you report.\n"
    )


def prompt_for_mode(mode: str, hint: Optional[str] = None) -> str:
    """Select the prompt matching an image's analysis mode."""
    if mode == "geometry":
        return build_geometry_prompt(hint)
    return build_observation_prompt(hint)


def build_room_hint(
    width_m: float,
    depth_m: float,
    ceiling_height_m: float,
    wall_count: int,
    room_type: Optional[str] = None,
) -> str:
    """Format the DXF-derived facts injected into the prompt."""
    lines: List[str] = [
        f"- Floor plan footprint: {width_m:.2f} m x {depth_m:.2f} m "
        f"({width_m * depth_m:.1f} m2).",
        f"- Ceiling height: {ceiling_height_m:.2f} m.",
        f"- The plan has {wall_count} wall segments.",
    ]
    if room_type:
        lines.append(f"- The floor plan labels this space as: {room_type}.")
    return "\n".join(lines)
