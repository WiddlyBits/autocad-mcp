"""Region cropping, save_to, cost reporting, and the no-image control arm.

These cover the levers that make a screenshot affordable, in the order they
matter: crop before downscale (region), keep the image out of context entirely
(save_to), and be able to run with images suppressed at all (ONLY_TEXT).
"""

import base64
import json

import ezdxf
import pytest
from PIL import Image

from autocad_mcp.config import SCREENSHOT_MAX_DIMENSION_RANGE
from autocad_mcp.screenshot import (
    MatplotlibScreenshotProvider,
    _resize_and_encode,
    crop_to_region,
    visual_tokens,
)


def _png_size(b64_data: str) -> tuple[int, int]:
    """Parse width/height from a base64 PNG's IHDR chunk."""
    img_bytes = base64.b64decode(b64_data)
    assert img_bytes[12:16] == b"IHDR"
    return (
        int.from_bytes(img_bytes[16:20], "big"),
        int.from_bytes(img_bytes[20:24], "big"),
    )


def _doc_with_content():
    doc = ezdxf.new("R2013")
    msp = doc.modelspace()
    for i in range(10):
        msp.add_line((0, i * 10), (100, i * 10))
    return doc


# ---------------------------------------------------------------------------
# visual_tokens — the cost model everything else is justified against
# ---------------------------------------------------------------------------


class TestVisualTokens:
    @pytest.mark.parametrize(
        "width, height, expected",
        [
            (1928, 1218, 3036),  # measured: Gianni's ThinkPad, uncapped
            (1280, 809, 1334),  # the same window downscaled to the 1280 default
            (500, 400, 270),  # a region crop of a detail area
            (28, 28, 1),  # exactly one patch
            (29, 29, 4),  # one pixel over rounds up on both axes
        ],
    )
    def test_known_sizes(self, width, height, expected):
        assert visual_tokens(width, height) == expected

    def test_region_crop_beats_downscaled_full_window(self):
        """The claim the region parameter exists to make."""
        full_window_downscaled = visual_tokens(1280, 809)
        detail_crop_native = visual_tokens(500, 400)
        assert detail_crop_native * 4 < full_window_downscaled


# ---------------------------------------------------------------------------
# crop_to_region
# ---------------------------------------------------------------------------


class TestCropToRegion:
    def test_crops_to_exact_box(self):
        img = Image.new("RGB", (800, 600))
        assert crop_to_region(img, (100, 50, 300, 250)).size == (200, 200)

    def test_clamps_to_image_bounds(self):
        img = Image.new("RGB", (800, 600))
        # Overhanging box is clamped, not rejected — asking for more than exists
        # is a harmless overshoot.
        assert crop_to_region(img, (600, 400, 5000, 5000)).size == (200, 200)

    def test_negative_origin_is_clamped(self):
        img = Image.new("RGB", (800, 600))
        assert crop_to_region(img, (-50, -50, 100, 100)).size == (100, 100)

    @pytest.mark.parametrize("region", [(100, 100, 100, 200), (100, 100, 200, 100)])
    def test_empty_region_raises(self, region):
        img = Image.new("RGB", (800, 600))
        with pytest.raises(ValueError, match="empty"):
            crop_to_region(img, region)

    def test_fully_outside_region_raises_rather_than_falling_back(self):
        """A silent full-frame fallback would charge full price for a cheap ask."""
        img = Image.new("RGB", (800, 600))
        with pytest.raises(ValueError, match="outside"):
            crop_to_region(img, (900, 700, 1000, 800))


# ---------------------------------------------------------------------------
# _resize_and_encode — crop must happen before the downscale
# ---------------------------------------------------------------------------


class TestCropBeforeResize:
    def test_crop_precedes_downscale(self):
        """A 400px crop under a 1000px cap must not be upscaled or rescaled.

        If the order were reversed the image would be downscaled to 1000 first
        and the crop would come back proportionally smaller — losing exactly the
        detail the crop was asked for.
        """
        img = Image.new("RGB", (4000, 3000))
        out = _resize_and_encode(img, max_dimension=1000, quality=None, region=(0, 0, 400, 400))
        assert _png_size(out["data"]) == (400, 400)

    def test_cap_still_applies_to_an_oversized_crop(self):
        img = Image.new("RGB", (4000, 3000))
        out = _resize_and_encode(img, max_dimension=500, quality=None, region=(0, 0, 2000, 1000))
        assert _png_size(out["data"]) == (500, 250)

    def test_reports_encoded_dimensions_and_cost(self):
        img = Image.new("RGB", (4000, 3000))
        out = _resize_and_encode(img, max_dimension=1000, quality=None, region=(0, 0, 400, 400))
        assert (out["width"], out["height"]) == (400, 400)
        assert out["est_tokens"] == visual_tokens(400, 400)

    def test_no_region_is_unchanged_behaviour(self):
        img = Image.new("RGB", (800, 600))
        out = _resize_and_encode(img, max_dimension=None, quality=None)
        assert _png_size(out["data"]) == (800, 600)


