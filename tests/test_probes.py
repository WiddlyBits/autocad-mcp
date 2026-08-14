"""The probe arithmetic, proven here so the AutoLISP port is a transcription.

execute_lisp runs only on the file_ipc backend, so mcp_probes.lsp cannot be
executed by this suite — or by anything without AutoCAD in front of it. What
can be pinned is the logic it implements. Every test here is a statement about
what mcp_probes.lsp must also do; if one changes, that file changes with it.
"""

import math

import pytest

from autocad_mcp.config import IPC_TIMEOUT
from autocad_mcp.probes import (
    DEFAULT_MAX_ENTITIES,
    EMPTY,
    OCCUPIED,
    PROBE_TIME_BUDGET_MS,
    UNKNOWN,
    BBox,
    Budget,
    arc_bbox,
    bbox_by_layer,
    bbox_is_exact,
    bbox_of,
    bbox_probe,
    bbox_union,
    ellipse_bbox,
    entity_bbox,
    entity_segments,
    extents,
    fmt,
    gap,
    geometry_probe,
    grid_map,
    grid_map_naive,
    intersects_closed,
    intersects_open,
    intersection,
    overlap,
    segment_hits_rect,
    text_bbox,
    text_dump,
)


class TestFormatting:
    def test_six_places(self):
        assert fmt(1.5) == "1.500000"

    def test_negative_zero_never_prints_a_sign(self):
        """The one float that breaks a golden file for no reason.

        -1e-9 and +1e-9 both round to zero but print with different signs, so
        without this the snapshot flips between runs on a value nothing
        touched, and the diff blames the wrong change.
        """
        assert fmt(-0.0) == "0.000000"
        assert fmt(-1e-9) == "0.000000"
        assert fmt(1e-9) == fmt(-1e-9)

    def test_a_real_negative_keeps_its_sign(self):
        assert fmt(-0.5) == "-0.500000"


class TestBBox:
    def test_of_points_empty_is_none(self):
        assert BBox.of_points([]) is None

    def test_union_with_none_is_identity(self):
        b = BBox(0, 0, 1, 1)
        assert b.union(None) == b

    def test_bbox_union_skips_none(self):
        assert bbox_union([None, BBox(0, 0, 1, 1), None]) == BBox(0, 0, 1, 1)

    def test_bbox_union_of_nothing_is_none(self):
        assert bbox_union([None, None]) is None

    def test_fmt_is_four_numbers(self):
        assert BBox(0, 0, 2, 3).fmt() == "0.000000 0.000000 2.000000 3.000000"


class TestIntersection:
    """Two rules, deliberately different, and the difference matters."""

    def test_touching_counts_for_a_crossing_window(self):
        """grid-map's cells share edges; ssget "_C" catches what lands on one."""
        assert intersects_closed(BBox(0, 0, 1, 1), BBox(1, 0, 2, 1)) is True

    def test_touching_is_not_overlapping(self):
        """A border flush against the diagram is flush, not a collision.

        Andy's note 4 with the closed rule would fire on every drawing where
        something is drawn exactly to the edge, which is most of them.
        """
        assert intersects_open(BBox(0, 0, 1, 1), BBox(1, 0, 2, 1)) is False

    def test_real_overlap_satisfies_both(self):
        a, b = BBox(0, 0, 2, 2), BBox(1, 1, 3, 3)
        assert intersects_closed(a, b) and intersects_open(a, b)

    def test_intersection_region(self):
        assert intersection(BBox(0, 0, 2, 2), BBox(1, 1, 3, 3)) == BBox(1, 1, 2, 2)

    def test_no_intersection_region_when_apart(self):
        assert intersection(BBox(0, 0, 1, 1), BBox(5, 5, 6, 6)) is None

    def test_containment_is_overlap(self):
        assert intersects_open(BBox(0, 0, 10, 10), BBox(1, 1, 2, 2)) is True

    def test_gap_is_zero_when_touching(self):
        assert gap(BBox(0, 0, 1, 1), BBox(1, 0, 2, 1)) == 0.0

    def test_gap_is_axis_distance_when_offset_on_one_axis(self):
        assert gap(BBox(0, 0, 1, 1), BBox(4, 0, 5, 1)) == pytest.approx(3.0)

    def test_gap_is_diagonal_when_offset_on_both(self):
        assert gap(BBox(0, 0, 1, 1), BBox(4, 5, 5, 6)) == pytest.approx(5.0)


