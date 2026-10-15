"""Stale-dispatcher detection: ping reports a load time, Python compares it with the file's mtime."""

import os
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests.test_dispatch_claim import DISPATCH_LSP, _backend

UNIX_EPOCH_JULIAN = 2440588.0  # AutoCAD DATE: days from local midnight; 09:16 on 2026-10-15 read 2461329.386


def _julian(dt: datetime) -> float:
    return UNIX_EPOCH_JULIAN + (dt - datetime(1970, 1, 1)).total_seconds() / 86400


def _lsp_with_mtime(tmp_path: Path, dt: datetime) -> Path:
    lisp_dir = tmp_path / "lisp"
    lisp_dir.mkdir()
    (lisp_dir / "mcp_dispatch.lsp").write_text(";; stub\n")
    ts = dt.timestamp()
    os.utime(lisp_dir / "mcp_dispatch.lsp", (ts, ts))
    return lisp_dir


def _freshness(tmp_path, loaded, file_dt):
    backend = _backend(tmp_path, 2)
    backend._dispatcher_loaded = loaded
    with patch("autocad_mcp.backends.file_ipc.LISP_DIR", _lsp_with_mtime(tmp_path, file_dt)):
        return backend.dispatcher_freshness()


class TestLisp:
    def test_load_time_recorded_and_pinged(self):
        text = DISPATCH_LSP.read_text(encoding="utf-8")
        assert re.search(r'\(setq \*mcp-dispatcher-loaded\* \(getvar "DATE"\)\)', text)
        assert '",\\"loaded\\":" (rtos *mcp-dispatcher-loaded* 2 8)' in text


class TestFreshness:
    def test_file_older_than_load_is_fresh(self, tmp_path):
        info = _freshness(tmp_path, _julian(datetime(2026, 10, 14, 12)), datetime(2026, 10, 14, 9))
        assert info["stale"] is False

    def test_file_edited_after_load_is_stale(self, tmp_path):
        info = _freshness(tmp_path, _julian(datetime(2026, 10, 14, 9)), datetime(2026, 10, 14, 12))
        assert info["stale"] is True
        assert "Reload" in info["note"]

    def test_old_dispatcher_without_load_time_is_unknown(self, tmp_path):
        info = _freshness(tmp_path, None, datetime(2026, 10, 14, 12))
        assert info["stale"] is None
        assert "Reload it once" in info["note"]

    def test_unreadable_file_is_unknown(self, tmp_path):
        backend = _backend(tmp_path, 2)
        backend._dispatcher_loaded = _julian(datetime(2026, 10, 14, 12))
        with patch("autocad_mcp.backends.file_ipc.LISP_DIR", tmp_path / "missing"):
            assert backend.dispatcher_freshness()["stale"] is None

    def test_status_carries_the_block(self, tmp_path):
        import asyncio

        backend = _backend(tmp_path, 2)
        result = asyncio.run(backend.status())
        assert result.payload["dispatcher"]["version"] == 2


class TestMultiWindow:
    TWO = [(330688, "Autodesk AutoCAD LT 2027 - [Drawing2.dwg]"), (197062, "Autodesk AutoCAD LT 2027 - [Drawing1.dwg]")]

    def test_one_window_is_silent(self):
        from autocad_mcp.backends import file_ipc

        with patch.object(file_ipc, "list_autocad_windows", return_value=self.TWO[:1]):
            assert file_ipc.multi_window_warning(330688) is None

    def test_two_windows_name_the_one_in_use(self):
        from autocad_mcp.backends import file_ipc

        with patch.object(file_ipc, "list_autocad_windows", return_value=self.TWO):
            w = file_ipc.multi_window_warning(197062)
        assert w["count"] == 2 and w["using"].endswith("[Drawing1.dwg]")
        assert len(w["windows"]) == 2

    def test_find_returns_the_front_window(self):
        from autocad_mcp.backends import file_ipc

        with patch.object(file_ipc, "list_autocad_windows", return_value=self.TWO):
            assert file_ipc.find_autocad_window() == 330688

    def test_command_line_payload_carries_the_warning(self):
        import asyncio
        import json

        from autocad_mcp import command_line as cl
        from autocad_mcp.backends import file_ipc

        class Proc:
            returncode = 0

            async def communicate(self):
                return json.dumps({"history": "h", "live": ["Command: "]}).encode(), b""

        async def spawn(*a, **k):
            return Proc()

        with patch.object(cl.asyncio, "create_subprocess_exec", spawn), \
                patch.object(file_ipc, "list_autocad_windows", return_value=self.TWO), \
                patch.object(cl.sys, "platform", "win32"):
            out = asyncio.run(cl.read_command_line(197062, 5))
        assert out.payload["multi_window"]["count"] == 2
