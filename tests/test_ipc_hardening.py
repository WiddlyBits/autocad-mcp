"""File-IPC behaviour that a live session depends on but a green suite did not check.

Each class pins one guarantee the dispatcher makes to the caller: that an
operation it cannot do honestly is refused rather than forwarded, that a
timeout says whether the drawing may have changed, and that a save is checked
on disk rather than taken on the LISP side's word. No AutoCAD is needed: the
IPC tests run the real _dispatch_unlocked against a temp dir with the keystroke
trigger replaced, and the LISP half is checked from its source, as in
test_entity_parity.py.
"""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend
from tests.lisp_source import _defun_body, strip_lisp

DISPATCH_LSP = Path(__file__).parent.parent / "lisp-code" / "mcp_dispatch.lsp"


def _dispatch_source() -> str:
    return DISPATCH_LSP.read_text(encoding="utf-8")


class TestRedoIsRefused:
    """c:mcp-dispatch is itself a command, and running it clears the redo
    stack. Forwarding redo therefore redoes nothing and reports "redone"."""

    async def test_python_refuses_without_dispatching(self):
        backend = FileIPCBackend()
        with patch.object(backend, "_dispatch", new_callable=AsyncMock) as mock_dispatch:
            result = await backend.redo()
        assert result.ok is False
        assert "redo stack" in result.error
        mock_dispatch.assert_not_called()

    def test_lisp_redo_branch_does_not_run_redo(self):
        text = _dispatch_source()
        branch = text[text.index('((= cmd-name "redo")'):]
        branch = branch[: branch.index("((= cmd-name", 1)]
        assert "_.REDO" not in branch
        assert "(cons nil " in branch


class TestEraseIsUndoable:
    """entdel is not a command, so the erase was not undo-aware. _.ERASE is,
    and it gets the same existence check as entity(get)."""

    def test_erase_uses_the_erase_command(self):
        body = _defun_body(_dispatch_source(), "mcp-cmd-entity-erase")
        assert body.count('(command "_.ERASE" ent "")') == 2
        assert "(entdel" not in strip_lisp(body)


class TestFiledialogStateIsRestored:
    """save-as and open suppress the file dialog for one command, then put back
    the user's own FILEDIA rather than forcing it to 1."""

    @pytest.mark.parametrize("cmd", ["drawing-save", "drawing-open"])
    def test_prior_filedia_is_restored(self, cmd):
        text = _dispatch_source()
        branch = text[text.index(f'((= cmd-name "{cmd}")'):]
        branch = branch[: branch.index("((= cmd-name", 1)]
        assert '(setq old-filedia (getvar "FILEDIA"))' in branch
        assert '(setvar "FILEDIA" old-filedia)' in branch
        assert '(setvar "FILEDIA" 1)' not in branch


def _backend(tmp_path) -> FileIPCBackend:
    backend = FileIPCBackend()
    backend._ipc_dir = tmp_path
    backend._status_file = tmp_path / "mcp_status.txt"
    return backend


async def _timeout(backend, command, on_trigger=lambda: None):
    """Run the real dispatch with no LISP side answering, so it times out."""
    with patch.object(sys, "platform", "linux"),             patch("autocad_mcp.backends.file_ipc.TIMEOUT", 0.2),             patch.object(backend, "_type_dispatch_trigger",
                         side_effect=lambda *a, **k: on_trigger()):
        return await backend._dispatch_unlocked(command, {})


class TestFailureCarriesPayload:
    def test_error_with_payload_keeps_it(self):
        d = CommandResult(ok=False, error="x", payload={"may_have_applied": True}).to_dict()
        assert d == {"ok": False, "error": "x", "payload": {"may_have_applied": True}}

    def test_error_without_payload_has_no_payload_key(self):
        assert "payload" not in CommandResult(ok=False, error="x").to_dict()


class TestTimeoutSaysWhetherItMayHaveApplied:
    """A timed-out write may still land after Python gives up, so retrying it
    blind can apply it twice. The caller has to be told which kind it was."""

    async def test_mutating_command_is_tagged(self, tmp_path):
        result = await _timeout(_backend(tmp_path), "execute-lisp")
        assert result.error == "timeout_mutating"
        assert result.payload["may_have_applied"] is True
        assert result.payload["request_id"]

    async def test_read_only_command_keeps_plain_timeout(self, tmp_path):
        result = await _timeout(_backend(tmp_path), "drawing-info")
        assert result.error.startswith("Timeout waiting for result")
        assert result.payload is None


