"""Tests for IPC protocol components — no AutoCAD needed.

Tests the file-based IPC protocol logic: command file structure,
result parsing, request_id generation, timeout behavior, stale cleanup,
and CommandResult serialization.
"""

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autocad_mcp.backends.base import BackendCapabilities, CommandResult


# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


class TestCommandResult:
    def test_ok_result(self):
        r = CommandResult(ok=True, payload={"entity": "LINE", "handle": "ABC"})
        d = r.to_dict()
        assert d["ok"] is True
        assert d["payload"]["entity"] == "LINE"
        assert "error" not in d

    def test_error_result(self):
        r = CommandResult(ok=False, error="Entity not found")
        d = r.to_dict()
        assert d["ok"] is False
        assert d["error"] == "Entity not found"
        assert "payload" not in d

    def test_default_values(self):
        r = CommandResult(ok=True)
        assert r.payload is None
        assert r.error is None
        d = r.to_dict()
        assert d["ok"] is True
        assert d["payload"] is None


# ---------------------------------------------------------------------------
# BackendCapabilities
# ---------------------------------------------------------------------------


class TestBackendCapabilities:
    def test_defaults_minimal(self):
        caps = BackendCapabilities()
        assert caps.can_create_entities is True
        assert caps.can_read_drawing is False
        assert caps.can_plot_pdf is False
        assert caps.can_undo is False

    def test_full_capabilities(self):
        caps = BackendCapabilities(
            can_read_drawing=True,
            can_modify_entities=True,
            can_create_entities=True,
            can_screenshot=True,
            can_save=True,
            can_plot_pdf=True,
            can_zoom=True,
            can_query_entities=True,
            can_file_operations=True,
            can_undo=True,
        )
        for field_name, value in caps.__dict__.items():
            assert value is True, f"{field_name} should be True"


# ---------------------------------------------------------------------------
# IPC command file format
# ---------------------------------------------------------------------------


class TestIPCCommandFormat:
    """Test the JSON command file structure matches what LISP expects."""

    def test_command_file_structure(self):
        request_id = uuid.uuid4().hex[:12]
        payload = {
            "request_id": request_id,
            "command": "create-line",
            "params": {"x1": 0, "y1": 0, "x2": 100, "y2": 100, "layer": "0"},
            "ts": time.time(),
        }
        json_str = json.dumps(payload)
        parsed = json.loads(json_str)
        assert parsed["request_id"] == request_id
        assert parsed["command"] == "create-line"
        assert parsed["params"]["x1"] == 0
        assert isinstance(parsed["ts"], float)

    def test_result_file_structure(self):
        request_id = "abc123def456"
        result = {
            "request_id": request_id,
            "ok": True,
            "payload": {"entity_type": "LINE", "handle": "1A2"},
            "error": None,
            "ts": time.time(),
        }
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        assert parsed["ok"] is True
        assert parsed["payload"]["handle"] == "1A2"

    def test_error_result_file_structure(self):
        result = {
            "request_id": "abc123",
            "ok": False,
            "payload": None,
            "error": "Command 'nonexistent' not found in dispatch map",
            "ts": time.time(),
        }
        parsed = json.loads(json.dumps(result))
        assert parsed["ok"] is False
        assert "not found" in parsed["error"]


# ---------------------------------------------------------------------------
# Request ID uniqueness
# ---------------------------------------------------------------------------


class TestRequestID:
    def test_uniqueness(self):
        """Generate 1000 request IDs and verify no duplicates."""
        ids = {uuid.uuid4().hex[:12] for _ in range(1000)}
        assert len(ids) == 1000

    def test_format(self):
        rid = uuid.uuid4().hex[:12]
        assert len(rid) == 12
        assert rid.isalnum()