class TestArcBBox:
    def test_quarter_arc_occupies_one_quadrant(self):
        """center +- r would inflate this fourfold and invent overlaps."""
        b = arc_bbox(0, 0, 10, 0, 90)
        assert (b.xmin, b.ymin, b.xmax, b.ymax) == pytest.approx((0, 0, 10, 10))

    def test_half_arc_reaches_the_top_cardinal(self):
        b = arc_bbox(0, 0, 10, 0, 180)
        assert (b.xmin, b.ymin, b.xmax, b.ymax) == pytest.approx((-10, 0, 10, 10))

    def test_arc_crossing_zero_degrees(self):
        """Sweep 315->45 passes through 0, so the box must reach +r in x."""
        b = arc_bbox(0, 0, 10, 315, 45)
        assert b.xmax == pytest.approx(10)
        assert b.xmin == pytest.approx(10 * math.cos(math.radians(45)))

    def test_equal_angles_mean_a_full_circle(self):
        """A zero-length arc is not what that encodes; treating it as one
        drops the entity from every bbox it belongs to."""
        b = arc_bbox(0, 0, 10, 90, 90)
        assert (b.xmin, b.ymin, b.xmax, b.ymax) == pytest.approx((-10, -10, 10, 10))

    def test_offset_center(self):
        b = arc_bbox(100, 50, 20, 0, 180)
        assert (b.xmin, b.ymin, b.xmax, b.ymax) == pytest.approx((80, 50, 120, 70))


class TestEllipseBBox:
    def test_axis_aligned(self):
        b = ellipse_bbox(0, 0, 10, 0, 0.5)
        assert (b.xmin, b.ymin, b.xmax, b.ymax) == pytest.approx((-10, -5, 10, 5))

    def test_rotated_ninety_degrees_swaps_the_axes(self):
        b = ellipse_bbox(0, 0, 0, 10, 0.5)
        assert (b.xmin, b.ymin, b.xmax, b.ymax) == pytest.approx((-5, -10, 5, 10))

    def test_forty_five_degrees_is_not_the_naive_box(self):
        """hypot(a.cos, b.sin), not a. Getting this wrong is invisible until
        a rotated ellipse reports an overlap it does not have."""
        a = 10.0
        b = ellipse_bbox(0, 0, a * math.cos(math.radians(45)), a * math.sin(math.radians(45)), 0.5)
        expected = math.hypot(a * math.cos(math.radians(45)), 5.0 * math.sin(math.radians(45)))
        assert b.xmax == pytest.approx(expected)
        assert b.xmax < a

    def test_circle_as_ellipse(self):
        b = ellipse_bbox(1, 2, 3, 0, 1.0)
        assert (b.xmin, b.ymin, b.xmax, b.ymax) == pytest.approx((-2, -1, 4, 5))


class TestTextBBox:
    def test_width_is_nominal_advance(self):
        b = text_bbox([0, 0], "ABCD", 2.0)
        assert b.xmax == pytest.approx(4 * 2.0 * 0.6)
        assert (b.ymin, b.ymax) == pytest.approx((0.0, 2.0))

    def test_mtext_hangs_below_its_insert(self):
        """MTEXT's insertion point is the top of the first line, not the
        baseline; anchoring it like TEXT puts the box a line too high."""
        b = text_bbox([0, 10], "X", 2.0, anchor_top=True)
        assert (b.ymin, b.ymax) == pytest.approx((8.0, 10.0))

    def test_rotation_moves_the_box(self):
        b = text_bbox([0, 0], "AB", 2.0, rotation=90)
        assert b.ymax == pytest.approx(2 * 2.0 * 0.6)
        assert b.xmax == pytest.approx(0.0, abs=1e-9)

    def test_empty_text_is_degenerate_in_x(self):
        b = text_bbox([5, 5], "", 2.0)
        assert b.xmin == b.xmax == 5.0


