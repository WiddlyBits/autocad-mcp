"""The one call a session runs before its first edit.

On 2026-08-22 the answers to "which libraries are live", "which document am I
in" and "which space is current" cost six separate `execute_lisp` round trips
across five sessions, at roughly 108 K cache-read tokens each. They are all
`getvar` and symbol lookups; there was never a reason for them to be six calls.

The other half of this is loading. `mcp_probes.lsp` appeared to be absent all
day — the whole L2/L3 probe rung silently degraded to hand-rolled AutoLISP —
because the APPLOAD Startup Suite only ever carried `mcp_dispatch.lsp`. Python
already knows where the files are, so `init` loads them by absolute path and
the Startup Suite stops being the thing that has to be right.
"""

import json
from unittest.mock import AsyncMock

import pytest

from autocad_mcp.backends.base import AutoCADBackend, CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend
from autocad_mcp.config import LISP_DIR


@pytest.fixture
def backend():
    return FileIPCBackend()


class TestPreflightLisp:
    """The snippet has to answer without the libraries it is asking about."""

    def test_reports_each_library_separately(self, backend):
        for key in ("dispatch", "probes", "select"):
            assert f'\\"{key}\\":' in backend._PREFLIGHT

    def test_reports_the_document_and_the_space(self, backend):
        assert '(getvar "DWGNAME")' in backend._PREFLIGHT
        assert '(getvar "CTAB")' in backend._PREFLIGHT

    def test_reports_pickfirst_and_tilemode(self, backend):
        assert '(getvar "PICKFIRST")' in backend._PREFLIGHT
        assert '(getvar "TILEMODE")' in backend._PREFLIGHT

    def test_it_does_not_use_atoms_family_to_test_for_a_definition(self, backend):
        """`(member 'sym (atoms-family 1))` compares a symbol against a list of
        STRINGS and is nil either way. That exact check was run on 2026-08-22
        and answered "NOT LOADED" — an answer it would also have given for a
        file that was loaded, which makes it evidence of nothing.

        An unbound symbol evaluates to nil rather than erroring, so `(type ...)`
        is both safe and actually discriminating."""
        assert "atoms-family" not in backend._PREFLIGHT
        assert "(type mcp:sel-dump)" in backend._PREFLIGHT

    def test_it_is_one_expression(self, backend):
        """The dispatcher returns the value of the LAST form. A preflight split
        across several top-level forms would report only its tail."""
        depth = 0
        closed_at = None
        for i, ch in enumerate(backend._PREFLIGHT):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and closed_at is None:
                    closed_at = i
        assert depth == 0
        assert closed_at == len(backend._PREFLIGHT) - 1


class TestLibraryLoading:
    def test_probes_load_before_select(self, backend):
        """mcp_select.lsp calls mcp:fmt, mcp:bb-union and mcp:ent-bbox-data.
        Load order is a dependency here, not a preference."""
        assert backend.LIBRARIES.index("mcp_probes.lsp") < backend.LIBRARIES.index(
            "mcp_select.lsp"
        )

    def test_the_dispatcher_is_not_one_of_them(self, backend):
        """mcp_dispatch.lsp is what receives the load instruction. It cannot
        load itself, and it stays the Startup Suite's one job."""
        assert "mcp_dispatch.lsp" not in backend.LIBRARIES

    @pytest.mark.parametrize("name", FileIPCBackend.LIBRARIES)
    def test_every_library_actually_exists(self, name):
        assert (LISP_DIR / name).is_file(), f"{name} is named but not present"

    def test_loads_by_absolute_path(self, backend):
        """`(load "mcp_probes.lsp")` depends on the AutoCAD support path, which
        is the invisible setting this replaces."""
        forms = backend._load_forms()
        for name in backend.LIBRARIES:
            assert str(LISP_DIR / name).replace("\\", "/") in forms

    def test_uses_forward_slashes(self, backend):
        """A backslash in an AutoLISP string literal is an escape character."""
        assert "\\" not in backend._load_forms()

    def test_each_load_is_guarded_by_findfile(self, backend):
        """Unguarded, one missing file makes `load` raise and takes the
        preflight down with it — reporting nothing about the libraries that
        *are* there, which is the case you most need reported."""
        forms = backend._load_forms()
        assert forms.count("(findfile ") == len(backend.LIBRARIES)
        assert forms.count("(load ") == len(backend.LIBRARIES)


class TestPreflightResult:
    async def test_payload_is_parsed_into_a_dict(self, backend):
        backend.execute_lisp = AsyncMock(
            return_value=CommandResult(
                ok=True,
                payload='{"dispatch":true,"probes":true,"select":true,"dwg":"X-DI-02.dwg","ctab":"Model"}',
            )
        )
        result = await backend.preflight()
        assert result.ok
        assert result.payload["select"] is True
        assert result.payload["dwg"] == "X-DI-02.dwg"

    async def test_unparseable_payload_is_surfaced_not_swallowed(self, backend):
        backend.execute_lisp = AsyncMock(
            return_value=CommandResult(ok=True, payload="; error: no function definition")
        )
        result = await backend.preflight()
        assert result.ok
        assert result.payload["raw"].startswith("; error")

    async def test_a_dead_dispatcher_stays_an_error(self, backend):
        backend.execute_lisp = AsyncMock(
            return_value=CommandResult(ok=False, error="IPC timeout")
        )
        result = await backend.preflight()
        assert not result.ok
        assert result.error == "IPC timeout"

    async def test_plain_preflight_does_not_load_anything(self, backend):
        """status() calls this. Reloading libraries on every health check would
        make an observation into a side effect."""
        backend.execute_lisp = AsyncMock(return_value=CommandResult(ok=True, payload="{}"))
        await backend.preflight()
        assert "(load " not in backend.execute_lisp.call_args[0][0]

    async def test_load_libraries_loads_then_reports(self, backend):
        """One round trip, not two: the preflight is the last form, so its
        value is the payload."""
        backend.execute_lisp = AsyncMock(return_value=CommandResult(ok=True, payload="{}"))
        await backend.load_libraries()
        code = backend.execute_lisp.call_args[0][0]
        assert code.count("(load ") == len(backend.LIBRARIES)
        assert code.index("(load ") < code.index("(strcat")


class TestOtherBackendsDecline:
    def test_the_base_class_answers_rather_than_raising(self):
        """ezdxf has no AutoLISP. status() folds the preflight in for every
        backend, so declining has to be an ordinary result."""
        for name in ("load_libraries", "preflight"):
            assert callable(getattr(AutoCADBackend, name))
