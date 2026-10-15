"""system(command_line): read the AutoCAD command line while a command is active.

Measured 2026-10-13 (LT 2027, LINE waiting): the dispatcher refuses all reads,
LOGFILEON lags a prompt behind, WM_GETTEXT sees nothing, UI Automation works.
These tests pin the parsing and the plumbing; the UIA read itself needs a live
AutoCAD and is checked by hand.
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from autocad_mcp import command_line as cl
from autocad_mcp.backends.base import AutoCADBackend, CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend


class TestParse:
    def test_tail_and_live_prompt(self):
        raw = json.dumps({
            "history": "a\r\n\r\nb\r\nCommand: LINE\r\nSpecify first point: \r\n",
            "live": ["LINE", "Specify next point or ", "[", "]", ": "],
        })
        out = cl.parse_output(raw, 2)
        assert out["lines"] == ["Command: LINE", "Specify first point:"]
        assert out["prompt"] == "LINESpecify next point or []:"
        assert out["history_total"] == 4

    def test_single_live_block_arrives_as_a_string(self):
        """ConvertTo-Json collapses a one-element array to a bare string."""
        out = cl.parse_output(json.dumps({"history": "x", "live": "Command: "}), 5)
        assert out["live"] == ["Command: "]

    def test_empty_history(self):
        out = cl.parse_output(json.dumps({"history": "", "live": []}), 5)
        assert out["lines"] == [] and out["prompt"] == ""


class TestCommand:
    def test_hwnd_is_an_int_in_the_script_and_script_is_utf16_b64(self):
        cmd = cl.build_command(721308)
        script = base64.b64decode(cmd[-1]).decode("utf-16-le")
        assert "[IntPtr]721308" in script and "{hwnd}" not in script
        assert cmd[0] == "powershell.exe" and "-EncodedCommand" in cmd

    def test_script_is_read_only(self):
        for verb in ("SendKeys", "SendMessage", "Invoke(", "SetValue", "SendInput"):
            assert verb not in cl._SCRIPT


class TestRead:
    def test_no_hwnd(self):
        r = asyncio.run(cl.read_command_line(None))
        assert not r.ok and "system(init)" in r.error

    def test_clamps_n_and_reports_subprocess_failure(self):
        class P:
            returncode = 1
            async def communicate(self):
                return b"", b"boom"
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=P())):
            r = asyncio.run(cl.read_command_line(5, 10_000))
        assert not r.ok and "boom" in r.error

    def test_ok_path(self):
        class P:
            returncode = 0
            async def communicate(self):
                return json.dumps({"history": "h1\nh2", "live": ["Command: "]}).encode(), b""
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=P())):
            r = asyncio.run(cl.read_command_line(5, 1))
        assert r.ok and r.payload["lines"] == ["h2"]

    def test_payload_names_the_document_from_the_title_bar(self):
        class P:
            returncode = 0
            async def communicate(self):
                return json.dumps({"history": "h", "live": ["Command: "]}).encode(), b""
        fake = type("W", (), {"GetWindowText": staticmethod(
            lambda h: "Autodesk AutoCAD LT 2027  - [C:\jobs\Panel A.dwg]")})
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=P())), \
             patch.dict("sys.modules", {"win32gui": fake}), \
             patch("autocad_mcp.backends.file_ipc.list_autocad_windows", return_value=[]):
            r = asyncio.run(cl.read_command_line(5, 1))
        assert r.payload["document"] == "Panel A.dwg"

    def test_document_is_none_without_a_dwg_title(self):
        fake = type("W", (), {"GetWindowText": staticmethod(lambda h: "Autodesk AutoCAD LT 2027")})
        with patch.dict("sys.modules", {"win32gui": fake}):
            assert cl.window_document(5) is None


class TestBackend:
    def test_base_backend_says_unsupported(self):
        assert "command_line" in AutoCADBackend.__dict__

    def test_file_ipc_does_not_touch_the_dispatcher(self):
        b = FileIPCBackend()
        b._hwnd = 7
        b._dispatch = AsyncMock(side_effect=AssertionError("dispatcher used"))
        b.execute_lisp = AsyncMock(side_effect=AssertionError("dispatcher used"))
        with patch("autocad_mcp.command_line.read_command_line",
                   AsyncMock(return_value=CommandResult(ok=True, payload={}))) as m:
            r = asyncio.run(b.command_line(3))
        assert r.ok
        m.assert_awaited_once_with(7, 3)


class TestServerRoute:
    def test_fresh_server_never_inits_the_backend(self):
        # Backend init pings the dispatcher, which is refused while a command waits.
        from autocad_mcp import client, server

        ok = CommandResult(ok=True, payload={"lines": [], "prompt": "p", "live": []})
        with patch.object(client, "_backend", None), \
             patch.object(server, "get_backend", AsyncMock(side_effect=AssertionError("init"))), \
             patch("autocad_mcp.backends.file_ipc.find_autocad_window", return_value=7), \
             patch("autocad_mcp.command_line.read_command_line", AsyncMock(return_value=ok)) as m:
            out = asyncio.run(server.system(operation="command_line", data={"n": 3}))
        m.assert_awaited_once_with(7, 3)
        assert '"prompt"' in out


class TestHiddenOptions:
    LIVE = ["LINE", "Specify next point or ", "[", "]", ": "]

    def _parse(self, history):
        return cl.parse_output(json.dumps({"history": history, "live": self.LIVE}), 5)

    def test_options_recovered_from_scrollback(self):
        out = self._parse("Specify first point:\nSpecify next point or [Undo]: (c:mcp-dispatch)")
        assert "[Undo]" in out["prompt"] and "options_hidden" not in out

    def test_hidden_options_report_the_button_count(self):
        raw = json.dumps({"history": "Command: TEXT", "live": self.LIVE, "option_buttons": 2})
        out = cl.parse_output(raw, 5)
        assert out["options_hidden"] is True and out["option_count"] == 2

    def test_flagged_when_not_in_scrollback(self):
        out = self._parse("Command: LINE")
        assert out["options_hidden"] is True


class TestInitializeCommandActive:
    def _init(self, error):
        b = FileIPCBackend()
        b._dispatch = AsyncMock(return_value=CommandResult(ok=False, error=error))
        with patch("autocad_mcp.backends.file_ipc.find_autocad_window", return_value=7),              patch.object(b, "_find_command_line_hwnd", return_value=None),              patch.object(b, "_cleanup_stale_files"):
            return asyncio.run(b.initialize())

    def test_command_active_is_not_reported_as_not_loaded(self):
        r = self._init('{"error":"command_active","active":"LINE"}')
        assert not r.ok
        assert "command_active" in r.error and "Esc" in r.error and "command_line" in r.error
        assert "not loaded" not in r.error

    def test_a_silent_dispatcher_still_says_not_loaded(self):
        r = self._init("timeout")
        assert "mcp_dispatch.lsp not loaded" in r.error