class TestEntityBBox:
    def test_line(self):
        assert entity_bbox({"type": "LINE", "start": [0, 0], "end": [10, 20]}) == BBox(0, 0, 10, 20)

    def test_line_backwards_still_normalises(self):
        assert entity_bbox({"type": "LINE", "start": [10, 20], "end": [0, 0]}) == BBox(0, 0, 10, 20)

    def test_circle(self):
        assert entity_bbox({"type": "CIRCLE", "center": [5, 5], "radius": 3}) == BBox(2, 2, 8, 8)

    def test_polyline_vertices(self):
        info = {"type": "LWPOLYLINE", "vertices": [[0, 0], [10, 0], [10, 5]]}
        assert entity_bbox(info) == BBox(0, 0, 10, 5)

    def test_point_is_degenerate(self):
        assert entity_bbox({"type": "POINT", "location": [9, 9]}) == BBox(9, 9, 9, 9)

    def test_unknown_type_has_no_box(self):
        """Not a zero-size box at the origin — that would drag every union to
        include (0,0) and quietly ruin the extents."""
        assert entity_bbox({"type": "SOLID"}) is None

    def test_insert_resolves_through_the_block(self):
        blocks = {"UPS": [{"type": "LINE", "start": [0, 0], "end": [2, 4]}]}
        info = {"type": "INSERT", "name": "UPS", "insert": [10, 10]}
        assert entity_bbox(info, blocks) == BBox(10, 10, 12, 14)

    def test_insert_applies_scale(self):
        blocks = {"B": [{"type": "LINE", "start": [0, 0], "end": [1, 1]}]}
        info = {"type": "INSERT", "name": "B", "insert": [0, 0], "xscale": 3, "yscale": 2}
        assert entity_bbox(info, blocks) == BBox(0, 0, 3, 2)

    def test_insert_applies_rotation(self):
        blocks = {"B": [{"type": "LINE", "start": [0, 0], "end": [2, 0]}]}
        info = {"type": "INSERT", "name": "B", "insert": [0, 0], "rotation": 90}
        b = entity_bbox(info, blocks)
        assert (b.xmin, b.ymin, b.xmax, b.ymax) == pytest.approx((0, 0, 0, 2), abs=1e-9)

    def test_unknown_block_degrades_to_the_insertion_point(self):
        """Better a point than a crash, and better than pretending to know."""
        info = {"type": "INSERT", "name": "MISSING", "insert": [7, 8]}
        assert entity_bbox(info, {}) == BBox(7, 8, 7, 8)

    def test_self_referencing_block_terminates(self):
        """Invalid DXF, but it exists in the wild. Unbounded recursion inside
        a 10 s IPC budget is not a stack overflow, it is a dead dispatcher."""
        blocks = {"LOOP": [{"type": "INSERT", "name": "LOOP", "insert": [1, 1]}]}
        assert entity_bbox({"type": "INSERT", "name": "LOOP", "insert": [0, 0]}, blocks) is not None

    def test_nested_block_one_level_deep(self):
        blocks = {
            "OUTER": [{"type": "INSERT", "name": "INNER", "insert": [5, 0]}],
            "INNER": [{"type": "LINE", "start": [0, 0], "end": [1, 1]}],
        }
        info = {"type": "INSERT", "name": "OUTER", "insert": [0, 0]}
        assert entity_bbox(info, blocks) == BBox(5, 0, 6, 1)


class TestExactness:
    """An exact box supports "these do not overlap". An approximate one only
    supports "these might", and the caller deserves to know which it has."""

    def test_line_is_exact(self):
        assert bbox_is_exact({"type": "LINE"}) is True

    def test_text_is_never_exact(self):
        assert bbox_is_exact({"type": "TEXT"}) is False

    def test_ellipse_is_not_exact(self):
        assert bbox_is_exact({"type": "ELLIPSE"}) is False

    def test_insert_is_not_exact(self):
        assert bbox_is_exact({"type": "INSERT"}) is False

    def test_plain_polyline_is_exact(self):
        assert bbox_is_exact({"type": "LWPOLYLINE", "has_bulge": False}) is True

    def test_bulged_polyline_is_not(self):
        """The one case that errs toward a *missed* overlap: a bulge arcs
        outside the vertex hull, so the box is too small, not too big."""
        assert bbox_is_exact({"type": "LWPOLYLINE", "has_bulge": True}) is False

    def test_unknown_type_is_not_exact(self):
        assert bbox_is_exact({"type": "SOLID"}) is False


