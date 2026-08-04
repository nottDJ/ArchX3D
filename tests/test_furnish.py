"""Procedural interior generation.

Stage 7: given a semantically typed scene graph, produce a furnished one.

The guarantees under test are the ones that separate a usable layout from a
plausible-looking pile of boxes: nothing overlaps, nothing blocks a door,
nothing leaves the room, wall furniture ends up against walls, and every item
that could not be placed says why.
"""

from __future__ import annotations

import math

import pytest

from furnish import furnish, placement, programme
from vision.schema import (
    ColourPalette, Dimensions, Opening, Room, SceneGraph, SceneObject, Vec3, Wall,
)


def make_room(room_id="room_0", room_type="bedroom", width=4.0, depth=3.5, **kwargs):
    settings = dict(
        id=room_id,
        room_type=room_type,
        polygon=[(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth)],
        bounds_min=(0.0, 0.0),
        bounds_max=(width, depth),
        area=width * depth,
        ceiling_height=3.0,
    )
    settings.update(kwargs)
    return Room(**settings)


def make_graph(*rooms, openings=()):
    return SceneGraph(rooms=list(rooms), openings=list(openings))


def quiet(*args, **kwargs):
    pass


# ---------------------------------------------------------------------------
# Programme
# ---------------------------------------------------------------------------


class TestProgramme:
    def test_a_bedroom_gets_a_bed_first(self):
        planned = programme.plan_room("bedroom", 14.0)
        assert planned
        assert planned[0].category == "bed"

    def test_small_rooms_get_fewer_items(self):
        small = programme.plan_room("bedroom", 8.0)
        large = programme.plan_room("bedroom", 28.0)
        assert len(small) < len(large)
        # But both still get the essential.
        assert any(p.category == "bed" for p in small)

    def test_below_the_minimum_area_nothing_is_planned(self):
        assert programme.plan_room("bedroom", 3.0) == []

    def test_counts_scale_with_area(self):
        small = programme.plan_room("dining_room", 8.0)
        large = programme.plan_room("dining_room", 30.0)
        chairs_small = sum(1 for p in small if p.category == "dining_chair")
        chairs_large = sum(1 for p in large if p.category == "dining_chair")
        assert chairs_large > chairs_small

    def test_counts_are_capped(self):
        planned = programme.plan_room("dining_room", 500.0)
        chairs = sum(1 for p in planned if p.category == "dining_chair")
        assert chairs <= 8

    def test_circulation_types_are_left_unfurnished(self):
        assert programme.plan_room("shaft", 5.0) == []
        assert programme.plan_room("staircase", 8.0) == []

    def test_unknown_room_type_is_not_furnished(self):
        """Furnishing a room we could not identify would be inventing twice."""
        assert programme.plan_room("unknown", 20.0) == []

    def test_ordering_is_by_importance(self):
        planned = programme.plan_room("living_room", 30.0)
        importances = [p.importance for p in planned]
        assert importances == sorted(importances, reverse=True)

    def test_every_programme_category_exists_in_the_catalog(self):
        """A category with no prior would emit an object nothing can build."""
        from vision import assets, catalog

        for room_type, items in programme.PROGRAMMES.items():
            for item in items:
                assert item.category in catalog.OBJECT_CATALOG, (
                    f"{room_type}: {item.category}"
                )
                assert assets.variants_for(item.category), (
                    f"{room_type}: {item.category} has no asset builder"
                )

    def test_every_lighting_kind_exists(self):
        from vision import catalog

        for room_type, items in programme.LIGHTING_PROGRAMMES.items():
            for item in items:
                assert item.kind in catalog.LIGHT_TYPES, f"{room_type}: {item.kind}"

    def test_dependent_items_name_a_real_dependency(self):
        for room_type, items in programme.PROGRAMMES.items():
            categories = {i.category for i in items}
            for item in items:
                if item.requires:
                    assert item.requires in categories, (
                        f"{room_type}: {item.category} requires "
                        f"{item.requires}, which is not in its programme"
                    )


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


