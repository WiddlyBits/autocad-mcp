"""File-IPC behaviour that a live session depends on but a green suite did not check.

Each class pins one guarantee the dispatcher makes to the caller: that an
operation it cannot do honestly is refused rather than forwarded, and that an
erase goes through the undo stack. No AutoCAD is needed; the LISP half is
checked from its source, as in test_entity_parity.py.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

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