class TestBudget:
    """A probe that blows the IPC deadline does not return late. It returns
    nothing, and Python reports a dead dispatcher."""

    def test_default_time_budget_leaves_room_in_the_ipc_window(self):
        """The remainder covers writing the result file and the poll that
        notices it. A probe that spends the whole window returns nothing."""
        assert PROBE_TIME_BUDGET_MS < IPC_TIMEOUT * 1000
        assert PROBE_TIME_BUDGET_MS >= 1000

    def test_entity_ceiling(self):
        b = Budget(max_entities=3, clock=lambda: 0.0)
        assert [b.spend_entity() for _ in range(5)] == [True, True, True, False, False]
        assert b.reason == "max_entities"

    def test_probe_ceiling(self):
        b = Budget(max_probes=2, clock=lambda: 0.0)
        assert [b.spend_probe() for _ in range(3)] == [True, True, False]
        assert b.reason == "max_probes"

    def test_deadline_stops_a_scan_that_is_within_its_count(self):
        """Count and time are separate ceilings: 500 slow entities can blow the
        deadline long before the 20 000-entity cap is anywhere in sight."""
        ticks = iter([0.0] + [i * 1000.0 for i in range(1, 20)])
        b = Budget(max_entities=10_000, time_ms=3000, clock=lambda: next(ticks))
        spent = 0
        while b.spend_entity():
            spent += 1
        assert b.reason == "time"
        assert spent < 10_000

    def test_restart_clears_the_reason(self):
        b = Budget(max_probes=1, clock=lambda: 0.0)
        b.spend_probe()
        b.spend_probe()
        assert b.exhausted
        assert b.restart().spend_probe() is True

    def test_default_entity_ceiling_covers_a_thirty_thousand_entity_drawing(self):
        """20k is the documented ceiling, so a 30k drawing truncates rather
        than hangs. The number is a decision, not an accident."""
        assert DEFAULT_MAX_ENTITIES == 20000


ENTITIES = [
    {"type": "LWPOLYLINE", "layer": "BORDER", "vertices": [[0, 0], [200, 0], [200, 100], [0, 100]]},
    {"type": "LINE", "layer": "SHAPES", "start": [10, 10], "end": [50, 50]},
    {"type": "CIRCLE", "layer": "SHAPES", "center": [100, 50], "radius": 30},
    {"type": "TEXT", "layer": "ANNOTATION", "text": "TITLE", "insert": [10, 90], "height": 5},
    {"type": "TEXT", "layer": "ANNOTATION", "text": "SUB", "insert": [10, 80], "height": 5},
]


class TestExtents:
    def test_union_of_everything(self):
        assert extents(ENTITIES)["extents"] == [0.0, 0.0, 200.0, 100.0]

    def test_empty_drawing(self):
        result = extents([])
        assert result["extents"] is None
        assert result["truncated"] is False

    def test_entities_without_geometry_do_not_drag_the_box_to_the_origin(self):
        result = extents([{"type": "SOLID", "layer": "0"}, ENTITIES[2]])
        assert result["extents"] == [70.0, 20.0, 130.0, 80.0]

    def test_truncation_is_reported_not_hidden(self):
        result = extents(ENTITIES, budget=Budget(max_entities=2, clock=lambda: 0.0))
        assert result["truncated"] is True
        assert result["truncated_reason"] == "max_entities"
        assert result["scanned"] == 2
        assert result["entities"] == 5


class TestBBoxOf:
    def test_union_of_named_entities(self):
        assert bbox_of([ENTITIES[1], ENTITIES[2]])["bbox"] == [10.0, 10.0, 130.0, 80.0]

    def test_reports_exactness_of_the_whole_set(self):
        assert bbox_of([ENTITIES[1]])["exact"] is True
        assert bbox_of([ENTITIES[1], ENTITIES[3]])["exact"] is False

    def test_nothing_selected(self):
        assert bbox_of([])["bbox"] is None


class TestBBoxByLayer:
    def test_groups_and_unions(self):
        result = bbox_by_layer(ENTITIES)["layers"]
        assert result["BORDER"]["bbox"] == [0.0, 0.0, 200.0, 100.0]
        assert result["SHAPES"]["bbox"] == [10.0, 10.0, 130.0, 80.0]
        assert result["SHAPES"]["count"] == 2

    def test_layers_are_sorted_so_the_output_diffs(self):
        assert list(bbox_by_layer(ENTITIES)["layers"]) == ["ANNOTATION", "BORDER", "SHAPES"]

    def test_layer_filter_narrows_the_scan(self):
        result = bbox_by_layer(ENTITIES, layers=["SHAPES"])
        assert list(result["layers"]) == ["SHAPES"]
        assert result["scanned"] == 2

    def test_a_text_only_layer_is_marked_inexact(self):
        assert bbox_by_layer(ENTITIES)["layers"]["ANNOTATION"]["exact"] is False

    def test_truncation_reports_what_it_managed(self):
        result = bbox_by_layer(ENTITIES, budget=Budget(max_entities=1, clock=lambda: 0.0))
        assert result["truncated"] is True
        assert result["scanned"] == 1
        assert list(result["layers"]) == ["BORDER"]

    def test_layer_with_no_measurable_geometry_still_counted(self):
        result = bbox_by_layer([{"type": "SOLID", "layer": "HATCHY"}])
        assert result["layers"]["HATCHY"] == {"count": 1, "bbox": None, "exact": False}


