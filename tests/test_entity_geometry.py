"""entity(get) must report where a thing is, for every type it can be.

Before this, entity(get) returned type/handle/layer for everything except LINE
and CIRCLE. That left "where is this thing" with no cheap answer, so the only
way to find out was to look at a screenshot — which is how a session ends up
paying thousands of tokens for a question worth fifty.

Every assertion here is a question that previously required pixels.
"""

import ezdxf
import pytest

from autocad_mcp.backends.ezdxf_backend import EzdxfBackend


@pytest.fixture
async def backend():
    b = EzdxfBackend()
    await b.initialize()
    return b


async def _get(backend, entity):
    result = await backend.entity_get(entity.dxf.handle)
    assert result.ok, result.error
    return result.payload


class TestEntityGeometry:
    async def test_line(self, backend):
        e = backend._msp.add_line((0, 0), (10, 20))
        info = await _get(backend, e)
        assert info["start"] == [0.0, 0.0]
        assert info["end"] == [10.0, 20.0]

    async def test_circle(self, backend):
        e = backend._msp.add_circle((5, 5), radius=3)
        info = await _get(backend, e)
        assert info["center"] == [5.0, 5.0]
        assert info["radius"] == 3

    async def test_arc(self, backend):
        e = backend._msp.add_arc((1, 2), radius=4, start_angle=30, end_angle=120)
        info = await _get(backend, e)
        assert info["center"] == [1.0, 2.0]
        assert info["radius"] == 4
        assert info["start_angle"] == 30
        assert info["end_angle"] == 120

    async def test_ellipse(self, backend):
        e = backend._msp.add_ellipse((0, 0), major_axis=(5, 0), ratio=0.5)
        info = await _get(backend, e)
        assert info["center"] == [0.0, 0.0]
        assert info["major_axis"] == [5.0, 0.0]
        assert info["ratio"] == 0.5

    async def test_lwpolyline_reports_every_vertex(self, backend):
        pts = [(0, 0), (10, 0), (10, 5), (0, 5)]
        e = backend._msp.add_lwpolyline(pts, close=True)
        info = await _get(backend, e)
        assert info["vertices"] == [list(p) for p in pts]
        assert info["closed"] is True

    async def test_open_lwpolyline_is_not_closed(self, backend):
        e = backend._msp.add_lwpolyline([(0, 0), (1, 1)], close=False)
        assert (await _get(backend, e))["closed"] is False

    async def test_text_contents_are_readable_without_a_screenshot(self, backend):
        """The whole point: read the drawing's text as text."""
        e = backend._msp.add_text("PANEL 44x56", height=2.5)
        e.dxf.insert = (3, 4)
        info = await _get(backend, e)
        assert info["text"] == "PANEL 44x56"
        assert info["insert"] == [3.0, 4.0]
        assert info["height"] == 2.5

    async def test_mtext_contents(self, backend):
        e = backend._msp.add_mtext("DIN RAIL ROW 4")
        e.dxf.insert = (7, 8)
        info = await _get(backend, e)
        assert info["text"] == "DIN RAIL ROW 4"
        assert info["insert"] == [7.0, 8.0]

    async def test_insert_reports_block_and_placement(self, backend):
        block = backend._doc.blocks.new(name="UPS")
        block.add_line((0, 0), (1, 1))
        e = backend._msp.add_blockref("UPS", (12, 34))
        info = await _get(backend, e)
        assert info["name"] == "UPS"
        assert info["insert"] == [12.0, 34.0]

    async def test_point(self, backend):
        e = backend._msp.add_point((9, 9))
        info = await _get(backend, e)
        assert info["location"] == [9.0, 9.0]

    @pytest.mark.parametrize(
        "build",
        [
            lambda msp: msp.add_line((0, 0), (1, 1)),
            lambda msp: msp.add_circle((0, 0), radius=1),
            lambda msp: msp.add_arc((0, 0), radius=1, start_angle=0, end_angle=90),
            lambda msp: msp.add_lwpolyline([(0, 0), (1, 1)]),
            lambda msp: msp.add_text("x"),
            lambda msp: msp.add_point((0, 0)),
        ],
    )
    async def test_payload_is_json_serialisable(self, backend, build):
        """ezdxf returns numpy scalars; json.dumps refuses them.

        An uncoerced vertex list does not merely look odd — it makes the whole
        tool call fail at encode time, which is how a working query turns into a
        screenshot.
        """
        import json

        info = await _get(backend, build(backend._msp))
        json.dumps(info)  # must not raise

    async def test_unknown_type_still_reports_identity(self, backend):
        """Degrade to type/handle/layer rather than erroring."""
        e = backend._msp.add_solid([(0, 0), (1, 0), (1, 1), (0, 1)])
        info = await _get(backend, e)
        assert info["type"] == "SOLID"
        assert "handle" in info and "layer" in info


class TestDrawingExtents:
    async def test_empty_drawing_reports_null_extents(self, backend):
        """Sentinel values must not leak out as if they were real coordinates."""
        info = (await backend.drawing_info()).payload
        assert info["extents"] is None

    async def test_extents_reflect_content(self, backend):
        backend._doc.header["$EXTMIN"] = (0.0, 0.0, 0.0)
        backend._doc.header["$EXTMAX"] = (44.0, 56.0, 0.0)
        info = (await backend.drawing_info()).payload
        assert info["extents"] == {"min": [0.0, 0.0], "max": [44.0, 56.0]}

    async def test_overlap_is_decidable_from_extents_alone(self, backend):
        """Andy's review note 4 as arithmetic instead of a look.

        'The border must never overlap the diagram' is a bounding-box
        intersection test. Two rectangles, four comparisons, zero pixels.
        """
        border = backend._msp.add_lwpolyline(
            [(0, 0), (36, 0), (36, 24), (0, 24)], close=True
        )
        diagram = backend._msp.add_lwpolyline(
            [(30, 20), (50, 20), (50, 40), (30, 40)], close=True
        )

        def bbox(info):
            xs = [v[0] for v in info["vertices"]]
            ys = [v[1] for v in info["vertices"]]
            return min(xs), min(ys), max(xs), max(ys)

        ax1, ay1, ax2, ay2 = bbox(await _get(backend, border))
        bx1, by1, bx2, by2 = bbox(await _get(backend, diagram))
        overlaps = ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2

        assert overlaps is True
