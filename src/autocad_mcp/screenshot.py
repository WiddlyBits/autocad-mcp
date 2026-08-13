"""Screenshot providers: Win32 window capture and matplotlib DXF render."""

from __future__ import annotations

import base64
import io
import sys
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

from autocad_mcp.config import SCREENSHOT_MAX_DIMENSION

if TYPE_CHECKING:
    import ezdxf
    from PIL.Image import Image as PILImage

log = structlog.get_logger()

# JPEG quality above ~95 costs bytes without visible gain; below 1 is invalid.
_JPEG_QUALITY_RANGE = (1, 95)


def _resize_and_encode(
    img: PILImage,
    max_dimension: int | None,
    quality: int | None,
) -> dict[str, str]:
    """Downscale img to fit max_dimension and encode it for transport.

    max_dimension caps the longest side in pixels; None or any non-positive value
    means full resolution. Images already within the cap are never upscaled.
    This is the parameter that controls an LLM's token cost for the image, which
    is ceil(w/28) * ceil(h/28) visual tokens — a function of dimensions only.

    quality (clamped to 1-95) switches encoding to JPEG; None keeps lossless PNG.
    Note that quality shrinks the transferred/stored payload but does NOT reduce
    token cost, since the token count ignores encoded size. Downscaling a screenshot
    can even make a PNG *larger* (resampling antialiases crisp linework into
    gradients that deflate compresses poorly) without changing what it costs to
    read — so treat quality as a bytes-on-disk lever, not a usage lever.

    Returns {"data": base64 payload, "mime": mime type}.
    """
    from PIL import Image

    longest = max(img.size)
    if max_dimension and max_dimension > 0 and longest > max_dimension:
        scale = max_dimension / longest
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    if quality is None:
        img.save(buf, format="PNG")
        mime = "image/png"
    else:
        low, high = _JPEG_QUALITY_RANGE
        if img.mode != "RGB":
            img = img.convert("RGB")  # JPEG has no alpha channel
        img.save(buf, format="JPEG", quality=max(low, min(high, quality)))
        mime = "image/jpeg"

    return {"data": base64.b64encode(buf.getvalue()).decode("ascii"), "mime": mime}


class ScreenshotProvider(ABC):
    """Abstract screenshot provider."""

    @abstractmethod
    def capture(
        self, max_dimension: int | None = SCREENSHOT_MAX_DIMENSION, quality: int | None = None
    ) -> dict[str, str] | None:
        """Return {"data": base64-encoded image, "mime": mime type}, or None if capture fails.

        max_dimension caps the longest side in pixels (None = full resolution).
        quality (1-95), if given, encodes as JPEG instead of PNG for a smaller payload.
        """


class NullScreenshotProvider(ScreenshotProvider):
    """No-op provider — always returns None."""

    def capture(
        self, max_dimension: int | None = SCREENSHOT_MAX_DIMENSION, quality: int | None = None
    ) -> dict[str, str] | None:
        return None


class MatplotlibScreenshotProvider(ScreenshotProvider):
    """Render an ezdxf document to PNG via matplotlib."""

    def __init__(self, doc: ezdxf.document.Drawing | None = None):
        self._doc = doc

    @property
    def doc(self) -> ezdxf.document.Drawing | None:
        return self._doc

    @doc.setter
    def doc(self, value: ezdxf.document.Drawing):
        self._doc = value

    def capture(
        self, max_dimension: int | None = SCREENSHOT_MAX_DIMENSION, quality: int | None = None
    ) -> dict[str, str] | None:
        if self._doc is None:
            return None
        try:
            import matplotlib
            from PIL import Image

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from ezdxf.addons.drawing import Frontend, RenderContext
            from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

            fig, ax = plt.subplots(figsize=(16, 10), dpi=150)
            ax.set_aspect("equal")

            ctx = RenderContext(self._doc)
            out = MatplotlibBackend(ax)
            Frontend(ctx, out).draw_layout(self._doc.modelspace())

            buf = io.BytesIO()
            fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.1)
            plt.close(fig)
            buf.seek(0)
            img = Image.open(buf)
            img.load()
            return _resize_and_encode(img, max_dimension, quality)
        except Exception as e:
            log.warning("matplotlib_screenshot_failed", error=str(e))
            return None


