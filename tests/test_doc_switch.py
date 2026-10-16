"""system(switch_doc) and the Start-tab guard on backend detection.

Measured 2026-10-16 (LT 2027): WM_MDIACTIVATE to the frame's MDIClient moves
WM_MDIGETACTIVE, the frame title and DWGNAME; Ctrl+Tab is swallowed on Start;
re-detecting with Start in front fell through to ezdxf. These tests pin the
window logic against a fake win32gui; the live switch is checked by
scripts/verify_switch_doc.py.
"""

import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

from autocad_mcp import config, doc_switch
from autocad_mcp.backends.base import CommandResult

FRAME, MDI, START, TA, BP = 100, 200, 301, 302, 303


class FakeWin32:
    """One AutoCAD frame with Start + two drawings. WM_MDIACTIVATE takes effect
    unless `stuck` is set, which models AutoCAD ignoring the message."""

    def __init__(self, active=TA, stuck=False, frame_title="Autodesk AutoCAD LT 2027 - [TA.dwg]"):
        self.titles = {FRAME: frame_title, START: "Start", TA: "TA.dwg", BP: "BP.dwg"}
        self.active, self.stuck, self.sent = active, stuck, []

    def EnumWindows(self, cb, extra):
        cb(FRAME, extra)
        cb(999, extra)  # an unrelated window

    def IsWindowVisible(self, h):
        return True

    def GetClassName(self, h):
        return "AfxMDIFrame140u" if h == FRAME else "Chrome_WidgetWin_1"

    def GetWindowText(self, h):
        return self.titles.get(h, "AutoCAD forum - Chrome")

    def FindWindowEx(self, parent, after, cls, title):
        return MDI if parent == FRAME and cls == "MDIClient" else 0

    def EnumChildWindows(self, parent, cb, extra):
        for h in (START, TA, 401, BP):  # 401 is a grandchild
            cb(h, extra)

    def GetParent(self, h):
        return TA if h == 401 else MDI

    def SendMessage(self, h, msg, w, l):
        self.sent.append((h, msg, w))
        if msg == doc_switch.WM_MDIACTIVATE and not self.stuck:
            self.active = w
        return self.active if msg == doc_switch.WM_MDIGETACTIVE else 0


@pytest.fixture
def fake(monkeypatch):
    f = FakeWin32()
    monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(
        **{n: getattr(f, n) for n in dir(f) if n[0].isupper()}))
    monkeypatch.setattr(doc_switch, "_win32", lambda: True)
    monkeypatch.setattr(doc_switch, "SETTLE_TIMEOUT_S", 0.2)
    return f


def run(coro):
    return asyncio.run(coro)


class TestFrame:
    def test_finds_frame_by_class_not_by_dwg_title(self, fake):
        fake.titles[FRAME] = "Autodesk AutoCAD LT 2027 - Start"
        assert doc_switch.find_autocad_frame() == FRAME

    def test_direct_children_only_and_start_excluded_from_titles(self, fake):
        mdi, docs, active = doc_switch.list_documents(FRAME)
        assert mdi == MDI and active == TA
        assert [t for _, t in docs] == ["Start", "TA.dwg", "BP.dwg"]
        assert doc_switch.open_document_titles(FRAME) == ["TA.dwg", "BP.dwg"]