class TestStatusFile:
    """mcp_status.txt says what the dispatcher is doing, so a stuck session can
    be told apart from an idle one without sending it another command."""

    async def test_busy_during_dispatch_then_idle(self, tmp_path):
        backend = _backend(tmp_path)
        seen = []
        await _timeout(backend, "drawing-info",
                       lambda: seen.append(backend._status_file.read_text()))
        assert seen == ["busy:drawing-info"]
        assert backend._status_file.read_text() == "idle"

    async def test_status_reports_it(self, tmp_path):
        backend = _backend(tmp_path)
        backend._status_file.write_text("busy:drawing-save")
        result = await backend.status()
        assert result.payload["mcp_status"] == "busy:drawing-save"

    async def test_status_without_file_is_unknown(self, tmp_path):
        result = await _backend(tmp_path).status()
        assert result.payload["mcp_status"] == "unknown"


SAVES = [
    ("drawing_save", "drawing-save"),
    ("drawing_save_as_dxf", "drawing-save-as-dxf"),
    ("drawing_plot_pdf", "drawing-plot-pdf"),
]


class TestSaveIsVerifiedOnDisk:
    """The LISP side reports ok when the command ran, not when a file landed.
    A save is believed only if the file exists and its mtime moved."""

    @staticmethod
    async def _save(backend, method, path, write, stamp=None):
        def dispatch(*_):
            if write:
                path.write_text("x")
                if stamp is not None:
                    os.utime(path, (stamp, stamp))
            return CommandResult(ok=True, payload=str(path))

        with patch.object(backend, "_dispatch", new_callable=AsyncMock, side_effect=dispatch):
            return await getattr(backend, method)(str(path))

    @pytest.mark.parametrize("method,_cmd", SAVES)
    async def test_written_file_is_ok(self, tmp_path, method, _cmd):
        result = await self._save(_backend(tmp_path), method, tmp_path / "out.f", write=True)
        assert result.ok

    @pytest.mark.parametrize("method,_cmd", SAVES)
    async def test_mtime_on_the_whole_second_is_ok(self, tmp_path, method, _cmd):
        """AutoCAD stamps a .dwg mtime to the whole second, which can be
        earlier than time.time() taken when the call began."""
        stamp = float(int(time.time()) - 1)
        result = await self._save(_backend(tmp_path), method, tmp_path / "out.f",
                                  write=True, stamp=stamp)
        assert result.ok

    @pytest.mark.parametrize("method,_cmd", SAVES)
    async def test_missing_file_is_unverified(self, tmp_path, method, _cmd):
        result = await self._save(_backend(tmp_path), method, tmp_path / "out.f", write=False)
        assert result.error == "save_unverified"
        assert result.payload["detail"] == "file not found"

    @pytest.mark.parametrize("method,_cmd", SAVES)
    async def test_untouched_file_is_unverified(self, tmp_path, method, _cmd):
        path = tmp_path / "out.f"
        path.write_text("old")
        past = time.time() - 3600
        os.utime(path, (past, past))
        result = await self._save(_backend(tmp_path), method, path, write=False)
        assert result.error == "save_unverified"
        assert result.payload["detail"] == "timestamp unchanged"


class TestBusyAutoCADIsNotDispatchedTo:
    """A keystroke trigger typed into a document that is mid-command lands in
    that command's prompt. Document.IsQuiescent says so before anything is
    written; when COM is not there to ask, dispatch goes ahead as before."""

    @staticmethod
    def _com(is_quiescent=None, error=None):
        def get_active_object(_name):
            if error:
                raise error
            return SimpleNamespace(ActiveDocument=SimpleNamespace(IsQuiescent=is_quiescent))

        client = SimpleNamespace(GetActiveObject=get_active_object)
        return patch.dict(sys.modules, {"win32com": SimpleNamespace(client=client),
                                        "win32com.client": client})

    async def test_busy_returns_before_writing_a_command(self, tmp_path):
        backend = _backend(tmp_path)
        with patch.object(sys, "platform", "linux"),                 patch.object(backend, "_is_autocad_busy", return_value=True),                 patch.object(backend, "_type_dispatch_trigger") as trigger:
            result = await backend._dispatch_unlocked("drawing-info", {})
        assert result.error == "autocad_busy"
        trigger.assert_not_called()
        assert not list(tmp_path.glob("autocad_mcp_cmd_*.json"))

    def test_mid_command_is_busy(self):
        with patch.object(sys, "platform", "win32"), self._com(is_quiescent=False):
            assert FileIPCBackend()._is_autocad_busy() is True

    def test_quiescent_is_not_busy(self):
        with patch.object(sys, "platform", "win32"), self._com(is_quiescent=True):
            assert FileIPCBackend()._is_autocad_busy() is False

    def test_no_com_is_not_busy(self):
        with patch.object(sys, "platform", "win32"), self._com(error=OSError("no server")):
            assert FileIPCBackend()._is_autocad_busy() is False

    def test_off_windows_is_not_busy(self):
        with patch.object(sys, "platform", "linux"):
            assert FileIPCBackend()._is_autocad_busy() is False
