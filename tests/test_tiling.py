"""Tiled analysis of large, dense images.

The failure this exists for: a 1493 x 1050 sheet carrying two floor plans was
analysed whole and returned 11 objects for an entire house, all from one
corner. The model sees a fixed internal resolution, so everything else was
below the threshold at which it could be resolved.
"""

from __future__ import annotations

import pytest

from vision import tiling

pytest.importorskip("PIL.Image", reason="tiling needs Pillow")


def write_image(path, width, height):
    from PIL import Image

    Image.new("RGB", (width, height), (128, 128, 128)).save(str(path))
    return str(path)


class TestGrid:
    def test_a_wide_sheet_is_cut_into_more_columns_than_rows(self):
        cols, rows = tiling._grid_for(1493, 1050, 9)
        assert cols > rows

    def test_tiles_come_out_near_square(self):
        cols, rows = tiling._grid_for(1493, 1050, 9)
        aspect = (1493 / cols) / (1050 / rows)
        assert 0.7 < aspect < 1.4

    def test_a_three_to_one_image_becomes_a_single_row(self):
        assert tiling._grid_for(3000, 1000, 9) == (3, 1)

    def test_the_cap_is_respected(self):
        cols, rows = tiling._grid_for(4000, 4000, 4)
        assert cols * rows <= 4

    def test_degenerate_sizes_do_not_crash(self):
        assert tiling._grid_for(0, 0, 9) == (1, 1)


class TestShouldTile:
    def test_small_images_are_analysed_whole(self, tmp_path):
        path = write_image(tmp_path / "small.png", 640, 480)
        assert not tiling.should_tile(path, "full")

    def test_a_large_plan_view_is_tiled(self, tmp_path):
        path = write_image(tmp_path / "sheet.png", 1500, 1050)
        assert tiling.should_tile(path, "layout")

    def test_technical_drawings_are_never_tiled(self, tmp_path):
        """Geometry views contribute openings, which tiling cannot help."""
        path = write_image(tmp_path / "cad.png", 2000, 1500)
        assert not tiling.should_tile(path, "geometry")

    def test_a_missing_file_is_not_tiled(self):
        assert not tiling.should_tile("does_not_exist.png", "layout")


class TestTiles:
    def test_tiles_cover_the_whole_image(self, tmp_path):
        path = write_image(tmp_path / "sheet.png", 1500, 1050)
        tiles = tiling.plan_tiles(path, str(tmp_path / "tiles"))
        assert len(tiles) > 1

        assert min(t.rect[0] for t in tiles) == pytest.approx(0.0)
        assert min(t.rect[1] for t in tiles) == pytest.approx(0.0)
        assert max(t.rect[2] for t in tiles) == pytest.approx(1.0)
        assert max(t.rect[3] for t in tiles) == pytest.approx(1.0)

    def test_tiles_overlap_their_neighbours(self, tmp_path):
        """An object on a seam must be wholly visible in at least one tile."""
        path = write_image(tmp_path / "sheet.png", 1500, 1050)
        tiles = tiling.plan_tiles(path, str(tmp_path / "tiles"))

        top_row = sorted(
            [t for t in tiles if t.rect[1] == pytest.approx(0.0)],
            key=lambda t: t.rect[0],
        )
        assert len(top_row) >= 2
        assert top_row[0].rect[2] > top_row[1].rect[0], "adjacent tiles must overlap"

    def test_tile_files_are_written(self, tmp_path):
        path = write_image(tmp_path / "sheet.png", 1500, 1050)
        tiles = tiling.plan_tiles(path, str(tmp_path / "tiles"))
        import os

        for tile in tiles:
            assert os.path.exists(tile.path)

    def test_bbox_maps_back_into_whole_image_space(self):
        tile = tiling.Tile(index=0, path="x", rect=(0.5, 0.25, 1.0, 0.75))
        # Centre of the tile is the centre of its rect in the whole image.
        assert tile.to_global([0.0, 0.0, 1.0, 1.0]) == pytest.approx(
            [0.5, 0.25, 1.0, 0.75]
        )
        assert tile.to_global([0.5, 0.5, 0.5, 0.5]) == pytest.approx(
            [0.75, 0.5, 0.75, 0.5]
        )


