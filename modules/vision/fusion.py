"""
ArchX3D — Multi-image fusion
============================
Consolidates observations from several photographs of the same room into one
consistent representation.

Why fusion is semantic rather than geometric
--------------------------------------------
The obvious approach — back-project each image separately, then merge by 3D
proximity — requires each image's camera pose in a shared room frame. Recovering
that from uncalibrated interior photos is a full SfM problem, and interiors are
exactly where SfM struggles (textureless walls, repeated furniture, few
overlapping features). Attempting it and getting it slightly wrong produces
*duplicated* furniture, which is far more visually damaging than a slightly
misplaced single instance.

So fusion works on **semantic identity** instead: the same physical sofa
observed from two angles is one `sofa` entity whose attributes are merged.
Object counts use the **maximum** seen in any single image rather than the sum,
because each viewpoint sees a subset of the same furniture — summing is what
produces phantom duplicates.

Confidence rises with independent corroboration (damped noisy-OR), so an object
two images agree on outranks one only a single image saw.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import catalog
from .observe import (
    ArchObservation,
    ImageObservation,
    LightObservation,
    ObjectObservation,
    OpeningObservation,
    RelationObservation,
)
from .schema import BBox2D, Finish

#: Two observations of the same category are treated as the same physical
#: object above this similarity. Tuned to be permissive — a false merge costs
#: one lost object, a false split costs a duplicate in the render.
MERGE_THRESHOLD = 0.45

#: Each corroborating observation closes this fraction of the remaining gap
#: to certainty. Deliberately damped: three mediocre looks at an object should
#: not manufacture near-certainty.
CORROBORATION_GAIN = 0.5


# ---------------------------------------------------------------------------
# Fused records
# ---------------------------------------------------------------------------


@dataclass
class FusedObject:
    """One physical object, assembled from one or more observations."""

    category: str
    label: str
    group: str
    size_bucket: str
    support: str
    support_category: str
    on_wall: str
    facing: str
    material: str
    color_hex: str
    confidence: float
    uncertain: bool
    partially_visible: bool
    base_occluded: bool
    #: The single best view of this object, used for metric grounding.
    primary_bbox: Optional[BBox2D]
    primary_image: str
    source_images: List[str] = field(default_factory=list)
    observation_count: int = 1
    #: Assigned once the whole set is ordered; stable across runs.
    instance_index: int = 0

    @property
    def entity_id(self) -> str:
        return f"{self.category}_{self.instance_index + 1}"


@dataclass
class FusedLight:
    kind: str
    mounting: str
    count: int
    cct_k: float
    brightness: float
    is_on: bool
    confidence: float
    uncertain: bool
    primary_bbox: Optional[BBox2D]
    source_images: List[str] = field(default_factory=list)
    instance_index: int = 0

    @property
    def entity_id(self) -> str:
        return f"{self.kind}_{self.instance_index + 1}"


@dataclass
class FusionResult:
    objects: List[FusedObject]
    lights: List[FusedLight]
    openings: List[OpeningObservation]
    architecture: List[ArchObservation]
    relationships: List[RelationObservation]
    room_type: str
    room_type_confidence: float
    style: str
    wall_finish: Finish
    floor_finish: Finish
    ceiling_finish: Finish
    ceiling_type: str
    #: Scene-level lighting the model reported, when it did. Empty when it
    #: stayed silent, in which case the environment is derived from geometry
    #: instead — see ``appearance.derive_lighting``.
    lighting_environment: Dict[str, object] = field(default_factory=dict)
    #: Maps each image's local object ids onto fused entity ids, so
    #: relationships survive the merge.
    id_map: Dict[Tuple[str, str], str] = field(default_factory=dict)
    stats: Dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def fuse(observations: Sequence[ImageObservation]) -> FusionResult:
    """Merge per-image observations into one room representation."""
    usable = [o for o in observations if o.objects or o.lights or o.openings]

    if not usable:
        return FusionResult(
            objects=[], lights=[], openings=[], architecture=[], relationships=[],
            room_type="unknown", room_type_confidence=0.0, style="unknown",
            wall_finish=Finish(), floor_finish=Finish(), ceiling_finish=Finish(),
            ceiling_type="plain",
            stats={"images_used": 0, "reason": "no usable observations"},
        )

    objects, id_map = _fuse_objects(usable)
    lights = _fuse_lights(usable)
    openings = _fuse_openings(usable)
    architecture = _dedupe_architecture(usable)
    relationships = _fuse_relationships(usable, id_map)

    room_type, room_conf = _resolve_room_type(usable, objects)
    style = _vote([(o.style, o.room_type_confidence or 0.5) for o in usable], "unknown")

    return FusionResult(
        objects=objects,
        lights=lights,
        openings=openings,
        architecture=architecture,
        relationships=relationships,
        room_type=room_type,
        room_type_confidence=room_conf,
        style=style,
        wall_finish=_merge_finishes([o.wall_finish for o in usable]),
        floor_finish=_merge_finishes([o.floor_finish for o in usable]),
        ceiling_finish=_merge_finishes([o.ceiling_finish for o in usable]),
        ceiling_type=_vote([(o.ceiling_type, 1.0) for o in usable], "plain"),
        lighting_environment=_merge_lighting_environment(usable),
        id_map=id_map,
        stats={
            "images_used": len(usable),
            "objects_before_fusion": sum(len(o.objects) for o in usable),
            "objects_after_fusion": len(objects),
            "lights_after_fusion": len(lights),
            "multi_image_objects": sum(1 for o in objects if o.observation_count > 1),
        },
    )


def _merge_lighting_environment(observations: Sequence[ImageObservation]) -> Dict[str, object]:
    """Reconcile scene-level lighting reported across several photographs.

    Only the qualitative judgements a model can actually make from a picture
    are taken: whether it is day or night, and how diffuse the light is. The
    numbers — ambient level, colour temperature, window share — are computed
    from the room's own geometry, which is evidence rather than impression.

    Photographs of the same room at different times would conflict; the most
    common answer wins, which is the same rule used for every other vote here.
    """
    merged: Dict[str, object] = {}

    times = [
        (str(getattr(o, "time_of_day", "") or ""), 1.0)
        for o in observations
        if getattr(o, "time_of_day", "")
    ]
    if times:
        merged["time_of_day"] = _vote(times, "day")

    return merged


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------


def _fuse_objects(
    observations: Sequence[ImageObservation],
) -> Tuple[List[FusedObject], Dict[Tuple[str, str], str]]:
    """Group same-category observations into physical instances.

    Within a category, each image proposes some number of instances. The
    surviving instance count is the maximum any single image reported, and
    observations are matched across images by attribute similarity.
    """
    by_category: Dict[str, Dict[str, List[ObjectObservation]]] = {}
    for image_obs in observations:
        for obj in image_obs.objects:
            by_category.setdefault(obj.category, {}).setdefault(image_obs.image_id, []).append(obj)

    fused: List[FusedObject] = []
    id_map: Dict[Tuple[str, str], str] = {}

    for category in sorted(by_category):
        per_image = by_category[category]

        # Seed clusters from the image that saw the most of this category —
        # that view has the best claim on the true instance count.
        seed_image = max(per_image, key=lambda img: (len(per_image[img]), img))
        seeds = sorted(per_image[seed_image], key=lambda o: -(o.bbox.area if o.bbox else 0.0))
        clusters: List[List[ObjectObservation]] = [[obs] for obs in seeds]

        for image_id in sorted(per_image):
            if image_id == seed_image:
                continue
            _assign_to_clusters(per_image[image_id], clusters)

        for cluster in clusters:
            entity = _merge_cluster(category, cluster)
            entity.instance_index = sum(1 for f in fused if f.category == category)
            fused.append(entity)
            for obs in cluster:
                id_map[(obs.image_id, obs.local_id)] = entity.entity_id

    # Largest, most confident furniture first: downstream placement resolves
    # anchors before dependants, and rendering order stays deterministic.
    fused.sort(key=lambda f: (_group_rank(f.group), -f.confidence, f.category))
    return fused, id_map


def _assign_to_clusters(
    candidates: Sequence[ObjectObservation], clusters: List[List[ObjectObservation]]
) -> None:
    """Greedily attach each candidate to its best-matching existing cluster.

    A candidate that matches nothing starts its own cluster — that is how an
    object only visible in a later image still gets represented.
    """
    taken: set = set()

    for candidate in sorted(candidates, key=lambda o: -(o.bbox.area if o.bbox else 0.0)):
        best_index, best_score = -1, 0.0
        for index, cluster in enumerate(clusters):
            if index in taken:
                continue
            score = max(_similarity(candidate, member) for member in cluster)
            if score > best_score:
                best_index, best_score = index, score

        if best_index >= 0 and best_score >= MERGE_THRESHOLD:
            clusters[best_index].append(candidate)
            taken.add(best_index)
        else:
            clusters.append([candidate])
            taken.add(len(clusters) - 1)


def _similarity(a: ObjectObservation, b: ObjectObservation) -> float:
    """Attribute-space similarity in ``[0, 1]`` between two observations.

    Image-space boxes are *not* compared: the two views may be from opposite
    ends of the room, so box overlap carries no signal here.
    """
    if a.category != b.category:
        return 0.0

    score, weight = 0.0, 0.0

    # Colour agreement is the strongest single cue for "same object".
    score += 0.40 * _color_similarity(a.color_hex, b.color_hex)
    weight += 0.40

    score += 0.20 * (1.0 if a.material == b.material else 0.0)
    weight += 0.20

    buckets = list(catalog.SIZE_BUCKETS)
    gap = abs(buckets.index(a.size_bucket) - buckets.index(b.size_bucket))
    score += 0.20 * max(0.0, 1.0 - gap / 2.0)
    weight += 0.20

    score += 0.20 * _label_similarity(a.label, b.label)
    weight += 0.20

    return score / weight if weight else 0.0


def _color_similarity(hex_a: str, hex_b: str) -> float:
    ra, ga, ba = _hex_to_rgb(hex_a)
    rb, gb, bb = _hex_to_rgb(hex_b)
    distance = math.dist((ra, ga, ba), (rb, gb, bb))
    # sqrt(3) is the maximum possible distance in the unit RGB cube.
    return max(0.0, 1.0 - distance / math.sqrt(3.0))


def _label_similarity(a: str, b: str) -> float:
    """Jaccard overlap of label word sets."""
    tokens_a = {t for t in a.lower().split() if len(t) > 2}
    tokens_b = {t for t in b.lower().split() if len(t) > 2}
    if not tokens_a or not tokens_b:
        return 0.5  # No evidence either way; stay neutral.
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _merge_cluster(category: str, cluster: List[ObjectObservation]) -> FusedObject:
    """Collapse corroborating observations into one entity."""
    ordered = sorted(cluster, key=lambda o: -o.confidence)
    best = ordered[0]

    confidence = _combine_confidence([o.confidence for o in ordered])
    weights = [(o, max(o.confidence, 1e-3)) for o in ordered]

    # The primary bbox comes from the most confident *unoccluded* view where
    # possible, since grounding depends on a clean bottom edge.
    clean = [o for o in ordered if o.bbox and not o.base_occluded]
    primary = clean[0] if clean else best

    prior = catalog.get_prior(category)

    return FusedObject(
        category=category,
        label=best.label,
        group=prior.group if prior else "furniture",
        size_bucket=_weighted_vote([(o.size_bucket, w) for o, w in weights], best.size_bucket),
        support=_weighted_vote([(o.support, w) for o, w in weights], best.support),
        support_category="",  # resolved in relations.py once ids are stable
        on_wall=_weighted_vote([(o.on_wall, w) for o, w in weights], best.on_wall),
        facing=_weighted_vote([(o.facing, w) for o, w in weights], best.facing),
        material=_weighted_vote([(o.material, w) for o, w in weights], best.material),
        color_hex=_average_color([(o.color_hex, w) for o, w in weights]),
        confidence=confidence,
        # One confident look is enough to clear the uncertain flag; requiring
        # every view to agree would penalise objects seen edge-on.
        uncertain=all(o.uncertain for o in ordered),
        partially_visible=all(o.partially_visible for o in ordered),
        base_occluded=all(o.base_occluded for o in ordered),
        primary_bbox=primary.bbox,
        primary_image=primary.image_id,
        source_images=sorted({o.image_id for o in ordered}),
        observation_count=len(ordered),
    )


def _combine_confidence(values: Sequence[float]) -> float:
    """Damped noisy-OR: corroboration helps, but with diminishing returns."""
    if not values:
        return 0.0
    ordered = sorted(values, reverse=True)
    combined = ordered[0]
    for value in ordered[1:]:
        combined += (1.0 - combined) * value * CORROBORATION_GAIN
    return round(min(0.99, combined), 4)


def _group_rank(group: str) -> int:
    return {"furniture": 0, "appliance": 1, "fixture": 2, "decor": 3}.get(group, 4)


# ---------------------------------------------------------------------------
# Lights, openings, architecture
# ---------------------------------------------------------------------------


def _fuse_lights(observations: Sequence[ImageObservation]) -> List[FusedLight]:
    """Merge luminaires by kind, taking the max count any one image saw."""
    by_kind: Dict[str, List[LightObservation]] = {}
    for image_obs in observations:
        for light in image_obs.lights:
            by_kind.setdefault(light.kind, []).append(light)

    fused: List[FusedLight] = []
    for kind in sorted(by_kind):
        group = by_kind[kind]

        # Per-image totals, then max — the same six downlights seen twice are
        # still six downlights.
        per_image: Dict[str, int] = {}
        for light in group:
            per_image[light.image_id] = per_image.get(light.image_id, 0) + light.count
        count = max(per_image.values()) if per_image else 1

        ordered = sorted(group, key=lambda x: -x.confidence)
        best = ordered[0]
        weights = [(x, max(x.confidence, 1e-3)) for x in ordered]

        fused.append(
            FusedLight(
                kind=kind,
                mounting=_weighted_vote([(x.mounting, w) for x, w in weights], best.mounting),
                count=count,
                cct_k=_weighted_mean([(x.cct_k, w) for x, w in weights], best.cct_k),
                brightness=_weighted_mean([(x.brightness, w) for x, w in weights], 1.0),
                is_on=any(x.is_on for x in ordered),
                confidence=_combine_confidence([x.confidence for x in ordered]),
                uncertain=all(x.uncertain for x in ordered),
                primary_bbox=best.bbox,
                source_images=sorted({x.image_id for x in ordered}),
                instance_index=len(fused),
            )
        )

    return fused


def _fuse_openings(observations: Sequence[ImageObservation]) -> List[OpeningObservation]:
    """Deduplicate openings by (kind, wall side), keeping the best view."""
    best: Dict[Tuple[str, str], OpeningObservation] = {}
    counts: Dict[Tuple[str, str], int] = {}

    for image_obs in observations:
        per_image: Dict[Tuple[str, str], int] = {}
        for opening in image_obs.openings:
            key = (opening.kind, opening.on_wall)
            per_image[key] = per_image.get(key, 0) + 1
            if key not in best or opening.confidence > best[key].confidence:
                best[key] = opening
        for key, value in per_image.items():
            counts[key] = max(counts.get(key, 0), value)

    result: List[OpeningObservation] = []
    for key, template in best.items():
        for index in range(counts.get(key, 1)):
            clone = OpeningObservation(**vars(template))
            clone.local_id = f"{template.kind}_{template.on_wall}_{index + 1}"
            result.append(clone)
    return result


def _dedupe_architecture(observations: Sequence[ImageObservation]) -> List[ArchObservation]:
    """Keep the highest-confidence instance of each structural kind per image count."""
    per_image_counts: Dict[str, int] = {}
    best: Dict[str, ArchObservation] = {}

    for image_obs in observations:
        local: Dict[str, int] = {}
        for element in image_obs.architecture:
            local[element.kind] = local.get(element.kind, 0) + 1
            if element.kind not in best or element.confidence > best[element.kind].confidence:
                best[element.kind] = element
        for kind, count in local.items():
            per_image_counts[kind] = max(per_image_counts.get(kind, 0), count)

    result: List[ArchObservation] = []
    for kind, template in best.items():
        for index in range(per_image_counts.get(kind, 1)):
            clone = ArchObservation(**vars(template))
            clone.local_id = f"{kind}_{index + 1}"
            result.append(clone)
    return result


def _fuse_relationships(
    observations: Sequence[ImageObservation], id_map: Dict[Tuple[str, str], str]
) -> List[RelationObservation]:
    """Rewrite relationships onto fused ids and merge duplicates."""
    merged: Dict[Tuple[str, str, str], List[float]] = {}

    for image_obs in observations:
        for relation in image_obs.relationships:
            subject = id_map.get((image_obs.image_id, relation.subject))
            target = id_map.get((image_obs.image_id, relation.object))
            if subject is None or target is None or subject == target:
                continue
            merged.setdefault((subject, relation.predicate, target), []).append(
                relation.confidence
            )

    return [
        RelationObservation(
            subject=subject,
            predicate=predicate,
            object=target,
            confidence=_combine_confidence(values),
        )
        for (subject, predicate, target), values in sorted(merged.items())
    ]


# ---------------------------------------------------------------------------
# Attribute merging helpers
# ---------------------------------------------------------------------------


def _resolve_room_type(
    observations: Sequence[ImageObservation], objects: Sequence[FusedObject]
) -> Tuple[str, float]:
    """Reconcile the model's stated room type with the furniture actually found.

    Furniture is the stronger signal — a bed in the room outweighs the model
    labelling it a living room — so evidence wins ties.
    """
    declared = _vote(
        [(o.room_type, o.room_type_confidence) for o in observations if o.room_type != "unknown"],
        "unknown",
    )
    evidence_type, evidence_conf = catalog.infer_room_type([o.category for o in objects])

    if declared == "unknown":
        return evidence_type, evidence_conf
    if evidence_type == "unknown":
        declared_conf = max(
            (o.room_type_confidence for o in observations if o.room_type == declared), default=0.5
        )
        return declared, declared_conf
    if declared == evidence_type:
        return declared, min(0.99, max(evidence_conf, 0.6) + 0.2)
    return evidence_type, evidence_conf * 0.8


def _merge_finishes(finishes: Sequence[Finish]) -> Finish:
    """Confidence-weighted merge of a surface finish across images."""
    usable = [f for f in finishes if f.confidence > 0.0] or list(finishes)
    if not usable:
        return Finish()

    ordered = sorted(usable, key=lambda f: -f.confidence)
    weights = [(f, max(f.confidence, 1e-3)) for f in ordered]
    material = _weighted_vote([(f.material, w) for f, w in weights], ordered[0].material)
    prior = catalog.get_material(material)

    return Finish(
        material=material,
        color_hex=_average_color([(f.color_hex, w) for f, w in weights]),
        roughness=_weighted_mean([(f.roughness, w) for f, w in weights], prior.roughness),
        metallic=prior.metallic,
        finish=_weighted_vote([(f.finish, w) for f, w in weights], ordered[0].finish),
        description=ordered[0].description,
        confidence=_combine_confidence([f.confidence for f in ordered]),
    )


def _vote(pairs: Sequence[Tuple[str, float]], default: str) -> str:
    return _weighted_vote(pairs, default)


def _weighted_vote(pairs: Sequence[Tuple[str, float]], default: str) -> str:
    tally: Dict[str, float] = {}
    for value, weight in pairs:
        if value and value not in ("unknown", ""):
            tally[value] = tally.get(value, 0.0) + weight
    if not tally:
        return default
    # Sort by weight then name so ties resolve deterministically.
    return max(sorted(tally), key=lambda k: tally[k])


def _weighted_mean(pairs: Sequence[Tuple[float, float]], default: float) -> float:
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 0:
        return default
    return sum(v * w for v, w in pairs) / total_weight


def _average_color(pairs: Sequence[Tuple[str, float]]) -> str:
    """Confidence-weighted mean colour, in sRGB space."""
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 0:
        return pairs[0][0] if pairs else "#BFBFBF"

    r = g = b = 0.0
    for hex_value, weight in pairs:
        cr, cg, cb = _hex_to_rgb(hex_value)
        r += cr * weight
        g += cg * weight
        b += cb * weight

    return "#{:02X}{:02X}{:02X}".format(
        int(round(255 * r / total_weight)),
        int(round(255 * g / total_weight)),
        int(round(255 * b / total_weight)),
    )


def _hex_to_rgb(value: str) -> Tuple[float, float, float]:
    text = (value or "#BFBFBF").lstrip("#")
    if len(text) != 6:
        text = "BFBFBF"
    try:
        return (
            int(text[0:2], 16) / 255.0,
            int(text[2:4], 16) / 255.0,
            int(text[4:6], 16) / 255.0,
        )
    except ValueError:
        return (0.75, 0.75, 0.75)
