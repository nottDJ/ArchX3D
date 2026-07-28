"""
Tests for reference-versus-generated similarity scoring.

Synthetic images are used rather than fixtures on disk: the point is that a
*known* difference produces the right axis drop and the right finding, and a
hand-made pair makes that unambiguous. A photograph pair would test the
metric's taste rather than its behaviour.

The object axis needs no images at all, so it is tested independently — that
separation is deliberate, and is what lets the report stay useful when Pillow
is unavailable.
"""

from __future__ import annotations

import pytest

from vision import similarity
from vision.schema import (
    Dimensions,
    Room,
    SceneGraph,
    SceneObject,
    Vec3,
    ViewPoint,
)

Image = pytest.importorskip("PIL.Image", reason="pixel axes need Pillow")
ImageDraw = pytest.importorskip("PIL.ImageDraw")
pytest.importorskip("numpy", reason="pixel axes need numpy")


# ---------------------------------------------------------------------------
# Synthetic imagery
# ---------------------------------------------------------------------------


def render(path, background, blocks=(), speckle=0):
    """A crude interior: a wall colour with furniture-shaped blocks on it."""
    import random

    image = Image.new("RGB", (320, 240), background)
    draw = ImageDraw.Draw(image)
    for x0, y0, x1, y1, colour in blocks:
        draw.rectangle([x0, y0, x1, y1], fill=colour)

    if speckle:
        random.seed(7)
        pixels = image.load()
        for _ in range(speckle):
            x, y = random.randrange(320), random.randrange(240)
            pixels[x, y] = tuple(
                min(255, max(0, channel + random.randint(-45, 45)))
                for channel in pixels[x, y]
            )

    image.save(path)
    return str(path)


BLOCKS = ((40, 120, 180, 220, (74, 82, 89)), (200, 150, 280, 220, (92, 64, 51)))


@pytest.fixture
def reference(tmp_path):
    return render(tmp_path / "ref.jpg", (237, 231, 221), BLOCKS, speckle=4000)


@pytest.fixture
def graph():
    room = Room(id="r1", area=30.0, bounds_min=(0.0, 0.0), bounds_max=(6.0, 5.0),
                style="modern", source_images=["img0"])
    sofa = SceneObject(
        id="sofa", category="sofa", room_id="r1", source_images=["img0"],
        dimensions=Dimensions(2.4, 0.95, 0.8), confidence=0.9,
        asset="sofa_low_modern", asset_score=0.9,
    )
    return SceneGraph(rooms=[room], objects=[sofa])


@pytest.fixture
def viewpoint():
    return ViewPoint(image_id="img0", room_id="r1", source_image="ref.jpg")


def compare(graph, viewpoint, reference, rendered):
    return similarity.compare(graph, [(viewpoint, reference, rendered)])


# ---------------------------------------------------------------------------
# Discrimination
# ---------------------------------------------------------------------------