class TestTextDump:
    def test_reads_the_drawing_as_text(self):
        result = text_dump(ENTITIES)
        assert [t["text"] for t in result["text"]] == ["TITLE", "SUB"]

    def test_ordered_top_down_then_left_to_right(self):
        entities = [
            {"type": "TEXT", "layer": "A", "text": "bottom", "insert": [0, 0], "height": 1},
            {"type": "TEXT", "layer": "A", "text": "top-right", "insert": [50, 10], "height": 1},
            {"type": "TEXT", "layer": "A", "text": "top-left", "insert": [0, 10], "height": 1},
        ]
        assert [t["text"] for t in text_dump(entities)["text"]] == ["top-left", "top-right", "bottom"]

    def test_non_text_entities_are_skipped_without_being_charged(self):
        result = text_dump(ENTITIES)
        assert result["scanned"] == 2

    def test_layer_filter(self):
        assert text_dump(ENTITIES, layers=["NOPE"])["count"] == 0

    def test_identical_labels_at_one_point_are_both_reported(self):
        """The invariant the AutoLISP port has to work to preserve: vl-sort
        drops elements its predicate calls equal, so two identical labels would
        silently become one and the dump would disagree with entity(count)."""
        dup = {"type": "TEXT", "layer": "A", "text": "N/A", "insert": [1, 1], "height": 1}
        assert text_dump([dict(dup), dict(dup)])["count"] == 2

    def test_mtext_is_included(self):
        entities = [{"type": "MTEXT", "layer": "A", "text": "M", "insert": [0, 0], "height": 1}]
        assert text_dump(entities)["count"] == 1


class TestOverlap:
    def test_andys_note_four_as_a_boolean(self):
        """"The border must not overlap the diagram" — four comparisons."""
        border = BBox(0, 0, 36, 24)
        diagram = BBox(30, 20, 50, 40)
        assert overlap(border, diagram)["overlaps"] is True

    def test_reports_the_offending_region(self):
        assert overlap(BBox(0, 0, 36, 24), BBox(30, 20, 50, 40))["intersection"] == [30, 20, 36, 24]

    def test_flush_is_not_an_overlap(self):
        assert overlap(BBox(0, 0, 10, 10), BBox(10, 0, 20, 10))["overlaps"] is False

    def test_clearance_is_the_real_drafting_requirement(self):
        """Not colliding is not the same as leaving room."""
        a, b = BBox(0, 0, 10, 10), BBox(11, 0, 20, 10)
        assert overlap(a, b, clearance=2.0)["clears"] is False
        assert overlap(a, b, clearance=1.0)["clears"] is True

    def test_gap_is_reported_so_the_margin_is_visible(self):
        assert overlap(BBox(0, 0, 10, 10), BBox(14, 0, 20, 10))["gap"] == pytest.approx(4.0)

    def test_missing_side_is_not_a_pass(self):
        """An empty layer must not read as "no overlap, all good"."""
        result = overlap(None, BBox(0, 0, 1, 1))
        assert result["overlaps"] is False
        assert result["clears"] is None


REGION = BBox(0, 0, 100, 100)


def _boxes(*specs):
    return [BBox(*s) for s in specs]


