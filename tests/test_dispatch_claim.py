"""Dispatch claim: a timeout can tell "never started" from "may have run".

Every execute_lisp timeout came back may_have_applied:true, including a
read-only (mcp:sys-snapshot) one, because Python could not tell a command
the dispatcher never picked up from one still running. The v2 dispatcher
renames cmd_<id> to run_<id> before it runs anything, so if Python can still
delete cmd_<id> at the deadline, the command cannot have applied.

The IPC tests run the real _dispatch_unlocked against a temp dir, with the
keystroke trigger replaced by a fake LISP side. The LISP checks are structural.
"""

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.lisp_source import _defun_body as defun_body
from tests.lisp_source import strip_lisp

DISPATCH_LSP = Path(__file__).parent.parent / "lisp-code" / "mcp_dispatch.lsp"


def _backend(tmp_path, version):
    from autocad_mcp.backends.file_ipc import FileIPCBackend

    backend = FileIPCBackend()
    backend._ipc_dir = tmp_path
    backend._status_file = tmp_path / "mcp_status.txt"
    backend._dispatcher_version = version
    return backend


async def _run(backend, command, on_trigger=lambda d: None):
    """Dispatch with the trigger replaced; returns (result, trigger count)."""
    calls = []

    def trigger(inject_esc=False):
        calls.append(inject_esc)
        on_trigger(backend._ipc_dir)

    with patch.object(sys, "platform", "linux"), \
            patch("autocad_mcp.backends.file_ipc.TIMEOUT", 0.3), \
            patch.object(backend, "_type_dispatch_trigger", side_effect=trigger):
        result = await backend._dispatch_unlocked(command, {"code": "(mcp:sys-snapshot)"})
    return result, len(calls)


def _claim(ipc_dir):
    """What the v2 LISP does first: rename cmd_<id> to run_<id>, then hang."""
    for f in ipc_dir.glob("autocad_mcp_cmd_*.json"):
        f.rename(f.with_name(f.name.replace("_cmd_", "_run_")))


class TestTimeoutClaim:
    @pytest.mark.asyncio
    async def test_unclaimed_is_not_dispatched_and_retried_once(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        result, triggers = await _run(backend, "execute-lisp")
        assert result.error == "timeout_not_dispatched"
        assert result.payload["may_have_applied"] is False
        assert triggers == 2
        assert not list(tmp_path.glob("autocad_mcp_cmd_*.json"))

    @pytest.mark.asyncio
    async def test_unclaimed_then_claimed_on_retry_stays_mutating(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        state = {"n": 0}

        def second_time_claims(d):
            state["n"] += 1
            if state["n"] == 2:
                _claim(d)

        result, triggers = await _run(backend, "execute-lisp", second_time_claims)
        assert result.error == "timeout_mutating"
        assert triggers == 2

    @pytest.mark.asyncio
    async def test_claimed_keeps_timeout_mutating(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        result, triggers = await _run(backend, "execute-lisp", _claim)
        assert result.error == "timeout_mutating"
        assert result.payload["may_have_applied"] is True
        assert triggers == 1

    @pytest.mark.asyncio
    async def test_old_dispatcher_keeps_old_behaviour(self, tmp_path):
        backend = _backend(tmp_path, version=1)
        result, triggers = await _run(backend, "execute-lisp")
        assert result.error == "timeout_mutating"
        assert result.payload["may_have_applied"] is True
        assert triggers == 1

    def test_run_files_count_as_stale(self, tmp_path):
        backend = _backend(tmp_path, version=2)
        (tmp_path / "autocad_mcp_run_abc.json").write_text("{}")
        assert [p.name for p in backend._check_stale_ipc_files()] == ["autocad_mcp_run_abc.json"]

    def test_withdraw_reports_whether_file_existed(self, tmp_path):
        from autocad_mcp.backends.file_ipc import FileIPCBackend

        f = tmp_path / "autocad_mcp_cmd_x.json"
        f.write_text("{}")
        assert FileIPCBackend._withdraw(f) is True
        assert FileIPCBackend._withdraw(f) is False


class TestDispatcherLisp:
    text = DISPATCH_LSP.read_text(encoding="utf-8")

    def test_claim_rename_precedes_dispatch(self):
        body = strip_lisp(defun_body(self.text, "c:mcp-dispatch"))
        rename = body.index("vl-file-rename")
        assert rename < body.index("mcp-read-file-lines")
        assert rename < body.index("'mcp-dispatch-command")

    def test_run_file_name_slices_after_cmd_prefix(self):
        # substr is 1-based: "autocad_mcp_cmd_" is 16 chars, so the id starts at 17.
        assert len("autocad_mcp_cmd_") == 16
        assert '"autocad_mcp_run_" (substr (car cmd-files) 17)' in self.text

    def test_ping_reports_dispatcher_version(self):
        assert re.search(r"\(setq \*mcp-dispatcher-version\* 2\)", self.text)
        assert '\\"dispatcher\\":" (itoa *mcp-dispatcher-version*)' in self.text


class TestPingVersion:
    @pytest.mark.parametrize("payload,version", [
        ("pong", 1),
        ({"pong": True, "dispatcher": 2}, 2),
    ])
    @pytest.mark.asyncio
    async def test_initialize_reads_version(self, tmp_path, payload, version):
        from autocad_mcp.backends.base import CommandResult

        # tmp_path, not the default C:/temp, which exists only on some machines.
        backend = _backend(tmp_path, version=None)
        replies = [CommandResult(ok=True, payload=payload)]
        with patch("autocad_mcp.backends.file_ipc.find_autocad_window", return_value=1), \
                patch.object(backend, "_find_command_line_hwnd", return_value=None), \
                patch.object(backend, "_cleanup_stale_files"), \
                patch.object(backend, "_dispatch", side_effect=replies):
            result = await backend.initialize()
        assert result.ok
        assert backend._dispatcher_version == version