class TestPlacement:
    def _space(self, width=4.0, depth=3.5, doors=(), windows=()):
        polygon = [(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth)]
        return placement.RoomSpace(
            polygon=polygon,
            bounds_min=(0.0, 0.0),
            bounds_max=(width, depth),
            walls=[(polygon[i], polygon[(i + 1) % 4]) for i in range(4)],
            doors=list(doors),
            windows=list(windows),
        )

    def test_wall_affine_items_end_up_against_a_wall(self):
        solver = placement.Solver(self._space())
        result = solver.place("wardrobe", (1.8, 0.6, 2.2), wall_affinity=1.0)
        assert result is not None
        assert result.against_wall

    # -- footprint sanity --------------------------------------------------

    def test_an_item_that_swamps_the_room_is_refused(self):
        """A king bed proposed for a toilet is a wrong-room error."""
        toilet = self._space(1.6, 1.7)          # 2.7 m2
        solver = placement.Solver(toilet)

        assert solver.place("bed", (2.0, 2.1, 0.6), wall_affinity=0.9) is None
        assert len(solver.rejections) == 1

    def test_the_refusal_says_it_is_the_wrong_room(self):
        """The reason has to distinguish this from a tight packing failure.

        "No candidate pose" invites nudging the furniture; the useful
        correction here is to re-examine which room the item was assigned to.
        """
        solver = placement.Solver(self._space(1.6, 1.7))
        solver.place("bed", (2.0, 2.1, 0.6), wall_affinity=0.9)

        reason = solver.rejections[0].reason
        assert "larger room" in reason
        assert "%" in reason, "the reason should quantify the overrun"

    def test_a_snug_fit_is_still_allowed(self):
        """The limit catches absurdity, not tightness.

        A double bed in a small single bedroom legitimately takes over half
        the floor, and refusing that would empty rooms that are simply small.
        """
        bedroom = self._space(2.6, 3.2)         # 8.3 m2
        solver = placement.Solver(bedroom)

        result = solver.place("bed", (1.4, 1.9, 0.6), wall_affinity=0.9)
        assert result is not None, "a 32% footprint must still place"

    def test_a_rug_may_cover_most_of_the_floor(self):
        """Floor coverings are exempt — covering the floor is the point.

        2.5 m in a 3 m room is 69% of it, comfortably past the limit that
        would refuse a solid object of the same footprint.
        """
        solver = placement.Solver(self._space(3.0, 3.0))
        result = solver.place("rug", (2.5, 2.5, 0.02), wall_affinity=0.0)

        assert result is not None
        assert not any("larger room" in r.reason for r in solver.rejections)

    def test_a_solid_object_of_the_same_footprint_is_refused(self):
        """The exemption is about being a covering, not about being large."""
        solver = placement.Solver(self._space(3.0, 3.0))
        assert solver.place("cabinet", (2.5, 2.5, 1.2), wall_affinity=1.0) is None
        assert any("larger room" in r.reason for r in solver.rejections)

    def test_room_area_comes_from_the_outline_not_the_bounds(self):
        """An L-shaped room is smaller than its bounding box."""
        el = placement.RoomSpace(
            polygon=[(0, 0), (4, 0), (4, 2), (2, 2), (2, 4), (0, 4)],
            bounds_min=(0.0, 0.0), bounds_max=(4.0, 4.0),
        )
        assert el.area == pytest.approx(12.0)   # not 16

    def test_free_standing_items_stay_off_the_walls(self):
        solver = placement.Solver(self._space(6.0, 5.0))
        result = solver.place("dining_table", (1.4, 0.9, 0.75), wall_affinity=0.0)
        assert result is not None
        assert not result.against_wall

    def test_placed_items_stay_inside_the_room(self):
        space = self._space()
        solver = placement.Solver(space)
        result = solver.place("bed", (1.6, 2.0, 0.55), wall_affinity=0.95)
        assert result is not None
        for corner in result.corners():
            assert placement.point_in_polygon(corner, space.polygon)

    def test_two_items_do_not_overlap(self):
        solver = placement.Solver(self._space(5.0, 4.0))
        first = solver.place("bed", (1.6, 2.0, 0.55), wall_affinity=0.95)
        second = solver.place("wardrobe", (1.8, 0.6, 2.2), wall_affinity=1.0)
        assert first is not None and second is not None
        assert not placement.rects_overlap(first.corners(), second.corners())

    def test_a_door_swing_is_kept_clear(self):
        space = self._space(4.0, 3.5, doors=[((2.0, 0.0), 0.9)])
        solver = placement.Solver(space)
        result = solver.place("wardrobe", (1.8, 0.6, 2.2), wall_affinity=1.0)
        assert result is not None
        swing = placement.rect_corners(
            2.0, 0.0, 0.9, placement.DOOR_SWING_CLEARANCE_M * 2.0, 0.0
        )
        assert not placement.rects_overlap(result.corners(), swing)

    def test_tall_items_do_not_block_a_window(self):
        space = self._space(4.0, 3.5, windows=[((2.0, 3.5), 1.2)])
        solver = placement.Solver(space)
        result = solver.place("wardrobe", (1.8, 0.6, 2.2), wall_affinity=1.0)
        assert result is not None
        assert math.dist(result.position, (2.0, 3.5)) > 0.9

    def test_low_items_may_sit_under_a_window(self):
        """A window bars a wardrobe, not a bed."""
        space = self._space(4.0, 3.5, windows=[((2.0, 3.5), 1.2)])
        solver = placement.Solver(space)
        assert solver.place("bed", (1.6, 2.0, 0.55), wall_affinity=0.95) is not None

    def test_an_item_too_big_for_the_room_is_rejected_with_a_reason(self):
        solver = placement.Solver(self._space(2.0, 2.0))
        result = solver.place("sectional", (4.0, 2.0, 0.85), wall_affinity=0.8)
        assert result is None
        assert solver.rejections
        assert solver.rejections[-1].category == "sectional"
        assert solver.rejections[-1].reason

    def test_filling_a_room_eventually_rejects_rather_than_stacking(self):
        solver = placement.Solver(self._space(3.0, 3.0))
        placed = sum(
            1 for _ in range(12)
            if solver.place("cabinet", (1.0, 0.5, 1.6), wall_affinity=1.0)
        )
        assert placed >= 1
        assert len(solver.rejections) >= 1
        # Whatever went in must still be mutually disjoint.
        for i, a in enumerate(solver.placements):
            for b in solver.placements[i + 1:]:
                assert not placement.rects_overlap(a.corners(), b.corners())

    def test_clearance_is_kept_in_front_of_furniture(self):
        """Nothing may stand in the space an item needs to be usable.

        Asserted on the clearance zone rather than on raw separation: two
        wardrobes standing side by side and touching is perfectly normal, and
        a distance test would fail that while passing the case that actually
        matters — one wardrobe blocking another's doors.
        """
        solver = placement.Solver(self._space(4.0, 1.6))
        solver.place("wardrobe", (1.8, 0.6, 2.2), wall_affinity=1.0)
        solver.place("wardrobe", (1.8, 0.6, 2.2), wall_affinity=1.0)

        for i, item in enumerate(solver.placements):
            zone = self._front_zone(item)
            for j, other in enumerate(solver.placements):
                if i == j:
                    continue
                assert not placement.rects_overlap(zone, other.corners()), (
                    f"placement {j} stands in front of placement {i}"
                )

    @staticmethod
    def _front_zone(item):
        """The usable space in front of a placement, as the solver defines it."""
        width, depth, _ = item.dimensions
        gap = max(placement.DEFAULT_USE_CLEARANCE_M, depth * 0.5)
        theta = math.radians(item.rotation_z)
        fx, fy = -math.sin(theta), math.cos(theta)
        front = (
            item.position[0] + fx * (depth / 2.0 + gap / 2.0),
            item.position[1] + fy * (depth / 2.0 + gap / 2.0),
        )
        return placement.rect_corners(
            front[0], front[1], width * 0.8, gap, item.rotation_z
        )

    def test_a_rug_may_lie_under_furniture(self):
        """A floor covering is not an obstruction; it is meant to go under."""
        solver = placement.Solver(self._space(5.0, 4.0))
        table = solver.place("coffee_table", (1.2, 0.6, 0.4), wall_affinity=0.0)
        rug = solver.place("rug", (2.4, 1.7, 0.02), wall_affinity=0.0)
        assert table is not None and rug is not None

    def test_degenerate_dimensions_are_refused(self):
        solver = placement.Solver(self._space())
        assert solver.place("ghost", (0.0, 0.0, 0.0), wall_affinity=0.0) is None