class TestCrossingWindow:
    """`ssget "_C"` selects what crosses the window or sits inside it.

    Modelling this instead of testing bounding boxes is what makes the grid
    worth reading. A 200x100 sheet border has a bbox covering the whole sheet:
    with a bbox probe every cell reads occupied and the map says nothing.
    """

    CELL = BBox(40, 40, 50, 50)

    def test_segment_through_the_window(self):
        assert segment_hits_rect((0, 45), (100, 45), self.CELL) is True

    def test_segment_ending_inside(self):
        assert segment_hits_rect((0, 45), (45, 45), self.CELL) is True

    def test_segment_entirely_inside(self):
        assert segment_hits_rect((42, 42), (48, 48), self.CELL) is True

    def test_segment_that_misses(self):
        assert segment_hits_rect((0, 0), (10, 10), self.CELL) is False

    def test_segment_passing_outside_a_corner(self):
        """The case a bbox test gets wrong: the segment's box overlaps the
        cell, the segment does not."""
        assert segment_hits_rect((30, 55), (55, 30), self.CELL) is True
        assert segment_hits_rect((20, 55), (55, 20), self.CELL) is False

    def test_segment_lying_on_an_edge_counts(self):
        assert segment_hits_rect((0, 40), (100, 40), self.CELL) is True

    def test_degenerate_segment_is_a_point_test(self):
        assert segment_hits_rect((45, 45), (45, 45), self.CELL) is True
        assert segment_hits_rect((5, 5), (5, 5), self.CELL) is False

    def test_a_hollow_border_leaves_its_middle_empty(self):
        """The bug this whole probe exists to avoid, stated directly."""
        border = {
            "type": "LWPOLYLINE", "layer": "BORDER", "closed": True,
            "vertices": [[0, 0], [100, 0], [100, 100], [0, 100]],
        }
        probe = geometry_probe([border])
        assert probe(40, 40, 50, 50) is False
        assert probe(0, 0, 10, 10) is True

    def test_bbox_probe_gets_that_wrong_on_purpose(self):
        """Kept as a conservative superset, and documented as not being this."""
        assert bbox_probe([BBox(0, 0, 100, 100)])(40, 40, 50, 50) is True

    def test_a_circle_reads_as_a_ring_not_a_disc(self):
        circle = {"type": "CIRCLE", "layer": "S", "center": [50, 50], "radius": 40}
        probe = geometry_probe([circle])
        assert probe(45, 45, 55, 55) is False
        assert probe(5, 45, 15, 55) is True

    def test_text_is_selected_by_its_extents(self):
        text = {"type": "TEXT", "layer": "A", "text": "WIDE", "insert": [0, 0], "height": 10}
        assert geometry_probe([text])(1, 1, 2, 2) is True

    def test_insert_is_probed_through_its_block_geometry(self):
        blocks = {"B": [{"type": "LWPOLYLINE", "closed": True,
                         "vertices": [[0, 0], [20, 0], [20, 20], [0, 20]]}]}
        probe = geometry_probe([{"type": "INSERT", "name": "B", "insert": [50, 50]}], blocks)
        assert probe(58, 58, 62, 62) is False
        assert probe(48, 48, 52, 52) is True

    def test_entity_with_no_geometry_is_never_selected(self):
        assert geometry_probe([{"type": "SOLID", "layer": "0"}])(0, 0, 100, 100) is False

    def test_open_polyline_has_no_closing_segment(self):
        info = {"type": "LWPOLYLINE", "closed": False,
                "vertices": [[0, 0], [100, 0], [100, 100], [0, 100]]}
        assert len(entity_segments(info)) == 3
        info_closed = dict(info, closed=True)
        assert len(entity_segments(info_closed)) == 4