class Win32ScreenshotProvider(ScreenshotProvider):
    """Capture AutoCAD window via Win32 PrintWindow."""

    _dpi_awareness_initialized = False

    def __init__(self, hwnd: int):
        self._hwnd = hwnd

    @classmethod
    def _ensure_dpi_awareness(cls) -> None:
        if cls._dpi_awareness_initialized:
            return

        import ctypes

        user32 = ctypes.windll.user32

        # Best effort: prefer per-monitor DPI awareness, then fall back.
        try:
            if hasattr(user32, "SetProcessDpiAwarenessContext"):
                dpi_aware_v2 = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
                if user32.SetProcessDpiAwarenessContext(dpi_aware_v2):
                    cls._dpi_awareness_initialized = True
                    return
        except Exception:
            pass

        try:
            shcore = ctypes.windll.shcore
            PROCESS_PER_MONITOR_DPI_AWARE = 2
            if shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
                cls._dpi_awareness_initialized = True
                return
        except Exception:
            pass

        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

        cls._dpi_awareness_initialized = True

    def _get_capture_rect(self) -> tuple[int, int, int, int]:
        import win32gui

        placement = win32gui.GetWindowPlacement(self._hwnd)
        normal_rect = placement[4]

        if win32gui.IsIconic(self._hwnd):
            width = normal_rect[2] - normal_rect[0]
            height = normal_rect[3] - normal_rect[1]
            if width > 0 and height > 0:
                return normal_rect

        return win32gui.GetWindowRect(self._hwnd)

    def capture(
        self, max_dimension: int | None = SCREENSHOT_MAX_DIMENSION, quality: int | None = None
    ) -> dict[str, str] | None:
        if sys.platform != "win32":
            return None
        try:
            import ctypes

            import win32gui
            import win32ui
            from PIL import Image

            self._ensure_dpi_awareness()

            rect = self._get_capture_rect()
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]

            if width <= 0 or height <= 0:
                log.warning("win32_screenshot_bad_dimensions", width=width, height=height)
                return None

            hwnd_dc = None
            mfc_dc = None
            save_dc = None
            bitmap = None

            try:
                hwnd_dc = win32gui.GetWindowDC(self._hwnd)
                mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
                save_dc = mfc_dc.CreateCompatibleDC()

                bitmap = win32ui.CreateBitmap()
                bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
                save_dc.SelectObject(bitmap)

                PW_RENDERFULLCONTENT = 0x00000002
                result = ctypes.windll.user32.PrintWindow(
                    self._hwnd,
                    save_dc.GetSafeHdc(),
                    PW_RENDERFULLCONTENT,
                )
                if result != 1:
                    log.warning("win32_printwindow_failed", flag=PW_RENDERFULLCONTENT)
                    return None

                bmpinfo = bitmap.GetInfo()
                bmpstr = bitmap.GetBitmapBits(True)

                img = Image.frombuffer(
                    "RGB",
                    (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
                    bmpstr,
                    "raw",
                    "BGRX",
                    0,
                    1,
                )
            finally:
                if bitmap is not None:
                    win32gui.DeleteObject(bitmap.GetHandle())
                if save_dc is not None:
                    save_dc.DeleteDC()
                if mfc_dc is not None:
                    mfc_dc.DeleteDC()
                if hwnd_dc is not None:
                    win32gui.ReleaseDC(self._hwnd, hwnd_dc)

            # Resize/encode is pure CPU work — do it after the GDI handles are
            # released rather than holding a window DC for the duration.
            return _resize_and_encode(img, max_dimension, quality)

        except Exception as e:
            log.warning("win32_screenshot_failed", error=str(e))
            return None