class TestSwitch:
    def test_switches_and_confirms(self, fake):
        r = run(doc_switch.switch_doc("BP.dwg"))
        assert r.ok and r.payload == {"before": "TA.dwg", "after": "BP.dwg", "switched": True}
        assert (MDI, doc_switch.WM_MDIACTIVATE, BP) in fake.sent

    def test_full_path_and_case_are_forgiven(self, fake):
        r = run(doc_switch.switch_doc(r"C:\Jobs\bp.DWG"))
        assert r.ok and r.payload["after"] == "BP.dwg"

    def test_from_start(self, fake):
        fake.active = START
        r = run(doc_switch.switch_doc("TA.dwg"))
        assert r.ok and r.payload["before"] == "Start" and r.payload["after"] == "TA.dwg"

    def test_already_active_sends_nothing(self, fake):
        r = run(doc_switch.switch_doc("TA.dwg"))
        assert r.ok and r.payload["switched"] is False
        assert not any(m == doc_switch.WM_MDIACTIVATE for _, m, _ in fake.sent)

    def test_refuses_start(self, fake):
        r = run(doc_switch.switch_doc("start"))
        assert not r.ok and r.error.startswith("refused")
        assert fake.sent == []

    def test_unknown_name_lists_open_drawings(self, fake):
        r = run(doc_switch.switch_doc("Nope.dwg"))
        assert not r.ok and r.error.startswith("no_such_document")
        assert r.payload["open"] == ["TA.dwg", "BP.dwg"]

    def test_empty_name(self, fake):
        assert "required" in run(doc_switch.switch_doc("  ")).error

    def test_unconfirmed_switch_fails(self, fake):
        fake.stuck = True
        r = run(doc_switch.switch_doc("BP.dwg"))
        assert not r.ok and r.error.startswith("switch_not_confirmed")
        assert r.payload["after"] == "TA.dwg"

    def test_no_keystrokes(self):
        src = open(doc_switch.__file__, encoding="utf-8").read()
        for verb in ("keybd_event", "SendInput", "SendKeys", "PostMessage", "WM_KEYDOWN"):
            assert verb not in src

    def test_not_windows(self, monkeypatch):
        monkeypatch.setattr(doc_switch, "_win32", lambda: False)
        assert "Windows" in run(doc_switch.switch_doc("BP.dwg")).error


class TestStartGuard:
    """Finding 3: with Start in front, `auto` must not pick ezdxf."""

    def test_start_tab_raises_instead_of_ezdxf(self, fake, monkeypatch):
        monkeypatch.setattr(config, "WIN32_AVAILABLE", True)
        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "auto")
        with patch("autocad_mcp.backends.file_ipc.find_autocad_window", return_value=None):
            with pytest.raises(RuntimeError, match="start_tab_active") as e:
                config.detect_backend()
        assert "TA.dwg" in str(e.value) and "switch_doc" in str(e.value)

    def test_no_autocad_still_falls_to_ezdxf(self, monkeypatch):
        monkeypatch.setattr(config, "WIN32_AVAILABLE", True)
        monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "auto")
        with patch("autocad_mcp.backends.file_ipc.find_autocad_window", return_value=None), \
                patch.object(doc_switch, "find_autocad_frame", return_value=None):
            assert config.detect_backend() == "ezdxf"

    def test_hint(self):
        from autocad_mcp.client import _error

        out = json.loads(_error(RuntimeError("start_tab_active: ...")))
        assert "switch_doc" in out["hint"]


class TestServerOp:
    """system(switch_doc): re-init after the move, ok only when DWGNAME agrees."""

    def _call(self, moved, dwgname):
        from autocad_mcp import client, server

        backend = AsyncMock()
        backend.load_libraries.return_value = CommandResult(ok=True, payload={"dwg": dwgname})
        with patch("autocad_mcp.doc_switch.switch_doc", AsyncMock(return_value=moved)), \
                patch.object(server, "get_backend", AsyncMock(return_value=backend)):
            client._backend = object()
            try:
                out = json.loads(run(server.system(operation="switch_doc", data={"dwg": "BP.dwg"})))
                return out, client._backend
            finally:
                client._backend = None

    def test_ok_when_dwgname_matches(self):
        moved = CommandResult(ok=True, payload={"before": "TA.dwg", "after": "BP.dwg", "switched": True})
        out, cached = self._call(moved, "BP.dwg")
        assert out["ok"] and out["payload"]["dwgname"] == "BP.dwg"
        assert cached is None  # backend was dropped for re-detection

    def test_mismatch_is_an_error(self):
        moved = CommandResult(ok=True, payload={"before": "TA.dwg", "after": "BP.dwg", "switched": True})
        out, _ = self._call(moved, "TA.dwg")
        assert not out["ok"] and out["error"].startswith("dwgname_mismatch")

    def test_move_failure_passes_through_without_reinit(self):
        out, cached = self._call(CommandResult(ok=False, error="no_such_document: x"), "TA.dwg")
        assert out == {"ok": False, "error": "no_such_document: x"}
        assert cached is not None