class TestGridMap:
    """Strip probing must be indistinguishable from probing every cell."""

    @pytest.mark.parametrize(
        "boxes",
        [
            [],
            _boxes((0, 0, 100, 100)),
            _boxes((0, 0, 10, 10)),
            _boxes((45, 45, 55, 55)),
            _boxes((0, 90, 100, 100), (0, 0, 100, 10)),
            _boxes((10, 10, 20, 20), (80, 80, 90, 90), (40, 0, 60, 100)),
            _boxes((0, 0, 0, 0)),
            _boxes((99.9, 99.9, 100, 100)),
        ],
    )
    @pytest.mark.parametrize("cols,rows", [(8, 4), (16, 8), (24, 12), (3, 3)])
    def test_matches_the_naive_oracle(self, boxes, cols, rows):
        probe = bbox_probe(boxes)
        fast = grid_map(probe, REGION, cols, rows, budget=_unlimited())
        slow = grid_map_naive(probe, REGION, cols, rows)
        assert fast["grid"] == slow["grid"]

    @pytest.mark.parametrize(
        "entities",
        [
            [],
            [{"type": "LWPOLYLINE", "closed": True,
              "vertices": [[0, 0], [100, 0], [100, 100], [0, 100]]}],
            [{"type": "CIRCLE", "center": [50, 50], "radius": 40}],
            [{"type": "LINE", "start": [0, 0], "end": [100, 100]}],
            [{"type": "TEXT", "text": "TITLE BLOCK", "insert": [5, 5], "height": 4},
             {"type": "ARC", "center": [80, 80], "radius": 15,
              "start_angle": 0, "end_angle": 270}],
        ],
    )
    @pytest.mark.parametrize("cols,rows", [(8, 4), (24, 12)])
    def test_oracle_holds_for_the_probe_actually_used(self, entities, cols, rows):
        """Strip probing must be invisible with geometry_probe too — that is
        the one the snapshot builds its grid from."""
        probe = geometry_probe(entities)
        fast = grid_map(probe, REGION, cols, rows, budget=_unlimited())
        slow = grid_map_naive(probe, REGION, cols, rows)
        assert fast["grid"] == slow["grid"]

    def test_empty_rows_cost_one_probe_instead_of_cols(self):
        """The reason strip probing exists. A drawing is mostly whitespace, and
        24x12 naive is 288 ssget calls the 10 s IPC budget cannot absorb."""
        probe = bbox_probe(_boxes((0, 0, 100, 8)))
        fast = grid_map(probe, REGION, 24, 12, budget=_unlimited())
        slow = grid_map_naive(probe, REGION, 24, 12)
        assert fast["probes"] < slow["probes"] / 4

    def test_an_empty_drawing_costs_one_probe_per_row(self):
        result = grid_map(bbox_probe([]), REGION, 24, 12, budget=_unlimited())
        assert result["probes"] == 12
        assert result["grid"] == [EMPTY * 24] * 12

    def test_worst_case_is_bounded_by_the_probe_budget(self):
        """Every row occupied is rows + rows*cols. The budget is what keeps
        that from becoming a timeout on a 30k-entity drawing."""
        probe = bbox_probe(_boxes((0, 0, 100, 100)))
        result = grid_map(probe, REGION, 24, 12, budget=Budget(max_probes=50, clock=lambda: 0.0))
        assert result["probes"] == 50
        assert result["truncated"] is True
        assert result["truncated_reason"] == "max_probes"

    def test_unprobed_cells_are_unknown_not_empty(self):
        """Reporting unprobed space as empty is how a probe lies. A caller
        reading '.' concludes there is nothing there."""
        probe = bbox_probe(_boxes((0, 0, 100, 100)))
        result = grid_map(probe, REGION, 8, 4, budget=Budget(max_probes=3, clock=lambda: 0.0))
        assert UNKNOWN in "".join(result["grid"])
        assert result["grid"][-1] == UNKNOWN * 8

    def test_row_zero_is_the_top_so_the_grid_reads_like_the_drawing(self):
        probe = bbox_probe(_boxes((0, 90, 100, 100)))
        grid = grid_map(probe, REGION, 4, 4, budget=_unlimited())["grid"]
        assert grid[0] == OCCUPIED * 4
        assert grid[-1] == EMPTY * 4

    def test_deadline_truncates_mid_grid(self):
        ticks = iter([0.0] + [i * 2000.0 for i in range(1, 100)])
        probe = bbox_probe(_boxes((0, 0, 100, 100)))
        result = grid_map(probe, REGION, 8, 8, budget=Budget(time_ms=3000, clock=lambda: next(ticks)))
        assert result["truncated_reason"] == "time"

    def test_a_shape_lands_where_it_should(self):
        probe = bbox_probe(_boxes((1, 1, 24, 24)))
        grid = grid_map(probe, REGION, 4, 4, budget=_unlimited())["grid"]
        assert grid == [EMPTY * 4, EMPTY * 4, EMPTY * 4, OCCUPIED + EMPTY * 3]

    def test_a_shape_on_a_cell_boundary_lights_both_cells(self):
        """Crossing-window semantics, not a rounding bug. ssget "_C" does the
        same, so the grid and AutoCAD agree about the edge case."""
        probe = bbox_probe(_boxes((1, 1, 25, 24)))
        grid = grid_map(probe, REGION, 4, 4, budget=_unlimited())["grid"]
        assert grid[-1] == OCCUPIED * 2 + EMPTY * 2


def _unlimited():
    return Budget(max_probes=10**6, max_entities=10**6, clock=lambda: 0.0)
