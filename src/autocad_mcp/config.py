"""Backend detection and environment configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import structlog

log = structlog.get_logger()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    """Read an int env var clamped to [low, high], falling back to default if unset/invalid.

    Malformed values fall back rather than raising — a bad env var should not stop
    the MCP server from starting.
    """
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        log.warning("invalid_env_int", var=name, fallback=default)
        return default


# Paths
LISP_DIR = Path(__file__).resolve().parent.parent.parent / "lisp-code"
IPC_DIR = Path(os.environ.get("AUTOCAD_MCP_IPC_DIR", "C:/temp"))

# Backend selection
BACKEND_DEFAULT = "auto"  # auto | file_ipc | ezdxf

# IPC timeout (seconds), clamped to [1, 300]
IPC_TIMEOUT = max(1.0, min(300.0, float(os.environ.get("AUTOCAD_MCP_IPC_TIMEOUT", "10.0"))))

# Screenshot
ONLY_TEXT_FEEDBACK = os.environ.get("AUTOCAD_MCP_ONLY_TEXT", "").lower() in ("1", "true", "yes")

# Default cap (longest side, in pixels) applied to captured screenshots unless a
# caller explicitly passes max_dimension=None for full resolution.
#
# This is the lever that controls an LLM's cost for a screenshot: images are
# billed as ceil(w/28) * ceil(h/28) visual tokens, so token cost falls roughly
# with the square of the cap and does not depend on the encoded file size at all.
# At 1280 a typical AutoCAD window costs ~1.2-1.3k visual tokens instead of the
# ~3-4.8k it costs uncapped.
#
# The upper clamp is 2576 because the vision API downscales anything longer than
# that (and caps a single image at 4784 visual tokens) before the model sees it —
# pixels beyond that ceiling are transmitted and then discarded.
#
# The same bounds apply to a max_dimension passed as a tool argument, not just to
# this env default — an unclamped argument would let a single call ask for a
# resolution the vision API is going to throw away anyway.
SCREENSHOT_MAX_DIMENSION_RANGE = (64, 2576)
SCREENSHOT_MAX_DIMENSION = _env_int(
    "AUTOCAD_MCP_SCREENSHOT_MAX_DIM", 1280, *SCREENSHOT_MAX_DIMENSION_RANGE
)

# Win32 availability
WIN32_AVAILABLE = sys.platform == "win32"


def _current_backend_env() -> str:
    """Read backend selection from env with normalization."""
    return os.environ.get("AUTOCAD_MCP_BACKEND", BACKEND_DEFAULT).strip().lower()


def _is_wsl() -> bool:
    """Detect WSL Linux runtime."""
    if os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in os.uname().release.lower()
    except AttributeError:
        return False


def _write_debug_snapshot(backend_env: str):
    """Optionally write backend detection debug information.

    Set AUTOCAD_MCP_DEBUG_DETECT_FILE to enable.
    """
    debug_file = os.environ.get("AUTOCAD_MCP_DEBUG_DETECT_FILE", "").strip()
    if not debug_file:
        return

    try:
        debug_path = Path(debug_file)
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_path.open("w", encoding="utf-8") as f:
            f.write(f"sys.platform={sys.platform}\n")
            f.write(f"WIN32_AVAILABLE={WIN32_AVAILABLE}\n")
            f.write(f"BACKEND_ENV={backend_env}\n")
            f.write(f"python={sys.executable}\n")
    except Exception:
        # Best-effort only; never fail backend detection due debug writes.
        pass


def _raise_if_start_tab_active() -> None:
    """AutoCAD is running but no drawing window was found: Start is in front.

    Measured 2026-10-16: re-detecting while Start was active found no window
    with ".dwg" in its title, so `auto` fell through to ezdxf and the next calls
    read an empty in-memory drawing (entity_count 0, save_path null) instead of
    failing. An AutoCAD that is present must never be mistaken for one that is not.
    """
    from autocad_mcp.doc_switch import find_autocad_frame, open_document_titles

    frame = find_autocad_frame()
    if not frame:
        return
    raise RuntimeError(
        "start_tab_active: AutoCAD is running but the Start tab is in front, so there is no drawing "
        f"to talk to. Open drawings: {open_document_titles(frame)}. Switch with "
        "system(operation='switch_doc', data={'dwg': '<filename>'}) or click a drawing tab, then retry."
    )


def detect_backend() -> str:
    """Return the backend name to use: 'file_ipc' or 'ezdxf'.

    Raises RuntimeError with actionable message if explicit backend fails,
    or if AutoCAD is running with the Start tab in front (never falls to ezdxf then).
    """
    backend_env = _current_backend_env()
    _write_debug_snapshot(backend_env)

    if backend_env == "ezdxf":
        return "ezdxf"

    if backend_env in ("auto", "file_ipc"):
        if WIN32_AVAILABLE:
            try:
                from autocad_mcp.backends.file_ipc import find_autocad_window

                hwnd = find_autocad_window()
                if hwnd:
                    log.info("autocad_window_found", hwnd=hwnd)
                    return "file_ipc"
                _raise_if_start_tab_active()
                if backend_env == "file_ipc":
                    raise RuntimeError(
                        "AUTOCAD_MCP_BACKEND=file_ipc but no AutoCAD window found. "
                        "Start AutoCAD LT and open a .dwg file."
                    )
            except ImportError:
                if backend_env == "file_ipc":
                    raise RuntimeError(
                        "AUTOCAD_MCP_BACKEND=file_ipc requires pywin32. "
                        "Install with: pip install pywin32"
                    )
                log.info("win32_deps_missing_fallback_ezdxf")
        elif backend_env == "file_ipc":
            raise RuntimeError(
                "AUTOCAD_MCP_BACKEND=file_ipc requires Windows. "
                "Use AUTOCAD_MCP_BACKEND=ezdxf for headless mode."
            )
        elif _is_wsl():
            log.info(
                "wsl_linux_python_fallback_ezdxf",
                platform=sys.platform,
                python=sys.executable,
                hint="Launch MCP with Windows python.exe for File IPC backend.",
            )

    log.info("using_ezdxf_backend")
    return "ezdxf"