class TestGeometryHelpers:
    def test_overlap_detects_a_rotated_intersection(self):
        a = placement.rect_corners(0, 0, 2, 1, 0)
        b = placement.rect_corners(0.5, 0, 2, 1, 45)
        assert placement.rects_overlap(a, b)

    def test_separated_rectangles_do_not_overlap(self):
        a = placement.rect_corners(0, 0, 1, 1, 0)
        b = placement.rect_corners(5, 5, 1, 1, 0)
        assert not placement.rects_overlap(a, b)

    def test_shrink_reduces_area_without_inverting(self):
        square = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
        inner = placement.shrink_polygon(square, 0.5)
        assert len(inner) == 4
        for x, y in inner:
            assert 0.0 < x < 4.0 and 0.0 < y < 4.0


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


class TestFurnisher:
    def test_a_bedroom_is_furnished(self):
        graph = make_graph(make_room(room_type="bedroom", width=4.0, depth=3.5))
        report = furnish(graph, log=quiet)

        categories = {o.category for o in graph.objects}
        assert "bed" in categories
        assert report.objects_created == len(graph.objects)

    def test_every_room_gets_light(self):
        """A furnished room with no luminaire renders black."""
        graph = make_graph(
            make_room("room_0", "bedroom"),
            make_room("room_1", "kitchen", width=3.0, depth=3.0),
        )
        furnish(graph, log=quiet)
        for room in graph.rooms:
            assert any(lt.room_id == room.id for lt in graph.lights), room.id

    def test_unlit_circulation_still_gets_a_fitting(self):
        graph = make_graph(make_room(room_type="staircase", width=2.5, depth=3.0))
        furnish(graph, log=quiet)
        assert graph.objects == []
        assert graph.lights

    def test_observed_rooms_are_left_alone(self):
        """An observation always beats a convention."""
        room = make_room(room_type="bedroom")
        graph = make_graph(room)
        graph.objects.append(SceneObject(
            id="seen_1", category="sofa", room_id=room.id,
            position=Vec3(2.0, 1.75, 0.0),
            dimensions=Dimensions(2.0, 0.9, 0.85), confidence=0.9,
        ))

        furnish(graph, log=quiet)

        assert len(graph.objects) == 1
        assert graph.objects[0].id == "seen_1"

    def test_overwrite_furnishes_an_observed_room(self):
        room = make_room(room_type="bedroom")
        graph = make_graph(room)
        graph.objects.append(SceneObject(
            id="seen_1", category="sofa", room_id=room.id,
            position=Vec3(2.0, 1.75, 0.0),
            dimensions=Dimensions(2.0, 0.9, 0.85), confidence=0.9,
        ))
        furnish(graph, overwrite=True, log=quiet)
        assert len(graph.objects) > 1

    def test_generated_objects_are_flagged_as_procedural(self):
        """A design decision must be distinguishable from an observation."""
        graph = make_graph(make_room(room_type="bedroom"))
        furnish(graph, log=quiet)
        assert graph.objects
        for obj in graph.objects:
            assert "procedural" in obj.flags
            assert obj.observation_count == 0
            assert obj.source_images == []

    def test_nothing_overlaps_across_a_whole_room(self):
        """No two solid items share floor space.

        Floor coverings are excluded: a rug is *supposed* to lie under a
        coffee table, and the `centered_under` relation puts it there
        deliberately.
        """
        graph = make_graph(make_room(room_type="living_room", width=6.0, depth=5.0))
        furnish(graph, log=quiet)

        solid = [
            o for o in graph.objects
            if o.support == "floor"
            and o.dimensions.height > placement.FLOOR_COVERING_HEIGHT_M
        ]
        assert len(solid) >= 3, "the fixture should furnish enough to be a real test"

        for i, a in enumerate(solid):
            for b in solid[i + 1:]:
                assert not placement.rects_overlap(
                    a.footprint_corners(), b.footprint_corners()
                ), f"{a.category} overlaps {b.category}"

    def test_everything_stays_inside_the_room(self):
        room = make_room(room_type="living_room", width=6.0, depth=5.0)
        graph = make_graph(room)
        furnish(graph, log=quiet)

        for obj in graph.objects:
            if obj.support != "floor":
                continue
            for corner in obj.footprint_corners():
                assert placement.point_in_polygon(corner, room.polygon), obj.category

    def test_a_door_is_not_blocked(self):
        room = make_room(room_type="bedroom", width=4.0, depth=3.5)
        door = Opening(
            id="d1", kind="door", room_id=room.id,
            position=Vec3(2.0, 0.0, 0.0), width=0.9,
        )
        graph = make_graph(room, openings=[door])
        furnish(graph, log=quiet)

        swing = placement.rect_corners(
            2.0, 0.0, 0.9, placement.DOOR_SWING_CLEARANCE_M * 2.0, 0.0
        )
        for obj in graph.objects:
            if obj.support == "floor":
                assert not placement.rects_overlap(
                    obj.footprint_corners(), swing
                ), f"{obj.category} blocks the door"

    def test_bedside_tables_flank_the_bed(self):
        graph = make_graph(make_room(room_type="bedroom", width=4.5, depth=4.0))
        furnish(graph, log=quiet)

        bed = next(o for o in graph.objects if o.category == "bed")
        tables = [o for o in graph.objects if o.category == "bedside_table"]
        assert tables
        for table in tables:
            distance = math.dist(
                (table.position.x, table.position.y), (bed.position.x, bed.position.y)
            )
            assert distance < 2.0, "a bedside table should be beside the bed"

    def test_rejections_are_reported_not_swallowed(self):
        graph = make_graph(make_room(room_type="living_room", width=3.6, depth=3.4))
        report = furnish(graph, log=quiet)
        record = report.rooms[0]
        # Something in a generous programme will not fit a small room, and
        # whatever it is must come back with a stated reason.
        for category, reason in record.rejected:
            assert reason, category

    def test_a_room_too_small_explains_itself_accurately(self):
        """"No programme for bedroom" would be false and would mislead."""
        graph = make_graph(make_room(room_type="bedroom", width=1.6, depth=1.6))
        report = furnish(graph, log=quiet)
        reason = report.rooms[0].skipped_reason
        assert "m2" in reason
        assert "no furniture programme" not in reason

    def test_palette_drives_generated_colours(self):
        """Layout is conventional; appearance still comes from the imagery."""
        room = make_room(room_type="bedroom")
        room.palette = ColourPalette(furniture="#123456", secondary="#654321")
        graph = make_graph(room)
        furnish(graph, log=quiet)

        furniture = [o for o in graph.objects if o.group == "furniture"]
        assert furniture
        assert any(o.color_hex == "#123456" for o in furniture)

    def test_report_round_trips_to_dict(self):
        graph = make_graph(make_room(room_type="bedroom"))
        data = furnish(graph, log=quiet).to_dict()
        assert data["objects_created"] > 0
        assert data["rooms"]
        assert "rejected" in data["rooms"][0]

    def test_furnishing_is_deterministic(self):
        def run():
            graph = make_graph(make_room(room_type="bedroom", width=4.2, depth=3.6))
            furnish(graph, log=quiet)
            return [
                (o.category, round(o.position.x, 4), round(o.position.y, 4))
                for o in graph.objects
            ]

        assert run() == run()

    def test_a_malformed_room_is_skipped_not_crashed(self):
        graph = make_graph(make_room(
            room_type="bedroom", polygon=[], bounds_min=(0.0, 0.0),
            bounds_max=(0.1, 0.1), area=0.01,
        ))
        report = furnish(graph, log=quiet)
        assert graph.objects == []
        assert report.rooms[0].skipped_reason