class TestAxisDiscrimination:
    def test_a_near_identical_render_scores_high(self, tmp_path, graph, viewpoint, reference):
        near = render(tmp_path / "near.jpg", (237, 231, 221),
                      ((42, 122, 182, 222, (76, 84, 91)),
                       (202, 152, 282, 222, (94, 66, 53))), speckle=3800)
        report = compare(graph, viewpoint, reference, near)

        assert report.axis_means()["colour"] > 0.9
        assert report.axis_means()["lighting"] > 0.9
        assert report.axis_means()["layout"] > 0.8

    def test_a_dark_render_is_caught_by_the_lighting_axis(
        self, tmp_path, graph, viewpoint, reference
    ):
        dark = render(tmp_path / "dark.jpg", (70, 66, 60),
                      ((40, 120, 180, 220, (30, 34, 38)),))
        report = compare(graph, viewpoint, reference, dark)

        assert report.axis_means()["lighting"] < 0.7
        assert any(
            "darker" in finding.detail for finding in report.all_findings()
        ), "the report must say which way it is wrong"

    def test_a_warm_render_is_named_as_too_warm(self, tmp_path, graph, viewpoint, reference):
        warm = render(tmp_path / "warm.jpg", (250, 205, 150),
                      ((40, 120, 180, 220, (120, 80, 50)),))
        report = compare(graph, viewpoint, reference, warm)

        assert any("too warm" in f.detail for f in report.all_findings())
        assert "lighting" in report.remedies()

    def test_a_cool_render_is_named_as_too_cool(self, tmp_path, graph, viewpoint):
        warm_reference = render(tmp_path / "warmref.jpg", (250, 210, 165), BLOCKS)
        cool = render(tmp_path / "cool.jpg", (200, 215, 250), BLOCKS)
        report = compare(graph, viewpoint, warm_reference, cool)

        assert any("too cool" in f.detail for f in report.all_findings())

    def test_moved_furniture_is_caught_by_the_layout_axis(
        self, tmp_path, graph, viewpoint, reference
    ):
        moved = render(tmp_path / "moved.jpg", (237, 231, 221),
                       ((200, 20, 300, 110, (74, 82, 89)),), speckle=4000)
        report = compare(graph, viewpoint, reference, moved)

        assert report.axis_means()["layout"] < 0.7
        assert any(f.axis == "layout" for f in report.all_findings())

    def test_a_flat_render_is_caught_by_the_material_axis(
        self, tmp_path, graph, viewpoint, reference
    ):
        """Procedural surfaces with no grain against a textured photograph."""
        flat = render(tmp_path / "flat.jpg", (237, 231, 221), BLOCKS)  # no speckle
        report = compare(graph, viewpoint, reference, flat)

        assert report.axis_means()["material"] < report.axis_means()["colour"]

    def test_the_wrong_colour_is_reported_with_both_values(
        self, tmp_path, graph, viewpoint, reference
    ):
        green = render(tmp_path / "green.jpg", (110, 170, 110), BLOCKS)
        report = compare(graph, viewpoint, reference, green)

        colour_findings = [f for f in report.all_findings() if f.axis == "colour"
                           and "differs" in f.detail]
        assert colour_findings, "a colour mismatch must be stated explicitly"
        assert "#" in colour_findings[0].detail, "state what was expected and got"


# ---------------------------------------------------------------------------
# Object axis
# ---------------------------------------------------------------------------


class TestObjectAxis:
    def test_everything_built_scores_full(self, graph, viewpoint, reference, tmp_path):
        same = render(tmp_path / "same.jpg", (237, 231, 221), BLOCKS, speckle=4000)
        report = compare(graph, viewpoint, reference, same)
        assert report.views[0].axes["objects"].score == 1.0

    def test_a_dropped_detection_is_reported_as_missing(
        self, graph, viewpoint, reference, tmp_path
    ):
        """The 'missing plant' case, named and explained."""
        graph.objects.append(SceneObject(
            id="plant", category="plant", room_id="r1", source_images=["img0"],
            dimensions=Dimensions(0.5, 0.5, 1.2), confidence=0.35, uncertain=True,
        ))
        same = render(tmp_path / "same.jpg", (237, 231, 221), BLOCKS, speckle=4000)
        report = compare(graph, viewpoint, reference, same)

        assert report.views[0].axes["objects"].score == 0.5
        findings = [f for f in report.all_findings() if f.subject == "plant"]
        assert findings
        assert "plant" in findings[0].detail
        assert "confidence" in findings[0].detail, "say *why* it is missing"

    def test_a_degenerate_object_is_explained_differently(
        self, graph, viewpoint, reference, tmp_path
    ):
        graph.objects.append(SceneObject(
            id="ghost", category="tv", room_id="r1", source_images=["img0"],
            dimensions=Dimensions(0.0, 0.0, 0.0), confidence=0.9,
        ))
        same = render(tmp_path / "same.jpg", (237, 231, 221), BLOCKS, speckle=4000)
        report = compare(graph, viewpoint, reference, same)

        finding = next(f for f in report.all_findings() if f.subject == "ghost")
        assert "no usable size" in finding.detail

    def test_objects_from_another_image_are_not_counted(
        self, graph, viewpoint, reference, tmp_path
    ):
        """A bedroom photo's furniture must not score against a living room view."""
        graph.objects.append(SceneObject(
            id="bed", category="bed", room_id="r2", source_images=["img9"],
            dimensions=Dimensions(1.6, 2.0, 0.6), confidence=0.9, uncertain=True,
        ))
        same = render(tmp_path / "same.jpg", (237, 231, 221), BLOCKS, speckle=4000)
        report = compare(graph, viewpoint, reference, same)

        assert report.views[0].axes["objects"].score == 1.0

    def test_an_untraceable_view_marks_the_axis_unavailable(
        self, viewpoint, reference, tmp_path
    ):
        """No score is better than a fabricated one."""
        same = render(tmp_path / "same.jpg", (237, 231, 221), BLOCKS)
        report = compare(SceneGraph(rooms=[Room(id="r1")]), viewpoint, reference, same)
        assert report.views[0].axes["objects"].available is False


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


