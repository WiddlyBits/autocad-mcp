"""Tests for screenshot providers — no AutoCAD needed."""

import base64
import io

import ezdxf
import pytest

from autocad_mcp.config import SCREENSHOT_MAX_DIMENSION
from autocad_mcp.screenshot import MatplotlibScreenshotProvider, NullScreenshotProvider


def _png_size(b64_data: str) -> tuple[int, int]:
    """Parse width/height from a base64 PNG's IHDR chunk."""
    img_bytes = base64.b64decode(b64_data)
    assert img_bytes[12:16] == b"IHDR"
    return (
        int.from_bytes(img_bytes[16:20], "big"),
        int.from_bytes(img_bytes[20:24], "big"),
    )


# ---------------------------------------------------------------------------
# NullScreenshotProvider
# ---------------------------------------------------------------------------


class TestNullProvider:
    def test_returns_none(self):
        provider = NullScreenshotProvider()
        assert provider.capture() is None

    def test_multiple_calls_return_none(self):
        provider = NullScreenshotProvider()
        for _ in range(5):
            assert provider.capture() is None


# ---------------------------------------------------------------------------
# MatplotlibScreenshotProvider
# ---------------------------------------------------------------------------


class TestMatplotlibProvider:
    def test_no_doc_returns_none(self):
        provider = MatplotlibScreenshotProvider()
        assert provider.capture() is None

    def test_empty_doc_renders(self):
        doc = ezdxf.new("R2013")
        provider = MatplotlibScreenshotProvider(doc)
        result = provider.capture()
        # Empty doc should still render (blank image)
        assert result is not None
        assert result["mime"] == "image/png"
        # Verify it's valid base64
        decoded = base64.b64decode(result["data"])
        assert len(decoded) > 0
        # Verify PNG magic bytes
        assert decoded[:4] == b"\x89PNG"

    def test_doc_with_entities_renders(self):
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))
        msp.add_circle((50, 50), 25)
        msp.add_lwpolyline([(0, 0), (50, 0), (50, 50), (0, 50)], close=True)

        provider = MatplotlibScreenshotProvider(doc)
        result = provider.capture()
        assert result is not None

        decoded = base64.b64decode(result["data"])
        assert decoded[:4] == b"\x89PNG"
        # Image with entities should be larger than empty
        assert len(decoded) > 1000

    def test_doc_setter(self):
        provider = MatplotlibScreenshotProvider()
        assert provider.doc is None

        doc = ezdxf.new("R2013")
        provider.doc = doc
        assert provider.doc is doc

    def test_base64_roundtrip(self):
        """Encode to base64 and decode back, verify PNG structure."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 0))

        provider = MatplotlibScreenshotProvider(doc)
        result = provider.capture()
        assert result is not None
        b64_str = result["data"]

        # Decode
        img_bytes = base64.b64decode(b64_str)

        # Verify PNG signature
        assert img_bytes[:8] == b"\x89PNG\r\n\x1a\n"

        # Re-encode and verify match
        re_encoded = base64.b64encode(img_bytes).decode("ascii")
        assert re_encoded == b64_str

    def test_image_dimensions_reasonable(self):
        """Verify rendered image has reasonable dimensions at full resolution."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))

        provider = MatplotlibScreenshotProvider(doc)
        # max_dimension=None disables the default downscale so this checks the
        # underlying matplotlib render itself, not the resize step.
        result = provider.capture(max_dimension=None)
        width, height = _png_size(result["data"])

        # At 150 DPI with 16x10 inch figsize, expect ~2400x1500
        assert 500 < width < 5000, f"Width {width} out of range"
        assert 500 < height < 3000, f"Height {height} out of range"

    def test_multiple_renders_consistent(self):
        """Rendering the same doc twice should produce same-sized output."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_circle((50, 50), 25)

        provider = MatplotlibScreenshotProvider(doc)
        r1 = provider.capture()
        r2 = provider.capture()

        # Both should succeed
        assert r1 is not None
        assert r2 is not None

        # Sizes should be very close (matplotlib may have minor non-determinism)
        s1 = len(base64.b64decode(r1["data"]))
        s2 = len(base64.b64decode(r2["data"]))
        assert abs(s1 - s2) < s1 * 0.1  # Within 10%

    def test_default_capture_capped_to_max_dimension(self):
        """Default capture() applies the SCREENSHOT_MAX_DIMENSION cap."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))

        provider = MatplotlibScreenshotProvider(doc)
        result = provider.capture()  # default max_dimension = SCREENSHOT_MAX_DIMENSION
        width, height = _png_size(result["data"])
        assert max(width, height) <= SCREENSHOT_MAX_DIMENSION

    def test_aspect_ratio_preserved_when_downscaling(self):
        """Downscaling must not distort the drawing."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))

        provider = MatplotlibScreenshotProvider(doc)
        full_w, full_h = _png_size(provider.capture(max_dimension=None)["data"])
        small_w, small_h = _png_size(provider.capture(max_dimension=400)["data"])

        assert max(small_w, small_h) == 400
        # Rounding to whole pixels allows a small tolerance.
        assert abs((full_w / full_h) - (small_w / small_h)) < 0.01

    def test_never_upscales_below_cap(self):
        """An image already smaller than the cap is returned untouched."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))

        provider = MatplotlibScreenshotProvider(doc)
        # Capture at 400px, then ask for a cap far above that render's size.
        small = provider.capture(max_dimension=400)
        small_w, small_h = _png_size(small["data"])
        assert max(small_w, small_h) == 400

        huge_cap = provider.capture(max_dimension=8192)
        huge_w, huge_h = _png_size(huge_cap["data"])
        # 8192 exceeds the native render, so no resize should have occurred.
        full_w, full_h = _png_size(provider.capture(max_dimension=None)["data"])
        assert (huge_w, huge_h) == (full_w, full_h)

    @pytest.mark.parametrize("bad_dim", [0, -1, -9999])
    def test_non_positive_max_dimension_means_full_resolution(self, bad_dim):
        """Zero/negative caps fall back to full resolution, not a 1px image."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))

        provider = MatplotlibScreenshotProvider(doc)
        result = provider.capture(max_dimension=bad_dim)
        width, height = _png_size(result["data"])
        full_w, full_h = _png_size(provider.capture(max_dimension=None)["data"])
        assert (width, height) == (full_w, full_h)

    def test_quality_param_produces_jpeg(self):
        """Passing quality switches the output to JPEG."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))

        provider = MatplotlibScreenshotProvider(doc)
        result = provider.capture(max_dimension=400, quality=60)
        assert result["mime"] == "image/jpeg"

        img_bytes = base64.b64decode(result["data"])
        assert img_bytes[:3] == b"\xff\xd8\xff"  # JPEG magic bytes

    @pytest.mark.parametrize("bad_quality", [0, -5, 200])
    def test_out_of_range_quality_is_clamped(self, bad_quality):
        """Out-of-range quality clamps into 1-95 instead of raising."""
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))

        provider = MatplotlibScreenshotProvider(doc)
        result = provider.capture(max_dimension=400, quality=bad_quality)
        assert result["mime"] == "image/jpeg"
        assert base64.b64decode(result["data"])[:3] == b"\xff\xd8\xff"

    def test_smaller_max_dimension_reduces_pixel_count(self):
        """A tighter cap monotonically shrinks dimensions — the cost-relevant property.

        Deliberately asserts pixels, not bytes. Token cost is
        ceil(w/28) * ceil(h/28), a function of dimensions only, so this is the
        invariant that matters. Encoded size is NOT guaranteed to shrink: LANCZOS
        resampling turns crisp 1px linework into antialiased gradients that deflate
        compresses poorly, and a downscaled PNG of a CAD screenshot can be larger
        than the original (measured on a real 1928x1218 AutoCAD window: 124 KB
        uncapped vs 253 KB at 1280).
        """
        doc = ezdxf.new("R2013")
        msp = doc.modelspace()
        msp.add_circle((50, 50), 25)
        msp.add_lwpolyline([(0, 0), (50, 0), (50, 50), (0, 50)], close=True)

        provider = MatplotlibScreenshotProvider(doc)
        # Derive caps from the native render — bbox_inches="tight" crops to the
        # drawing extents, so the native size varies with the doc's aspect ratio.
        native = max(_png_size(provider.capture(max_dimension=None)["data"]))
        caps = [native // 2, native // 4, native // 8]

        sizes = [_png_size(provider.capture(max_dimension=c)["data"]) for c in caps]
        for (w, h), cap in zip(sizes, caps):
            assert max(w, h) == cap
        # Strictly decreasing pixel count as the cap tightens.
        counts = [w * h for w, h in sizes]
        assert counts == sorted(counts, reverse=True)