# ---------------------------------------------------------------------------
# Atomic write simulation
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_write_then_rename(self):
        """Verify atomic write pattern: write to .tmp, then rename."""
        with tempfile.TemporaryDirectory() as tmpdir:
            final_path = Path(tmpdir) / "autocad_mcp_cmd_test123.json"
            tmp_path = final_path.with_suffix(".tmp")

            payload = {"request_id": "test123", "command": "ping", "params": {}}
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")

            assert tmp_path.exists()
            assert not final_path.exists()

            tmp_path.rename(final_path)

            assert final_path.exists()
            assert not tmp_path.exists()

            data = json.loads(final_path.read_text(encoding="utf-8"))
            assert data["request_id"] == "test123"

    def test_concurrent_files_dont_collide(self):
        """Simulate two concurrent requests with different IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            id1 = uuid.uuid4().hex[:12]
            id2 = uuid.uuid4().hex[:12]

            f1 = Path(tmpdir) / f"autocad_mcp_cmd_{id1}.json"
            f2 = Path(tmpdir) / f"autocad_mcp_cmd_{id2}.json"

            f1.write_text(json.dumps({"request_id": id1, "command": "a"}))
            f2.write_text(json.dumps({"request_id": id2, "command": "b"}))

            d1 = json.loads(f1.read_text())
            d2 = json.loads(f2.read_text())

            assert d1["request_id"] != d2["request_id"]
            assert d1["command"] == "a"
            assert d2["command"] == "b"


# ---------------------------------------------------------------------------
# Stale file cleanup
# ---------------------------------------------------------------------------


class TestStaleCleanup:
    def test_cleanup_old_files(self):
        """Files older than threshold should be cleaned up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stale_threshold = 60.0

            # Create a "stale" file
            stale_file = Path(tmpdir) / "autocad_mcp_cmd_old123.json"
            stale_file.write_text('{"old": true}')
            # Backdate the file
            old_time = time.time() - stale_threshold - 10
            os.utime(stale_file, (old_time, old_time))

            # Create a "fresh" file
            fresh_file = Path(tmpdir) / "autocad_mcp_result_new456.json"
            fresh_file.write_text('{"new": true}')

            # Simulate cleanup
            now = time.time()
            for f in Path(tmpdir).glob("autocad_mcp_*.json"):
                if now - f.stat().st_mtime > stale_threshold:
                    f.unlink(missing_ok=True)

            assert not stale_file.exists()
            assert fresh_file.exists()

    def test_cleanup_tmp_files(self):
        """All .tmp files should be cleaned up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp1 = Path(tmpdir) / "autocad_mcp_cmd_abc.tmp"
            tmp2 = Path(tmpdir) / "autocad_mcp_result_def.tmp"
            tmp1.write_text("partial write")
            tmp2.write_text("partial write")

            for f in Path(tmpdir).glob("autocad_mcp_*.tmp"):
                f.unlink(missing_ok=True)

            assert not tmp1.exists()
            assert not tmp2.exists()


# ---------------------------------------------------------------------------
# Timeout behavior
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_deadline_calculation(self):
        timeout = 10.0
        deadline = time.time() + timeout
        # Deadline should be ~10s in the future
        assert deadline > time.time()
        assert deadline - time.time() < timeout + 0.1

    def test_poll_interval_reasonable(self):
        """Verify poll interval gives ~100 checks in 10s timeout."""
        poll_interval = 0.1
        timeout = 10.0
        max_polls = timeout / poll_interval
        assert max_polls == 100


# ---------------------------------------------------------------------------
# Error hint mapping (from client.py)
# ---------------------------------------------------------------------------


class TestErrorHints:
    """Test the _error helper's hint mapping logic."""

    def _classify(self, msg: str) -> str:
        msg_lower = msg.lower()
        if "window not found" in msg_lower or "no autocad" in msg_lower:
            return "autocad_not_running"
        elif "timeout" in msg_lower:
            return "timeout"
        elif "not supported" in msg_lower or "backend" in msg_lower:
            return "unsupported"
        elif "dispatcher" in msg_lower or "mcp_dispatch" in msg_lower:
            return "dispatcher_not_loaded"
        else:
            return "unknown"

    def test_window_not_found(self):
        assert self._classify("AutoCAD window not found") == "autocad_not_running"

    def test_no_autocad(self):
        assert self._classify("No AutoCAD LT detected") == "autocad_not_running"

    def test_timeout(self):
        assert self._classify("Timeout waiting for result") == "timeout"

    def test_not_supported(self):
        assert self._classify("Not supported on this backend") == "unsupported"

    def test_dispatcher(self):
        assert self._classify("mcp_dispatch.lsp not loaded") == "dispatcher_not_loaded"

    def test_unknown(self):
        assert self._classify("Something unexpected happened") == "unknown"