class TestReport:
    def test_a_missing_render_does_not_drag_the_score_down(
        self, graph, viewpoint, reference
    ):
        """Unmeasured is not the same as bad."""
        report = similarity.compare(graph, [(viewpoint, reference, "")])
        pixel_axes = ["colour", "lighting", "layout", "material"]

        assert all(not report.views[0].axes[name].available for name in pixel_axes)
        # Only the object axis counted, and it was perfect.
        assert report.score == 1.0

    def test_remedies_are_ordered_and_deduplicated(
        self, tmp_path, graph, viewpoint, reference
    ):
        dark = render(tmp_path / "dark.jpg", (60, 58, 54), ((10, 10, 60, 60, (20, 20, 20)),))
        remedies = compare(graph, viewpoint, reference, dark).remedies()

        assert remedies == sorted(set(remedies), key=["materials", "lighting", "assets", "decor"].index)

    def test_a_poor_asset_match_is_reported_with_its_score(
        self, graph, viewpoint, reference, tmp_path
    ):
        """"Closest match available, 55% similar" rather than silence."""
        graph.objects[0].asset_score = 0.55
        same = render(tmp_path / "same.jpg", (237, 231, 221), BLOCKS)
        report = compare(graph, viewpoint, reference, same)

        finding = next(f for f in report.all_findings() if "closest available" in f.detail)
        assert "55%" in finding.detail
        assert finding.remedy == "assets"

    def test_categories_with_no_model_are_named(self, graph, viewpoint, reference, tmp_path):
        graph.objects[0].asset = "generic_seating"
        same = render(tmp_path / "same.jpg", (237, 231, 221), BLOCKS)
        report = compare(graph, viewpoint, reference, same)

        assert any("no procedural model" in f.detail for f in report.all_findings())

    def test_the_report_serialises(self, tmp_path, graph, viewpoint, reference):
        same = render(tmp_path / "same.jpg", (237, 231, 221), BLOCKS, speckle=4000)
        payload = compare(graph, viewpoint, reference, same).to_dict()

        assert set(payload) >= {
            "score", "axis_means", "views", "findings", "remedies", "summary"
        }
        assert 0.0 <= payload["score"] <= 1.0
        for view in payload["views"]:
            assert set(view) >= {"image_id", "reference", "rendered", "score", "axes"}

    def test_the_summary_names_the_weakest_axes(self, tmp_path, graph, viewpoint, reference):
        dark = render(tmp_path / "dark.jpg", (50, 48, 44), ((10, 10, 60, 60, (20, 20, 20)),))
        summary = compare(graph, viewpoint, reference, dark).summary()

        assert "%" in summary
        assert "weakest" in summary

    def test_no_views_is_handled(self, graph):
        report = similarity.compare(graph, [])
        assert report.score == 0.0
        assert "no comparable views" in report.summary()

    def test_scores_average_across_views(self, tmp_path, graph, reference):
        good = render(tmp_path / "good.jpg", (237, 231, 221), BLOCKS, speckle=4000)
        bad = render(tmp_path / "bad.jpg", (30, 30, 30), ())
        first = ViewPoint(image_id="img0", room_id="r1", source_image="ref.jpg")
        second = ViewPoint(image_id="img0", room_id="r1", source_image="ref.jpg")

        report = similarity.compare(graph, [(first, reference, good), (second, reference, bad)])
        assert len(report.views) == 2
        assert report.views[1].score < report.score < report.views[0].score

    def test_an_unreadable_render_is_not_a_crash(self, tmp_path, graph, viewpoint, reference):
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not an image")
        report = compare(graph, viewpoint, reference, str(broken))

        assert not report.views[0].axes["colour"].available
        assert report.views[0].axes["objects"].available