class TestMerging:
    def _tile(self, index, rect):
        return tiling.Tile(index=index, path=f"t{index}", rect=rect)

    def test_objects_from_all_tiles_are_kept(self):
        merged = tiling.merge_payloads([
            (self._tile(0, (0.0, 0.0, 0.5, 1.0)),
             {"objects": [{"category": "sofa", "bbox": [0.1, 0.1, 0.3, 0.3],
                           "confidence": 0.9}]}),
            (self._tile(1, (0.5, 0.0, 1.0, 1.0)),
             {"objects": [{"category": "bed", "bbox": [0.1, 0.1, 0.3, 0.3],
                           "confidence": 0.9}]}),
        ])
        categories = {o["category"] for o in merged["objects"]}
        assert categories == {"sofa", "bed"}

    def test_boxes_are_remapped_to_whole_image_space(self):
        merged = tiling.merge_payloads([
            (self._tile(1, (0.5, 0.0, 1.0, 1.0)),
             {"objects": [{"category": "bed", "bbox": [0.0, 0.0, 1.0, 1.0],
                           "confidence": 0.9}]}),
        ])
        assert merged["objects"][0]["bbox"] == pytest.approx([0.5, 0.0, 1.0, 1.0])

    def test_a_seam_duplicate_is_merged(self):
        """The same sofa seen in two overlapping tiles is one sofa.

        The sofa occupies global x 0.45-0.55, which falls in the overlap
        between tile 0 (0.0-0.6) and tile 1 (0.4-1.0). Each tile reports it in
        its own local coordinates, and both must map back onto the same box.
        """
        merged = tiling.merge_payloads([
            # (0.45, 0.55) inside tile 0 -> local (0.75, 0.9167)
            (self._tile(0, (0.0, 0.0, 0.6, 1.0)),
             {"objects": [{"category": "sofa", "bbox": [0.75, 0.2, 0.9167, 0.5],
                           "confidence": 0.8}]}),
            # (0.45, 0.55) inside tile 1 -> local (0.0833, 0.25)
            (self._tile(1, (0.4, 0.0, 1.0, 1.0)),
             {"objects": [{"category": "sofa", "bbox": [0.0833, 0.2, 0.25, 0.5],
                           "confidence": 0.9}]}),
        ])
        assert len(merged["objects"]) == 1
        # The more confident copy survives.
        assert merged["objects"][0]["confidence"] == pytest.approx(0.9)
        assert merged["objects"][0]["bbox"][0] == pytest.approx(0.45, abs=0.01)

    def test_genuinely_separate_items_are_not_merged(self):
        """Two sofas near a seam are two sofas, not one seen twice."""
        merged = tiling.merge_payloads([
            (self._tile(0, (0.0, 0.0, 0.6, 1.0)),
             {"objects": [{"category": "sofa", "bbox": [0.1, 0.2, 0.3, 0.5],
                           "confidence": 0.8}]}),
            (self._tile(1, (0.4, 0.0, 1.0, 1.0)),
             {"objects": [{"category": "sofa", "bbox": [0.7, 0.2, 0.9, 0.5],
                           "confidence": 0.9}]}),
        ])
        assert len(merged["objects"]) == 2

    def test_different_categories_are_never_merged(self):
        merged = tiling.merge_payloads([
            (self._tile(0, (0.0, 0.0, 1.0, 1.0)),
             {"objects": [{"category": "sofa", "bbox": [0.1, 0.1, 0.4, 0.4],
                           "confidence": 0.9}]}),
            (self._tile(1, (0.0, 0.0, 1.0, 1.0)),
             {"objects": [{"category": "armchair", "bbox": [0.1, 0.1, 0.4, 0.4],
                           "confidence": 0.9}]}),
        ])
        assert len(merged["objects"]) == 2

    def test_detections_without_a_box_are_dropped(self):
        """A detection with no box can be neither placed nor de-duplicated."""
        merged = tiling.merge_payloads([
            (self._tile(0, (0.0, 0.0, 1.0, 1.0)),
             {"objects": [{"category": "sofa", "confidence": 0.9}]}),
        ])
        assert merged["objects"] == []

    def test_scalar_readings_come_from_the_most_confident_tile(self):
        merged = tiling.merge_payloads([
            (self._tile(0, (0.0, 0.0, 0.5, 1.0)),
             {"room": {"room_type": "kitchen", "confidence": 0.4}}),
            (self._tile(1, (0.5, 0.0, 1.0, 1.0)),
             {"room": {"room_type": "bedroom", "confidence": 0.9}}),
        ])
        assert merged["room"]["room_type"] == "bedroom"

    def test_relationships_are_dropped(self):
        """Tile-local ids are meaningless once tiles are merged."""
        merged = tiling.merge_payloads([
            (self._tile(0, (0.0, 0.0, 1.0, 1.0)),
             {"relationships": [{"subject": "a", "predicate": "faces",
                                 "object": "b"}]}),
        ])
        assert merged["relationships"] == []

    def test_empty_input_is_handled(self):
        assert tiling.merge_payloads([]) == {}

    def test_malformed_payloads_do_not_crash(self):
        merged = tiling.merge_payloads([
            (self._tile(0, (0.0, 0.0, 1.0, 1.0)), None),
            (self._tile(1, (0.0, 0.0, 1.0, 1.0)), {"objects": "not a list"}),
            (self._tile(2, (0.0, 0.0, 1.0, 1.0)),
             {"objects": [{"category": "sofa", "bbox": ["x", "y", 1, 1]}]}),
        ])
        assert merged["objects"] == []