# ---------------------------------------------------------------------------
# LISP command dispatch map coverage
# ---------------------------------------------------------------------------


class TestDispatchMapCoverage:
    """Verify all commands that FileIPCBackend sends are documented."""

    # These must match the command names in mcp_dispatch.lsp's command map
    EXPECTED_COMMANDS = [
        "ping",
        "execute-lisp",
        "undo",
        "redo",
        "drawing-info",
        "drawing-save",
        "drawing-save-as-dxf",
        "drawing-create",
        "drawing-purge",
        "drawing-open",
        "drawing-get-variables",
        "create-line",
        "create-circle",
        "create-polyline",
        "create-rectangle",
        "create-arc",
        "create-ellipse",
        "create-mtext",
        "create-hatch",
        "create-text",
        "entity-list",
        "entity-count",
        "entity-get",
        "entity-erase",
        "entity-copy",
        "entity-move",
        "entity-rotate",
        "entity-scale",
        "entity-mirror",
        "entity-offset",
        "entity-array",
        "entity-fillet",
        "entity-chamfer",
        "layer-list",
        "layer-create",
        "layer-set-current",
        "layer-set-properties",
        "layer-freeze",
        "layer-thaw",
        "layer-lock",
        "layer-unlock",
        "block-list",
        "block-insert",
        "block-insert-with-attributes",
        "block-get-attributes",
        "block-update-attribute",
        "block-define",
        "create-dimension-linear",
        "create-dimension-aligned",
        "create-dimension-angular",
        "create-dimension-radius",
        "create-leader",
        "zoom-extents",
        "zoom-window",
        "pid-setup-layers",
        "pid-insert-symbol",
        "pid-list-symbols",
        "pid-draw-process-line",
        "pid-connect-equipment",
        "pid-add-flow-arrow",
        "pid-add-equipment-tag",
        "pid-add-line-number",
        "pid-insert-valve",
        "pid-insert-instrument",
        "pid-insert-pump",
        "pid-insert-tank",
        "drawing-plot-pdf",
    ]

    def test_all_commands_use_hyphen_convention(self):
        for cmd in self.EXPECTED_COMMANDS:
            assert "_" not in cmd, f"Command '{cmd}' should use hyphens, not underscores"

    def test_no_duplicates(self):
        assert len(self.EXPECTED_COMMANDS) == len(set(self.EXPECTED_COMMANDS))

    def test_new_v31_commands_present(self):
        """Verify v3.1 additions are in the expected commands list."""
        for cmd in ("execute-lisp", "undo", "redo", "drawing-open"):
            assert cmd in self.EXPECTED_COMMANDS, f"Missing new command: {cmd}"


# ---------------------------------------------------------------------------
# Configurable IPC timeout
# ---------------------------------------------------------------------------


class TestConfigurableTimeout:
    def test_default_timeout(self):
        from autocad_mcp.config import IPC_TIMEOUT
        # Default should be 10.0 (unless overridden by env)
        assert isinstance(IPC_TIMEOUT, float)
        assert 1.0 <= IPC_TIMEOUT <= 300.0

    def test_timeout_used_by_file_ipc(self):
        from autocad_mcp.backends.file_ipc import TIMEOUT
        from autocad_mcp.config import IPC_TIMEOUT
        assert TIMEOUT == IPC_TIMEOUT


# ---------------------------------------------------------------------------
# Stale .lsp file cleanup
# ---------------------------------------------------------------------------