class TestEveryRoomIsLit:
    """Lighting is a property of the room, not of who furnished it.

    Furniture and light used to share one exit: a room that arrived with its
    own observed furniture was abandoned before the lighting pass and finished
    with no luminaire. A plan view furnishes every room it covers and
    contributes no lighting at all, so the common case was a fully furnished,
    completely dark building.
    """

    def _graph(self, observed_room=None):
        from vision.schema import Dimensions, Room, SceneGraph, SceneObject, Vec3

        rooms = [
            Room(id="room_0", room_type="bedroom", area=14.0,
                 polygon=[(0.0, 0.0), (4.0, 0.0), (4.0, 3.5), (0.0, 3.5)],
                 bounds_min=(0.0, 0.0), bounds_max=(4.0, 3.5)),
            Room(id="room_1", room_type="unknown", area=6.0,
                 polygon=[(5.0, 0.0), (8.0, 0.0), (8.0, 2.0), (5.0, 2.0)],
                 bounds_min=(5.0, 0.0), bounds_max=(8.0, 2.0)),
        ]
        graph = SceneGraph(rooms=rooms)
        if observed_room:
            graph.objects.append(SceneObject(
                id="obs_bed", category="bed", room_id=observed_room,
                position=Vec3(2.0, 1.75, 0.0),
                dimensions=Dimensions(1.4, 1.9, 0.6), confidence=0.9,
            ))
        return graph

    def test_an_already_furnished_room_still_gets_a_light(self):
        from furnish import furnish

        graph = self._graph(observed_room="room_0")
        furnish(graph, log=lambda *a, **k: None)

        assert any(light.room_id == "room_0" for light in graph.lights), (
            "an observed sofa is not evidence about the ceiling"
        )

    def test_a_room_of_unknown_type_is_not_left_dark(self):
        """Not knowing what a room is does not mean it has no ceiling."""
        from furnish import furnish

        graph = self._graph()
        furnish(graph, log=lambda *a, **k: None)

        assert any(light.room_id == "room_1" for light in graph.lights)

    def test_every_room_ends_up_lit(self):
        from furnish import furnish

        graph = self._graph(observed_room="room_0")
        furnish(graph, log=lambda *a, **k: None)

        lit = {light.room_id for light in graph.lights}
        assert {r.id for r in graph.rooms} <= lit

    def test_procedural_light_is_not_attributed_to_any_image(self):
        """A convention must never masquerade as an observation."""
        from furnish import furnish

        graph = self._graph()
        furnish(graph, log=lambda *a, **k: None)

        assert all(not light.source_images for light in graph.lights)

    def test_an_existing_light_is_not_duplicated(self):
        from furnish import furnish
        from vision.schema import LightSource, Vec3

        graph = self._graph()
        graph.lights.append(LightSource(
            id="observed", kind="ceiling_light", room_id="room_0",
            position=Vec3(2.0, 1.75, 2.7), source_images=["img0"],
        ))
        furnish(graph, log=lambda *a, **k: None)

        assert sum(1 for l in graph.lights if l.room_id == "room_0") == 1
