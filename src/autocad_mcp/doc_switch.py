"""Switch the active AutoCAD drawing without a click or a keystroke.

Measured 2026-10-16 on LT 2027:

- LT has no COM automation, OPEN is refused from LISP, and vla-Activate returns
  without moving AutoCAD's document manager (DWGNAME stays put).
- Ctrl+Tab works from a drawing but not from the Start tab: there, keyboard
  focus sits in an embedded Chromium widget (GetGUIThreadInfo hwndFocus class
  Chrome_WidgetWin_1, "AutoCAD AppHome") that swallows synthetic Ctrl+Tab.
- The main frame (AfxMDIFrame140u) has an MDIClient child. Each drawing is an
  MDI child whose window text is the bare filename, plus one titled "Start".
  WM_MDIACTIVATE to the MDIClient moves WM_MDIGETACTIVE, the frame title, and
  (after the backend re-initializes) DWGNAME. First no-click switch that
  AutoCAD actually follows.

Nothing here sends keystrokes, so there is no foreground or stray-key risk.
"""

from __future__ import annotations

import asyncio
import ntpath
import sys

from autocad_mcp.backends.base import CommandResult

WM_MDIACTIVATE = 0x0222
WM_MDIGETACTIVE = 0x0229
START_TITLE = "Start"
SETTLE_TIMEOUT_S = 2.0
POLL_S = 0.1


def _win32() -> bool:
    return sys.platform == "win32"


def find_autocad_frame() -> int | None:
    """The AutoCAD main frame, whichever tab is active.

    find_autocad_window() needs ".dwg" or "Drawing" in the title, so it misses
    the frame while Start is in front. This matches on the MFC frame class and
    an MDIClient child instead.
    """
    if not _win32():
        return None
    try:
        import win32gui
    except ImportError:
        return None

    frames: list[int] = []

    def callback(hwnd, result):
        if (
            win32gui.IsWindowVisible(hwnd)
            and win32gui.GetClassName(hwnd).startswith("AfxMDIFrame")
            and "autocad" in win32gui.GetWindowText(hwnd).lower()
            and win32gui.FindWindowEx(hwnd, 0, "MDIClient", None)
        ):
            result.append(hwnd)
        return True

    win32gui.EnumWindows(callback, frames)
    return frames[0] if frames else None


def list_documents(frame: int) -> tuple[int, list[tuple[int, str]], int]:
    """(mdi_client, [(child_hwnd, title)], active_child) for one frame.

    mdi_client is 0 when the frame has none.
    """
    import win32gui

    mdi = win32gui.FindWindowEx(frame, 0, "MDIClient", None)
    if not mdi:
        return 0, [], 0
    kids: list[int] = []

    def callback(hwnd, result):
        # Direct children only; EnumChildWindows also walks grandchildren.
        if win32gui.GetParent(hwnd) == mdi:
            result.append(hwnd)
        return True

    win32gui.EnumChildWindows(mdi, callback, kids)
    docs = [(h, win32gui.GetWindowText(h)) for h in kids]
    return mdi, docs, win32gui.SendMessage(mdi, WM_MDIGETACTIVE, 0, 0)


def open_document_titles(frame: int | None = None) -> list[str]:
    """Titles of the open drawings, Start excluded. [] when none can be read."""
    frame = frame or find_autocad_frame()
    if not frame:
        return []
    _, docs, _ = list_documents(frame)
    return [t for _, t in docs if t != START_TITLE]


async def switch_doc(dwg: str, frame: int | None = None) -> CommandResult:
    """Make the drawing titled `dwg` the active MDI child.

    Matches by title, never by a cached handle: handles change when a drawing is
    closed and reopened. A full path is reduced to its filename. Exactly one
    match is required; anything else fails with the titles that are open.

    This only moves the window. The caller must re-initialize the backend and
    confirm DWGNAME, because that is what AutoCAD's document manager reports.
    """
    if not _win32():
        return CommandResult(ok=False, error="switch_doc requires Windows (File IPC backend).")
    name = ntpath.basename((dwg or "").strip())
    if not name:
        return CommandResult(ok=False, error="data.dwg is required: the drawing's filename, e.g. 'Plan.dwg'.")
    if name.lower() == START_TITLE.lower():
        return CommandResult(ok=False, error="refused: Start is not a drawing, and the dispatcher cannot run there.")

    frame = frame or find_autocad_frame()
    if not frame:
        return CommandResult(ok=False, error="AutoCAD window not found. Start AutoCAD LT and open a .dwg file.")
    mdi, docs, active = list_documents(frame)
    if not mdi:
        return CommandResult(ok=False, error="AutoCAD frame has no MDIClient; cannot list drawings.")

    import win32gui

    titles = {h: t for h, t in docs}
    open_titles = [t for _, t in docs if t != START_TITLE]
    matches = [h for h, t in docs if t.lower() == name.lower()]
    if len(matches) != 1:
        return CommandResult(
            ok=False,
            error=f"no_such_document: {len(matches)} open drawings are titled '{name}'.",
            payload={"open": open_titles},
        )
    target = matches[0]
    before = titles.get(active)
    if target == active:
        return CommandResult(ok=True, payload={"before": before, "after": before, "switched": False})

    win32gui.SendMessage(mdi, WM_MDIACTIVATE, target, 0)
    # Measured: the switch lands well inside 700 ms; poll rather than sleep.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_TIMEOUT_S
    now = win32gui.SendMessage(mdi, WM_MDIGETACTIVE, 0, 0)
    while now != target and loop.time() < deadline:
        await asyncio.sleep(POLL_S)
        now = win32gui.SendMessage(mdi, WM_MDIGETACTIVE, 0, 0)
    after = titles.get(now, win32gui.GetWindowText(now) if now else None)
    if now != target:
        return CommandResult(
            ok=False,
            error=f"switch_not_confirmed: WM_MDIGETACTIVE still reports '{after}' after {SETTLE_TIMEOUT_S}s.",
            payload={"before": before, "after": after, "open": open_titles},
        )
    return CommandResult(ok=True, payload={"before": before, "after": after, "switched": True})
