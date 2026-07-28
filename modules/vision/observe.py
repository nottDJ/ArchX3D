"""
ArchX3D — Per-image observation parsing
=======================================
Turns one raw VLM payload into typed, normalised records.

This layer is deliberately paranoid. Model output is *usually* well-formed but
occasionally contains nulls in numeric slots, categories outside the
vocabulary, boxes with swapped corners, ids referenced before definition, or a
whole section missing. None of that should abort a run: a malformed entry is
dropped and counted, and the rest of the image still contributes.

Nothing here invents data. If a field is absent, it stays absent and the
downstream defaulting is explicit and recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import catalog
from .schema import BBox2D, ConfidencePolicy, Finish, _f, normalise_hex

#: Wall-facing labels the model may use, in the order they map onto the
#: camera's view: left/back/right/front are relative to the viewpoint.
WALL_SIDES = ("left", "back", "right", "front")

#: Coarse colour-temperature words → kelvin.
CCT_WORDS = {"warm": 2700.0, "neutral": 3500.0, "cool": 5000.0, "daylight": 6000.0}

#: Brightness words → multiplier applied to the fixture's nominal power.
BRIGHTNESS_WORDS = {"dim": 0.55, "moderate": 1.0, "bright": 1.6, "very_bright": 2.1}

#: Sill descriptions → height above floor in metres.
SILL_WORDS = {"floor": 0.0, "low": 0.35, "mid": 0.85, "high": 1.45}


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------


@dataclass
class ObjectObservation:
    """One object as seen in one image."""

    local_id: str
    category: str
    label: str
    group: str
    bbox: Optional[BBox2D]
    size_bucket: str
    support: str
    support_target: str
    on_wall: str
    facing: str
    material: str
    color_hex: str
    confidence: float
    partially_visible: bool = False
    base_occluded: bool = False
    uncertain: bool = False
    image_id: str = ""


@dataclass
class LightObservation:
    local_id: str
    kind: str
    bbox: Optional[BBox2D]
    mounting: str
    count: int
    cct_k: float
    brightness: float
    is_on: bool
    confidence: float
    uncertain: bool = False
    image_id: str = ""


@dataclass
class OpeningObservation:
    local_id: str
    kind: str
    bbox: Optional[BBox2D]
    on_wall: str
    size_bucket: str
    sill_height: float
    confidence: float
    uncertain: bool = False
    image_id: str = ""


@dataclass
class ArchObservation:
    local_id: str
    kind: str
    bbox: Optional[BBox2D]
    material: str
    color_hex: str
    size_bucket: str
    confidence: float
    uncertain: bool = False
    image_id: str = ""


@dataclass
class RelationObservation:
    subject: str
    predicate: str
    object: str
    confidence: float
    image_id: str = ""


@dataclass
class CameraObservation:
    height_bucket: str = "eye_level"
    horizon_y: float = 0.5
    field_of_view: str = "normal"
    facing_wall: str = "unknown"
    confidence: float = 0.0

    @property
    def eye_height_m(self) -> float:
        """Approximate camera height above the floor, metres."""
        return {"low": 1.05, "eye_level": 1.55, "high": 2.10}.get(self.height_bucket, 1.55)

    @property
    def vertical_fov_deg(self) -> float:
        return {
            "narrow": 32.0,
            "normal": 45.0,
            "wide": 62.0,
            "very_wide": 78.0,
        }.get(self.field_of_view, 45.0)


@dataclass
class ImageObservation:
    """Everything one reference image contributed."""

    image_id: str
    image_path: str
    room_type: str = "unknown"
    room_type_confidence: float = 0.0
    style: str = "unknown"
    #: full | layout | geometry | skip — set from the image's classification.
    analysis_mode: str = "full"
    #: How far this image may influence metric geometry, in [0, 1].
    geometry_trust: float = 0.5
    camera: CameraObservation = field(default_factory=CameraObservation)
    wall_finish: Finish = field(default_factory=Finish)
    floor_finish: Finish = field(default_factory=Finish)
    ceiling_finish: Finish = field(default_factory=Finish)
    ceiling_type: str = "plain"
    #: day | evening | night | overcast, when the model could tell. The one
    #: lighting judgement arithmetic on the fixture list cannot recover: no
    #: amount of counting lamps reveals whether it is dark outside.
    time_of_day: str = ""
    objects: List[ObjectObservation] = field(default_factory=list)
    lights: List[LightObservation] = field(default_factory=list)
    openings: List[OpeningObservation] = field(default_factory=list)
    architecture: List[ArchObservation] = field(default_factory=list)
    relationships: List[RelationObservation] = field(default_factory=list)
    #: Counts of what was thrown away and why.
    rejected: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def _reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_observation(
    payload: Dict[str, Any],
    image_id: str,
    image_path: str,
    analysis_mode: str = "full",
) -> ImageObservation:
    """Normalise one raw VLM payload. Never raises on malformed input.

    ``analysis_mode`` comes from `classify.ImageProfile` and is enforced here
    as well as requested in the prompt — see `enforce_analysis_mode`.
    """
    obs = ImageObservation(image_id=image_id, image_path=image_path)
    obs.analysis_mode = analysis_mode

    if not isinstance(payload, dict):
        obs.notes.append("payload was not a JSON object")
        return obs

    _parse_room(payload.get("room"), obs)
    _parse_camera(payload.get("camera"), obs)
    _parse_finishes(payload.get("finishes"), obs)
    _parse_objects(payload.get("objects"), obs)
    _parse_lights(payload.get("lights"), obs)
    _parse_openings(payload.get("openings"), obs)
    _parse_architecture(payload.get("architecture"), obs)
    _parse_relationships(payload.get("relationships"), obs)

    enforce_analysis_mode(obs, analysis_mode)
    return obs


def enforce_analysis_mode(obs: ImageObservation, mode: str) -> None:
    """Discard anything the image's class does not entitle it to contribute.

    The prompt already tells the model not to report wall colours for a CAD
    export. Models do not always comply, and a single invented "light grey
    wall, confidence 0.9" from a blueprint would propagate into the fused
    finish for the whole building. So the rule is enforced on our side too:
    the prompt is an optimisation, this is the guarantee.
    """
    if mode == "full":
        return

    if mode == "skip":
        dropped = len(obs.objects) + len(obs.lights) + len(obs.openings) + len(obs.architecture)
        obs.objects.clear()
        obs.lights.clear()
        obs.openings.clear()
        obs.architecture.clear()
        obs.relationships.clear()
        obs.wall_finish = Finish()
        obs.floor_finish = Finish()
        obs.ceiling_finish = Finish()
        if dropped:
            obs._reject("dropped_by_skip_mode")
            obs.notes.append(f"exterior/site image: discarded {dropped} detections")
        return

    if mode == "geometry":
        # Technical drawings supply openings and structure, nothing else.
        dropped = len(obs.objects) + len(obs.lights)
        obs.objects.clear()
        obs.lights.clear()
        obs.relationships.clear()
        obs.wall_finish = Finish()
        obs.floor_finish = Finish()
        obs.ceiling_finish = Finish()
        obs.ceiling_type = "plain"
        if dropped:
            obs._reject("appearance_from_technical_drawing")
            obs.notes.append(
                f"technical drawing: discarded {dropped} appearance detections "
                "(a drawing has no materials, furniture or lighting)"
            )
        return

    if mode == "layout":
        # A plan view shows layout truthfully; its fill colours and any
        # "lighting" are diagrammatic, so only low-confidence finishes survive.
        dropped = len(obs.lights)
        obs.lights.clear()
        for finish_name in ("wall_finish", "floor_finish", "ceiling_finish"):
            finish = getattr(obs, finish_name)
            if finish.confidence > 0:
                # Halved, not zeroed: a plan does carry some palette signal.
                finish.confidence = round(finish.confidence * 0.5, 4)
        if dropped:
            obs.notes.append(f"plan view: discarded {dropped} lighting detections")


def _parse_room(raw: Any, obs: ImageObservation) -> None:
    if not isinstance(raw, dict):
        return
    room_type = str(raw.get("room_type", "unknown")).strip().lower().replace(" ", "_")
    obs.room_type = room_type if room_type in catalog.ROOM_TYPES else "unknown"
    obs.room_type_confidence = _clamp01(_f(raw.get("confidence")))
    # Kept as the model's own words; `appearance.resolve_style` normalises it
    # against the style vocabulary and scores how much to trust it.
    obs.style = str(raw.get("style", "unknown")).strip().lower() or "unknown"

    time_of_day = str(raw.get("time_of_day", "")).strip().lower()
    obs.time_of_day = (
        time_of_day if time_of_day in ("day", "evening", "night", "overcast") else ""
    )


def _parse_camera(raw: Any, obs: ImageObservation) -> None:
    if not isinstance(raw, dict):
        return
    horizon = _f(raw.get("horizon_y"), 0.5)
    obs.camera = CameraObservation(
        height_bucket=str(raw.get("height_bucket", "eye_level")).strip().lower(),
        # A horizon outside the frame is possible but a value outside [0,1]
        # is meaningless, so fall back rather than propagate nonsense.
        horizon_y=horizon if 0.0 <= horizon <= 1.0 else 0.5,
        field_of_view=str(raw.get("field_of_view", "normal")).strip().lower(),
        facing_wall=str(raw.get("facing_wall", "unknown")).strip().lower(),
        confidence=_clamp01(_f(raw.get("confidence"))),
    )


def _parse_finishes(raw: Any, obs: ImageObservation) -> None:
    if not isinstance(raw, dict):
        return

    obs.wall_finish = _finish_from(raw.get("wall"), "wall", "paint_matte")
    obs.floor_finish = _finish_from(raw.get("floor"), "floor", "wood")
    obs.ceiling_finish = _finish_from(raw.get("ceiling"), "ceiling", "paint_matte")

    ceiling_raw = raw.get("ceiling") if isinstance(raw.get("ceiling"), dict) else {}
    declared = str(ceiling_raw.get("ceiling_type", "plain")).strip().lower()
    obs.ceiling_type = declared if declared in catalog.CEILING_TYPES else "plain"


def _finish_from(raw: Any, surface: str, default_material: str) -> Finish:
    """Build a `Finish`, letting the catalog supply shading defaults.

    The model's hex colour is trusted (it reads colour well); roughness and
    metallic come from the material prior, since those are physical properties
    a VLM cannot observe reliably.
    """
    if not isinstance(raw, dict):
        prior = catalog.get_material(default_material)
        return Finish(
            material=default_material,
            color_hex=prior.color_hex,
            roughness=prior.roughness,
            metallic=prior.metallic,
            confidence=0.0,
        )

    material = catalog.normalise_material(str(raw.get("material", "")))
    if material == "unknown":
        material = default_material

    prior = catalog.get_material(material)
    # Reject a material the model assigned to a surface it cannot apply to
    # (e.g. "carpet" on a ceiling) rather than shading it wrongly.
    if surface not in prior.applies_to:
        material = default_material
        prior = catalog.get_material(material)

    roughness = prior.roughness
    finish_word = str(raw.get("finish", "")).strip().lower()
    if finish_word == "gloss":
        roughness = max(0.05, roughness * 0.30)
    elif finish_word == "satin":
        roughness = max(0.10, roughness * 0.65)

    return Finish(
        material=material,
        color_hex=normalise_hex(raw.get("color_hex"), prior.color_hex),
        roughness=roughness,
        metallic=prior.metallic,
        finish=finish_word or "matte",
        description=str(raw.get("description", ""))[:200],
        confidence=_clamp01(_f(raw.get("confidence"))),
    )


def _parse_objects(raw: Any, obs: ImageObservation) -> None:
    if not isinstance(raw, list):
        return

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            obs._reject("object_not_object")
            continue

        raw_category = str(item.get("category", "")) or str(item.get("label", ""))
        category = catalog.normalise_category(raw_category)
        if category is None:
            # Try the free-text label before giving up — the model sometimes
            # puts the recognisable noun there and something vague in category.
            category = catalog.normalise_category(str(item.get("label", "")))
        if category is None:
            obs._reject("unrecognised_category")
            continue

        confidence = _clamp01(_f(item.get("confidence")))
        band = ConfidencePolicy.classify(confidence)
        if band == "discard":
            obs._reject("below_confidence_floor")
            continue

        prior = catalog.get_prior(category)
        bbox = _bbox_from(item.get("bbox"))
        if bbox is None or bbox.area <= 1e-6:
            # Without a box the object cannot be grounded to a position.
            obs._reject("missing_or_empty_bbox")
            continue

        material = catalog.normalise_material(str(item.get("material", "")))
        support = str(item.get("support", "")).strip().lower()
        if support not in ("floor", "wall", "ceiling", "on_object"):
            support = prior.support if prior else "floor"

        obs.objects.append(
            ObjectObservation(
                local_id=_slug(item.get("id"), f"{category}_{index}"),
                category=category,
                label=str(item.get("label", ""))[:160],
                group=prior.group if prior else "furniture",
                bbox=bbox,
                size_bucket=_size_bucket(item.get("size_bucket")),
                support=support,
                support_target=_slug(item.get("support_target"), ""),
                on_wall=str(item.get("on_wall", "unknown")).strip().lower(),
                facing=str(item.get("facing", "unknown")).strip().lower(),
                material=material,
                color_hex=normalise_hex(item.get("color_hex")),
                confidence=confidence,
                partially_visible=bool(item.get("partially_visible", False)),
                base_occluded=bool(item.get("base_occluded", False)),
                uncertain=band == "uncertain" or bool(item.get("uncertain", False)),
                image_id=obs.image_id,
            )
        )


def _parse_lights(raw: Any, obs: ImageObservation) -> None:
    if not isinstance(raw, list):
        return

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            obs._reject("light_not_object")
            continue

        kind = catalog.normalise_light_kind(str(item.get("kind", "")))
        if kind is None:
            obs._reject("unrecognised_light_kind")
            continue

        confidence = _clamp01(_f(item.get("confidence")))
        if ConfidencePolicy.classify(confidence) == "discard":
            obs._reject("light_below_confidence_floor")
            continue

        prior = catalog.get_light_prior(kind)
        cct_word = str(item.get("color_temperature", "")).strip().lower()
        brightness_word = str(item.get("brightness", "")).strip().lower()

        # A fixture may legitimately appear many times (recessed downlights);
        # clamp so one bad number cannot spawn hundreds of lamps.
        count = int(_f(item.get("count"), 1.0)) or 1
        count = max(1, min(count, 24))

        obs.lights.append(
            LightObservation(
                local_id=_slug(item.get("id"), f"{kind}_{index}"),
                kind=kind,
                bbox=_bbox_from(item.get("bbox")),
                mounting=str(item.get("mounting", prior.mounting)).strip().lower()
                or prior.mounting,
                count=count,
                cct_k=CCT_WORDS.get(cct_word, prior.cct_k),
                brightness=BRIGHTNESS_WORDS.get(brightness_word, 1.0),
                is_on=bool(item.get("is_on", True)),
                confidence=confidence,
                uncertain=ConfidencePolicy.classify(confidence) == "uncertain"
                or bool(item.get("uncertain", False)),
                image_id=obs.image_id,
            )
        )


def _parse_openings(raw: Any, obs: ImageObservation) -> None:
    if not isinstance(raw, list):
        return

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            obs._reject("opening_not_object")
            continue

        kind = str(item.get("kind", "")).strip().lower()
        if kind not in ("door", "window", "archway", "niche"):
            obs._reject("unrecognised_opening_kind")
            continue

        confidence = _clamp01(_f(item.get("confidence")))
        if ConfidencePolicy.classify(confidence) == "discard":
            obs._reject("opening_below_confidence_floor")
            continue

        sill_word = str(item.get("sill_bucket", "")).strip().lower()
        default_sill = 0.0 if kind in ("door", "archway") else 0.85

        obs.openings.append(
            OpeningObservation(
                local_id=_slug(item.get("id"), f"{kind}_{index}"),
                kind=kind,
                bbox=_bbox_from(item.get("bbox")),
                on_wall=str(item.get("on_wall", "unknown")).strip().lower(),
                size_bucket=_size_bucket(item.get("size_bucket")),
                sill_height=SILL_WORDS.get(sill_word, default_sill),
                confidence=confidence,
                uncertain=ConfidencePolicy.classify(confidence) == "uncertain",
                image_id=obs.image_id,
            )
        )


def _parse_architecture(raw: Any, obs: ImageObservation) -> None:
    if not isinstance(raw, list):
        return

    valid = ("column", "beam", "staircase", "partition", "false_ceiling", "niche")
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            obs._reject("arch_not_object")
            continue

        kind = str(item.get("kind", "")).strip().lower().replace(" ", "_")
        if kind not in valid:
            obs._reject("unrecognised_arch_kind")
            continue

        confidence = _clamp01(_f(item.get("confidence")))
        if ConfidencePolicy.classify(confidence) == "discard":
            obs._reject("arch_below_confidence_floor")
            continue

        material = catalog.normalise_material(str(item.get("material", "")))
        obs.architecture.append(
            ArchObservation(
                local_id=_slug(item.get("id"), f"{kind}_{index}"),
                kind=kind,
                bbox=_bbox_from(item.get("bbox")),
                material=material,
                color_hex=normalise_hex(
                    item.get("color_hex"), catalog.get_material(material).color_hex
                ),
                size_bucket=_size_bucket(item.get("size_bucket")),
                confidence=confidence,
                uncertain=ConfidencePolicy.classify(confidence) == "uncertain",
                image_id=obs.image_id,
            )
        )


def _parse_relationships(raw: Any, obs: ImageObservation) -> None:
    if not isinstance(raw, list):
        return

    known_ids = {o.local_id for o in obs.objects}
    for item in raw:
        if not isinstance(item, dict):
            obs._reject("relationship_not_object")
            continue

        predicate = str(item.get("predicate", "")).strip().lower().replace(" ", "_")
        if predicate not in catalog.ENFORCED_PREDICATES:
            obs._reject("unenforceable_predicate")
            continue

        subject = _slug(item.get("subject"), "")
        target = _slug(item.get("object"), "")
        # Drop relationships that reference objects we discarded, otherwise the
        # solver would chase ids that no longer exist.
        if subject not in known_ids or (target not in known_ids and predicate != "mounted_on"):
            obs._reject("relationship_dangling_reference")
            continue

        obs.relationships.append(
            RelationObservation(
                subject=subject,
                predicate=predicate,
                object=target,
                confidence=_clamp01(_f(item.get("confidence"), 0.6)),
                image_id=obs.image_id,
            )
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(value: Any, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    text = _SLUG_RE.sub("_", value.strip().lower()).strip("_")
    return text or default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _size_bucket(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    return text if text in catalog.SIZE_BUCKETS else catalog.DEFAULT_SIZE_BUCKET


def _bbox_from(raw: Any) -> Optional[BBox2D]:
    """Accept both ``[x0,y0,x1,y1]`` and ``{"x0":..}`` box spellings."""
    if isinstance(raw, dict):
        return BBox2D.from_dict(raw)
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        return BBox2D.from_dict(
            {"x0": _f(raw[0]), "y0": _f(raw[1]), "x1": _f(raw[2]), "y1": _f(raw[3])}
        )
    return None