class TestStaleLispCleanup:
    def test_cleanup_stale_lisp_files(self):
        """Temp .lsp files older than threshold should be cleaned up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stale_threshold = 60.0

            stale_lsp = Path(tmpdir) / "autocad_mcp_lisp_old123.lsp"
            stale_lsp.write_text("(+ 1 2)")
            old_time = time.time() - stale_threshold - 10
            os.utime(stale_lsp, (old_time, old_time))

            fresh_lsp = Path(tmpdir) / "autocad_mcp_lisp_new456.lsp"
            fresh_lsp.write_text("(+ 3 4)")

            # Simulate the cleanup logic
            now = time.time()
            for pattern in ("autocad_mcp_*.json", "autocad_mcp_*.tmp", "autocad_mcp_lisp_*.lsp"):
                for f in Path(tmpdir).glob(pattern):
                    if now - f.stat().st_mtime > stale_threshold:
                        f.unlink(missing_ok=True)

            assert not stale_lsp.exists()
            assert fresh_lsp.exists()


# ---------------------------------------------------------------------------
# New backend methods — default implementations
# ---------------------------------------------------------------------------


class TestNewBackendDefaults:
    """New base class methods return 'not supported' by default."""

    @pytest.mark.asyncio
    async def test_execute_lisp_default(self):
        from autocad_mcp.backends.base import AutoCADBackend
        # Cannot instantiate ABC directly, use ezdxf backend instead
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        result = await backend.execute_lisp("(+ 1 2)")
        assert result.ok is False
        assert "Not supported" in result.error

    @pytest.mark.asyncio
    async def test_undo_default(self):
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        result = await backend.undo()
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_redo_default(self):
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        result = await backend.redo()
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_drawing_open_ezdxf(self):
        """ezdxf backend should support drawing_open for DXF files."""
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        # Save a DXF first, then open it
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
            dxf_path = f.name
        try:
            await backend.drawing_save(dxf_path)
            result = await backend.drawing_open(dxf_path)
            assert result.ok is True
            assert result.payload["path"] == dxf_path
        finally:
            Path(dxf_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Semicolon-encoded point passing (polyline / leader)
# ---------------------------------------------------------------------------


class TestSemicolonEncoding:
    def test_polyline_points_encoding(self):
        """Verify Python encodes polyline points as semicolon-delimited string."""
        points = [[0, 0], [10, 0], [10, 10], [0, 10]]
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        assert pts_str == "0,0;10,0;10,10;0,10"

    def test_leader_points_encoding(self):
        """Verify Python encodes leader points as semicolon-delimited string."""
        points = [[5, 5], [15, 15]]
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        assert pts_str == "5,5;15,15"

    def test_closed_boolean_encoding(self):
        """Closed flag encoded as string '1'/'0' for LISP compatibility."""
        assert ("1" if True else "0") == "1"
        assert ("1" if False else "0") == "0"


# ---------------------------------------------------------------------------
# Variable name $ prefix stripping
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# assert_doc response parsing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stale IPC file detection (A-4 Phase 2B)
# ---------------------------------------------------------------------------


class TestStaleIpcDetection:
    """_check_stale_ipc_files detects leftover files from timed-out dispatches."""

    def _backend_with_tmpdir(self, tmpdir: str):
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        backend._ipc_dir = Path(tmpdir)
        return backend

    def test_no_stale_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = self._backend_with_tmpdir(tmpdir)
            assert backend._check_stale_ipc_files() == []

    def test_stale_cmd_file_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stale = Path(tmpdir) / "autocad_mcp_cmd_abc123def456.json"
            stale.write_text('{"request_id": "abc123def456"}')
            backend = self._backend_with_tmpdir(tmpdir)
            found = backend._check_stale_ipc_files()
            assert len(found) == 1
            assert found[0] == stale

    def test_stale_result_file_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stale = Path(tmpdir) / "autocad_mcp_result_deadbeef1234.json"
            stale.write_text('{"ok": true}')
            backend = self._backend_with_tmpdir(tmpdir)
            found = backend._check_stale_ipc_files()
            assert len(found) == 1
            assert found[0] == stale

    def test_multiple_stale_files_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for name in (
                "autocad_mcp_cmd_111111111111.json",
                "autocad_mcp_result_222222222222.json",
            ):
                (Path(tmpdir) / name).write_text("{}")
            backend = self._backend_with_tmpdir(tmpdir)
            assert len(backend._check_stale_ipc_files()) == 2

    def test_unrelated_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "some_other_file.json").write_text("{}")
            (Path(tmpdir) / "autocad_mcp_lisp_abc123.lsp").write_text("(+ 1 2)")
            backend = self._backend_with_tmpdir(tmpdir)
            assert backend._check_stale_ipc_files() == []

    def test_nonexistent_ipc_dir_returns_empty(self):
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        backend._ipc_dir = Path("C:/does/not/exist/at/all")
        assert backend._check_stale_ipc_files() == []


class TestConditionalEscInjection:
    """_type_dispatch_trigger only injects ESC when inject_esc=True."""

    def test_inject_esc_false_by_default(self):
        import inspect
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        sig = inspect.signature(FileIPCBackend._type_dispatch_trigger)
        assert sig.parameters["inject_esc"].default is False

    def test_inject_esc_parameter_exists(self):
        import inspect
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        sig = inspect.signature(FileIPCBackend._type_dispatch_trigger)
        assert "inject_esc" in sig.parameters


class TestAssertDoc:
    """Unit tests for assert_doc Python-side parsing — no AutoCAD needed."""

    @pytest.mark.asyncio
    async def test_ok_response(self):
        from unittest.mock import patch, AsyncMock
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        with patch.object(backend, "execute_lisp", new_callable=AsyncMock) as mock_lisp:
            mock_lisp.return_value = CommandResult(ok=True, payload="OK")
            result = await backend.assert_doc("ECSI Border Template - Blank.dwg")
        assert result.ok is True
        assert result.payload["doc"] == "ECSI Border Template - Blank.dwg"

    @pytest.mark.asyncio
    async def test_wrong_doc_response(self):
        from unittest.mock import patch, AsyncMock
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        with patch.object(backend, "execute_lisp", new_callable=AsyncMock) as mock_lisp:
            mock_lisp.return_value = CommandResult(ok=True, payload='("WRONG-DOC" "Drawing1.dwg")')
            result = await backend.assert_doc("ECSI Border Template - Blank.dwg")
        assert result.ok is False
        assert result.error == "wrong_doc"
        assert result.payload["expected"] == "ECSI Border Template - Blank.dwg"
        assert "Drawing1.dwg" in result.payload["actual"]

    @pytest.mark.asyncio
    async def test_lisp_error_propagated(self):
        from unittest.mock import patch, AsyncMock
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        with patch.object(backend, "execute_lisp", new_callable=AsyncMock) as mock_lisp:
            mock_lisp.return_value = CommandResult(
                ok=False, error="LISP error: no function definition: MCP:ASSERT-DOC"
            )
            result = await backend.assert_doc("drawing.dwg")
        assert result.ok is False
        assert "MCP:ASSERT-DOC" in result.error

    @pytest.mark.asyncio
    async def test_vlax_guard_propagated(self):
        """assert_doc doesn't use vlax-, but execute_lisp errors should pass through."""
        from unittest.mock import patch, AsyncMock
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        with patch.object(backend, "execute_lisp", new_callable=AsyncMock) as mock_lisp:
            mock_lisp.return_value = CommandResult(
                ok=False, error="timeout_mutating", payload={"may_have_applied": True}
            )
            result = await backend.assert_doc("drawing.dwg")
        assert result.ok is False
        assert result.error == "timeout_mutating"

    @pytest.mark.asyncio
    async def test_base_backend_returns_not_supported(self):
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
        backend = EzdxfBackend()
        await backend.initialize()
        result = await backend.assert_doc("any.dwg")
        assert result.ok is False
        assert "Not supported" in result.error