class TestProviderRegion:
    def test_matplotlib_provider_accepts_region(self):
        provider = MatplotlibScreenshotProvider(_doc_with_content())
        full = provider.capture(max_dimension=None)
        cropped = provider.capture(max_dimension=None, region=(0, 0, 200, 150))

        assert cropped["width"] == 200
        assert cropped["height"] == 150
        assert cropped["est_tokens"] < full["est_tokens"]

    def test_bad_region_propagates_as_valueerror(self):
        """Must not be swallowed by the provider's generic failure handler."""
        provider = MatplotlibScreenshotProvider(_doc_with_content())
        with pytest.raises(ValueError):
            provider.capture(region=(10, 10, 5, 5))


# ---------------------------------------------------------------------------
# The view tool: save_to, ONLY_TEXT, clamping
# ---------------------------------------------------------------------------


def _text_of(result):
    """Unwrap a tool result into its JSON text block."""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list):
        blocks = [b for b in result if getattr(b, "type", None) == "text"]
        return json.loads(blocks[0].text)
    return json.loads(result)


def _image_blocks(result):
    if isinstance(result, tuple):
        result = result[0]
    if not isinstance(result, list):
        return []
    return [b for b in result if getattr(b, "type", None) == "image"]


class TestViewScreenshotTool:
    async def test_attaches_image_and_reports_cost(self):
        from autocad_mcp.server import mcp

        result = await mcp.call_tool("view", {"operation": "get_screenshot"})
        payload = _text_of(result)

        assert payload["screenshot"] == "attached"
        assert payload["est_tokens"] > 0
        assert len(_image_blocks(result)) == 1

    async def test_save_to_writes_file_and_attaches_nothing(self, tmp_path):
        from autocad_mcp.server import mcp

        target = tmp_path / "nested" / "shot.png"
        result = await mcp.call_tool(
            "view", {"operation": "get_screenshot", "save_to": str(target)}
        )
        payload = _text_of(result)

        assert payload["screenshot"] == "saved"
        assert target.exists()
        assert target.read_bytes()[:4] == b"\x89PNG"
        # The whole point: no pixels enter context.
        assert _image_blocks(result) == []
        assert payload["est_tokens"] > 0

    async def test_only_text_suppresses_the_image_but_still_reports_cost(self, monkeypatch):
        """view(get_screenshot) must honour the kill switch, not just include_screenshot."""
        from autocad_mcp import config
        from autocad_mcp.server import mcp

        monkeypatch.setattr(config, "ONLY_TEXT_FEEDBACK", True)
        result = await mcp.call_tool("view", {"operation": "get_screenshot"})
        payload = _text_of(result)

        assert payload["screenshot"] == "suppressed"
        assert _image_blocks(result) == []
        assert payload["est_tokens"] > 0

    async def test_save_to_still_works_under_only_text(self, monkeypatch, tmp_path):
        """Writing to disk costs no context, so the kill switch should not block it."""
        from autocad_mcp import config
        from autocad_mcp.server import mcp

        monkeypatch.setattr(config, "ONLY_TEXT_FEEDBACK", True)
        target = tmp_path / "shot.png"
        result = await mcp.call_tool(
            "view", {"operation": "get_screenshot", "save_to": str(target)}
        )

        assert _text_of(result)["screenshot"] == "saved"
        assert target.exists()

    async def test_oversized_max_dimension_is_clamped(self):
        """An unclamped argument buys resolution the vision API discards."""
        from autocad_mcp.server import mcp

        result = await mcp.call_tool(
            "view", {"operation": "get_screenshot", "max_dimension": 8000}
        )
        payload = _text_of(result)

        assert max(payload["width"], payload["height"]) <= SCREENSHOT_MAX_DIMENSION_RANGE[1]

    async def test_region_reduces_cost(self):
        from autocad_mcp.server import mcp

        full = _text_of(await mcp.call_tool("view", {"operation": "get_screenshot"}))
        cropped = _text_of(
            await mcp.call_tool(
                "view", {"operation": "get_screenshot", "region": [0, 0, 300, 200]}
            )
        )

        assert (cropped["width"], cropped["height"]) == (300, 200)
        assert cropped["est_tokens"] < full["est_tokens"]

    @pytest.mark.parametrize("region", [[1, 2, 3], [1, 2, 3, 4, 5]])
    async def test_malformed_region_is_rejected_clearly(self, region):
        from autocad_mcp.server import mcp

        result = await mcp.call_tool(
            "view", {"operation": "get_screenshot", "region": region}
        )
        assert "region must be 4 integers" in _text_of(result)["error"]

    async def test_degenerate_region_reports_the_reason(self):
        from autocad_mcp.server import mcp

        result = await mcp.call_tool(
            "view", {"operation": "get_screenshot", "region": [900, 900, 100, 100]}
        )
        payload = _text_of(result)
        assert payload.get("ok") is False
        assert "empty" in payload["error"]