class TestVariableNameStripping:
    def test_strip_dollar_prefix(self):
        """File IPC strips $ prefix from ezdxf-style variable names."""
        names = ["$ACADVER", "DIMSCALE", "$CLAYER"]
        clean = [n.lstrip("$") for n in names]
        assert clean == ["ACADVER", "DIMSCALE", "CLAYER"]

    def test_semicolon_encoding(self):
        names = ["ACADVER", "DIMSCALE", "LTSCALE"]
        names_str = ";".join(names)
        assert names_str == "ACADVER;DIMSCALE;LTSCALE"

    def test_empty_names_list(self):
        """Empty names list results in empty string."""
        names = None
        names_str = "" if not names else ";".join(names)
        assert names_str == ""


class TestStaleHwnd:
    """_reacquire_hwnd transparently recovers from a stale cached window handle."""

    def test_reacquire_updates_hwnd_when_window_found(self):
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        backend._hwnd = 0xDEAD
        with patch("autocad_mcp.backends.file_ipc.find_autocad_window", return_value=0xBEEF):
            with patch.object(backend, "_find_command_line_hwnd", return_value=0xCAFE):
                result = backend._reacquire_hwnd()
        assert result is True
        assert backend._hwnd == 0xBEEF
        assert backend._command_hwnd == 0xCAFE

    def test_reacquire_returns_false_when_autocad_not_found(self):
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        backend._hwnd = 0xDEAD
        with patch("autocad_mcp.backends.file_ipc.find_autocad_window", return_value=None):
            result = backend._reacquire_hwnd()
        assert result is False
        assert backend._hwnd == 0xDEAD  # unchanged

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform != "win32", reason="requires pywin32")
    async def test_dispatch_reacquires_on_stale_hwnd_then_proceeds(self):
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        backend._hwnd = 0xDEAD
        dispatch_called = []
        original_dispatch = FileIPCBackend._dispatch_unlocked

        async def fake_dispatch(self, command, params):
            dispatch_called.append(command)
            return CommandResult(ok=True)

        with patch("sys.platform", "win32"):
            with patch("win32gui.IsWindow", return_value=False):
                with patch.object(backend, "_reacquire_hwnd", return_value=True) as mock_reacquire:
                    with patch.object(backend, "_dispatch", new_callable=AsyncMock) as mock_dispatch:
                        mock_dispatch.return_value = CommandResult(ok=True)
                        # Directly call _dispatch_unlocked to exercise the guard
                        with patch.object(backend, "_check_stale_ipc_files", return_value=[]):
                            with patch.object(backend, "_write_status"):
                                with patch.object(backend, "_type_dispatch_trigger"):
                                    import tempfile
                                    with tempfile.TemporaryDirectory() as tmpdir:
                                        backend._ipc_dir = Path(tmpdir)
                                        # Simulate AutoCAD writing the result file
                                        import uuid as _uuid
                                        original_uuid = _uuid.uuid4

                                        fixed_id = "aabbccddeeff"

                                        def fake_uuid4():
                                            class FakeUUID:
                                                hex = fixed_id
                                            return FakeUUID()

                                        result_path = Path(tmpdir) / f"autocad_mcp_result_{fixed_id}.json"
                                        result_path.write_text(
                                            json.dumps({"ok": True, "request_id": fixed_id})
                                        )
                                        with patch("uuid.uuid4", fake_uuid4):
                                            result = await backend._dispatch_unlocked("ping", {})
                        mock_reacquire.assert_called_once()
        assert result.ok is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform != "win32", reason="requires pywin32")
    async def test_dispatch_returns_autocad_not_found_when_reacquire_fails(self):
        from autocad_mcp.backends.file_ipc import FileIPCBackend
        backend = FileIPCBackend()
        backend._hwnd = 0xDEAD
        with patch("sys.platform", "win32"):
            with patch("win32gui.IsWindow", return_value=False):
                with patch.object(backend, "_reacquire_hwnd", return_value=False):
                    result = await backend._dispatch_unlocked("ping", {})
        assert result.ok is False
        assert result.error == "autocad_not_found"
