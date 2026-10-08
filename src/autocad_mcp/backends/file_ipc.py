"""File-based IPC backend for AutoCAD LT.

Protocol:
1. Python writes JSON command to C:/temp/autocad_mcp_cmd_{request_id}.json
2. Python types the fixed string "(c:mcp-dispatch)" + Enter
3. LISP reads cmd, dispatches via command map, writes result to
   C:/temp/autocad_mcp_result_{request_id}.json
4. Python polls for result file (100ms intervals, 10s timeout)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import structlog

from autocad_mcp.backends.base import AutoCADBackend, BackendCapabilities, CommandResult
from autocad_mcp.config import IPC_DIR, IPC_TIMEOUT, LISP_DIR, SCREENSHOT_MAX_DIMENSION

log = structlog.get_logger()

# IPC settings
POLL_INTERVAL = 0.1  # seconds
TIMEOUT = IPC_TIMEOUT  # seconds (configurable via AUTOCAD_MCP_IPC_TIMEOUT)
STALE_THRESHOLD = 60.0  # clean up files older than this

# Commands that write to the drawing — a timeout on these may have partially applied.
_MUTATING_COMMANDS: frozenset[str] = frozenset({
    "create-line", "create-circle", "create-polyline", "create-rectangle",
    "create-arc", "create-ellipse", "create-mtext", "create-hatch", "create-text",
    "create-dimension-linear", "create-dimension-aligned",
    "create-dimension-angular", "create-dimension-radius", "create-leader",
    "entity-erase", "entity-move", "entity-copy", "entity-rotate", "entity-scale",
    "entity-mirror", "entity-offset", "entity-array", "entity-fillet", "entity-chamfer",
    "block-insert", "block-insert-with-attributes", "block-update-attribute", "block-define",
    "layer-create", "layer-set-current", "layer-set-properties",
    "layer-freeze", "layer-thaw", "layer-lock", "layer-unlock",
    "drawing-save", "drawing-save-as-dxf", "drawing-plot-pdf", "drawing-purge",
    "execute-lisp",
    "pid-setup-layers", "pid-insert-symbol", "pid-draw-process-line",
    "pid-connect-equipment", "pid-add-flow-arrow", "pid-add-equipment-tag",
    "pid-add-line-number", "pid-insert-valve", "pid-insert-instrument",
    "pid-insert-pump", "pid-insert-tank",
})


def find_autocad_window() -> int | None:
    """Find the AutoCAD LT window handle by checking window titles."""
    if sys.platform != "win32":
        return None
    try:
        import win32gui

        windows: list[int] = []

        def callback(hwnd, result):
            if win32gui.IsWindowVisible(hwnd):
                text = win32gui.GetWindowText(hwnd).lower()
                if "autocad" in text and ("drawing" in text or ".dwg" in text):
                    result.append(hwnd)
            return True

        win32gui.EnumWindows(callback, windows)
        return windows[0] if windows else None
    except ImportError:
        return None


class FileIPCBackend(AutoCADBackend):
    """File-based IPC with AutoCAD LT via mcp_dispatch.lsp."""

    def __init__(self):
        self._hwnd: int | None = None
        self._command_hwnd: int | None = None
        self._ipc_dir = Path(IPC_DIR)
        self._screenshot_provider = None
        self._lock = asyncio.Lock()  # Single in-flight command
        self.is_lt: bool = False
        self._status_file = self._ipc_dir / "mcp_status.txt"
        # Set from the ping reply. >= 2 means LISP claims cmd files by renaming
        # them before dispatch, so a timeout can tell "never started" apart.
        self._dispatcher_version: int = 1

    @property
    def name(self) -> str:
        return "file_ipc"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
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

    async def initialize(self) -> CommandResult:
        """Find AutoCAD window and verify dispatcher is loaded."""
        self._hwnd = find_autocad_window()
        if not self._hwnd:
            return CommandResult(ok=False, error="AutoCAD LT window not found")

        # Set up screenshot provider
        try:
            from autocad_mcp.screenshot import Win32ScreenshotProvider

            self._screenshot_provider = Win32ScreenshotProvider(self._hwnd)
        except Exception:
            pass

        # Find command-line child edit control for focus-free dispatch
        self._command_hwnd = self._find_command_line_hwnd()
        log.info("command_line_hwnd", hwnd=self._command_hwnd)

        # Ensure IPC directory exists
        self._ipc_dir.mkdir(parents=True, exist_ok=True)

        # Clean up stale IPC files
        self._cleanup_stale_files()

        # Ping the dispatcher to verify it's loaded
        result = await self._dispatch("ping", {})
        if result.ok:
            # Old dispatchers reply with a bare "pong" and no version.
            payload = result.payload
            self._dispatcher_version = payload.get("dispatcher", 1) if isinstance(payload, dict) else 1
        if not result.ok:
            lisp_path = str(LISP_DIR / "mcp_dispatch.lsp").replace("\\", "/")
            return CommandResult(
                ok=False,
                error=(
                    "AutoCAD LT detected but mcp_dispatch.lsp not loaded.\n"
                    f'In AutoCAD command line, type:\n  (load "{lisp_path}")\n'
                    "Or add lisp-code/ to trusted paths for auto-loading."
                ),
            )

        # Detect AutoCAD LT vs full AutoCAD (logged; nothing branches on it)
        prog_result = await self.execute_lisp('(getvar "PROGRAM")')
        if prog_result.ok and isinstance(prog_result.payload, str):
            self.is_lt = prog_result.payload.strip().lower() == "acadlt"
        log.info("lt_detection", is_lt=self.is_lt)

        return CommandResult(ok=True, payload={"backend": "file_ipc", "hwnd": self._hwnd})

    def _write_status(self, msg: str) -> None:
        try:
            self._status_file.write_text(msg, encoding="utf-8")
        except OSError:
            pass

    async def status(self) -> CommandResult:
        info = {
            "backend": "file_ipc",
            "hwnd": self._hwnd,
            "ipc_dir": str(self._ipc_dir),
            "capabilities": {k: v for k, v in self.capabilities.__dict__.items()},
        }
        try:
            info["mcp_status"] = self._status_file.read_text(encoding="utf-8").strip()
        except OSError:
            info["mcp_status"] = "unknown"
        return CommandResult(ok=True, payload=info)

    # --- IPC dispatch ---

    def _reacquire_hwnd(self) -> bool:
        """Re-find the AutoCAD window after a restart. Returns True if acquired."""
        hwnd = find_autocad_window()
        if not hwnd:
            return False
        self._hwnd = hwnd
        self._command_hwnd = self._find_command_line_hwnd()
        try:
            from autocad_mcp.screenshot import Win32ScreenshotProvider
            self._screenshot_provider = Win32ScreenshotProvider(self._hwnd)
        except Exception:
            pass
        log.info("hwnd_reacquired", hwnd=self._hwnd, command_hwnd=self._command_hwnd)
        return True

    async def _dispatch(self, command: str, params: dict, expected_doc: str | None = None) -> CommandResult:
        """Send a command via file IPC and wait for result."""
        if expected_doc is not None:
            pre = await self.assert_doc(expected_doc)
            if not pre.ok:
                return pre
        async with self._lock:
            return await self._dispatch_unlocked(command, params)

    async def _dispatch_unlocked(self, command: str, params: dict, _retried: bool = False) -> CommandResult:
        """Core IPC logic (must be called under _lock)."""
        result = await self._dispatch_once(command, params)
        if result.error != "timeout_not_dispatched" or _retried:
            return result
        # The command never started, so re-sending cannot double-apply it.
        log.warning("timeout_not_dispatched_retry", command=command)
        return await self._dispatch_unlocked(command, params, _retried=True)

    async def _dispatch_once(self, command: str, params: dict) -> CommandResult:
        """Write one command file, trigger the dispatcher, and wait for its result."""
        request_id = uuid.uuid4().hex[:12]
        cmd_file = self._ipc_dir / f"autocad_mcp_cmd_{request_id}.json"
        result_file = self._ipc_dir / f"autocad_mcp_result_{request_id}.json"
        tmp_file = cmd_file.with_suffix(".tmp")

        # Detect and remove leftover IPC files from a previous timed-out dispatch.
        # ESC injection is targeted: only fired when stale files are present, not
        # unconditionally. Blanket ESC was interrupting user commands (A-4 T2).
        stale = self._check_stale_ipc_files()
        if stale:
            log.warning("stale_ipc_recovery", stale_count=len(stale), command=command)
            for f in stale:
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

        # Verify the cached hwnd is still valid. PostMessageW to a dead handle
        # fails silently — the cmd file is written but AutoCAD never receives the
        # keystroke, so the dispatch times out with no other indication.
        if sys.platform == "win32":
            try:
                import win32gui
                if self._hwnd is None or not win32gui.IsWindow(self._hwnd):
                    log.warning("stale_hwnd_detected", hwnd=self._hwnd)
                    if not self._reacquire_hwnd():
                        return CommandResult(ok=False, error="autocad_not_found")
            except ImportError:
                pass

        if self._is_autocad_busy():
            return CommandResult(ok=False, error="autocad_busy")

        self._write_status(f"busy:{command}")
        try:
            # Strip None values — the simple LISP JSON parser can't handle null
            clean_params = {k: v for k, v in params.items() if v is not None}
            # Atomic write: write to .tmp, then rename
            payload = {
                "request_id": request_id,
                "command": command,
                "params": clean_params,
                "ts": time.time(),
            }
            tmp_file.write_text(json.dumps(payload), encoding="utf-8")
            tmp_file.rename(cmd_file)

            # Type the fixed dispatch trigger; ESC only when recovering from stale state
            self._type_dispatch_trigger(inject_esc=bool(stale))

            # Poll for result
            deadline = time.time() + TIMEOUT
            while time.time() < deadline:
                if result_file.exists():
                    try:
                        # AutoCAD LISP writes files in Windows-1252 encoding;
                        # try UTF-8 first (covers ASCII), fall back to cp1252
                        try:
                            text = result_file.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            text = result_file.read_text(encoding="cp1252")
                        data = json.loads(text)
                        # Verify request_id matches
                        if data.get("request_id") == request_id:
                            return CommandResult(
                                ok=data.get("ok", False),
                                payload=data.get("payload"),
                                error=data.get("error"),
                            )
                    except (json.JSONDecodeError, OSError):
                        pass  # File may be partially written, retry
                await asyncio.sleep(POLL_INTERVAL)

            if self._dispatcher_version >= 2 and self._withdraw(cmd_file):
                return CommandResult(
                    ok=False,
                    error="timeout_not_dispatched",
                    payload={"may_have_applied": False, "request_id": request_id},
                )
            if command in _MUTATING_COMMANDS:
                return CommandResult(
                    ok=False,
                    error="timeout_mutating",
                    payload={"may_have_applied": True, "request_id": request_id},
                )
            return CommandResult(ok=False, error=f"Timeout waiting for result (request_id={request_id})")

        finally:
            self._write_status("idle")
            # Cleanup
            for f in (cmd_file, result_file, tmp_file):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    def _check_stale_ipc_files(self) -> list[Path]:
        """Return leftover IPC files from a previous timed-out dispatch.

        Called at the start of every dispatch before any files are written for
        the current request, so any match predates the current call. Presence
        of these files means a prior timed-out mutating dispatch may have left
        AutoCAD mid-command and ESC recovery is warranted.
        """
        found: list[Path] = []
        try:
            for pattern in ("autocad_mcp_cmd_*.json", "autocad_mcp_run_*.json", "autocad_mcp_result_*.json"):
                found.extend(self._ipc_dir.glob(pattern))
        except OSError:
            pass
        return found

    @staticmethod
    def _withdraw(cmd_file: Path) -> bool:
        """Delete an unclaimed command file. True means LISP never claimed it.

        The v2 dispatcher renames cmd_<id> to run_<id> before running anything,
        so a cmd file that still exists to be deleted cannot have applied.
        """
        try:
            cmd_file.unlink()
            return True
        except OSError:
            return False

    def _find_command_line_hwnd(self) -> int | None:
        """Find AutoCAD's MDIClient child window for command routing."""
        if sys.platform != "win32" or not self._hwnd:
            return None
        try:
            import win32gui

            mdi_client: list[int] = []

            def cb(child_hwnd, _):
                if win32gui.GetClassName(child_hwnd) == "MDIClient":
                    mdi_client.append(child_hwnd)
                    return False  # stop enumeration
                return True

            win32gui.EnumChildWindows(self._hwnd, cb, None)
            return mdi_client[0] if mdi_client else None
        except Exception:
            return None

    def _is_autocad_busy(self) -> bool:
        """Return True if AutoCAD's active document is mid-command.

        Best-effort: returns False if COM is unavailable (LT, no pywin32,
        no document open). Caller must hold _lock so the check is atomic
        with the subsequent file write.
        """
        if sys.platform != "win32":
            return False
        try:
            import win32com.client
            app = win32com.client.GetActiveObject("AutoCAD.Application")
            return not app.ActiveDocument.IsQuiescent
        except Exception:
            return False

    def _type_dispatch_trigger(self, inject_esc: bool = False) -> None:
        """Post '(c:mcp-dispatch)' + Enter via WM_CHAR to MDIClient — no focus steal.

        inject_esc: send 2×ESC before dispatching to cancel a stale pending
        command. Only True when _check_stale_ipc_files() found leftover files,
        indicating a prior timed-out dispatch may have left AutoCAD mid-command.
        Not injected on clean dispatches to avoid interrupting user commands.
        """
        try:
            import ctypes

            WM_CHAR = 0x0102
            WM_KEYDOWN = 0x0100
            WM_KEYUP = 0x0101
            VK_ESCAPE = 0x1B
            target = self._command_hwnd or self._hwnd
            post = ctypes.windll.user32.PostMessageW

            if inject_esc:
                # Cancel stale pending command (2x ESC for nested commands)
                for _ in range(2):
                    post(target, WM_KEYDOWN, VK_ESCAPE, 0)
                    post(target, WM_KEYUP, VK_ESCAPE, 0)
                time.sleep(0.05)

            for ch in "(c:mcp-dispatch)":
                post(target, WM_CHAR, ord(ch), 0)
            # Enter = carriage return
            post(target, WM_CHAR, 0x0D, 0)
            time.sleep(0.05)
        except Exception as e:
            log.error("dispatch_trigger_failed", error=str(e))

    def _cleanup_stale_files(self):
        """Remove stale IPC files from previous sessions."""
        try:
            now = time.time()
            for pattern in ("autocad_mcp_*.json", "autocad_mcp_*.tmp", "autocad_mcp_lisp_*.lsp"):
                for f in self._ipc_dir.glob(pattern):
                    if now - f.stat().st_mtime > STALE_THRESHOLD:
                        f.unlink(missing_ok=True)
        except OSError:
            pass

    # --- Drawing management ---

    @staticmethod
    def _file_state(path: str) -> tuple[int, int] | None:
        """(mtime_ns, size) of path, or None when it does not exist."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _verify_file_written(
        self, path: str, before: tuple[int, int] | None
    ) -> CommandResult | None:
        """Return an error CommandResult unless path is new or its mtime/size moved.

        Compares against the file's own state from before the call, not the
        wall clock: AutoCAD stamps a .dwg mtime to the whole second, so a save
        landing inside the second it started compared <= time.time() and read
        as unchanged (x.dwg from a .dwt, 2026-10-08).
        """
        after = self._file_state(path)
        if after is None:
            return CommandResult(
                ok=False, error="save_unverified",
                payload={"detail": "file not found", "path": path},
            )
        if after == before:
            return CommandResult(
                ok=False, error="save_unverified",
                payload={"detail": "timestamp unchanged", "path": path},
            )
        return None

    async def drawing_info(self) -> CommandResult:
        return await self._dispatch("drawing-info", {})

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        before = self._file_state(path) if path else None
        result = await self._dispatch("drawing-save", {"path": path})
        if result.ok and path:
            err = self._verify_file_written(path, before)
            if err:
                return err
        return result

    async def drawing_save_as_dxf(self, path: str) -> CommandResult:
        before = self._file_state(path)
        result = await self._dispatch("drawing-save-as-dxf", {"path": path})
        if result.ok:
            err = self._verify_file_written(path, before)
            if err:
                return err
        return result

    async def drawing_create(self, name: str | None = None) -> CommandResult:
        return await self._dispatch("drawing-create", {"name": name})

    async def drawing_purge(self) -> CommandResult:
        return await self._dispatch("drawing-purge", {})

    async def drawing_plot_pdf(self, path: str) -> CommandResult:
        before = self._file_state(path)
        result = await self._dispatch("drawing-plot-pdf", {"path": path})
        if result.ok:
            err = self._verify_file_written(path, before)
            if err:
                return err
        return result

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        if names:
            # Strip $ prefix for AutoCAD compatibility (ezdxf uses $ACADVER, AutoCAD uses ACADVER)
            clean_names = [n.lstrip("$") for n in names]
            names_str = ";".join(clean_names)
        else:
            names_str = ""
        return await self._dispatch("drawing-get-variables", {"names_str": names_str})

    async def drawing_open(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-open", {"path": path})

    # --- Undo / Redo ---

    async def undo(self) -> CommandResult:
        return await self._dispatch("undo", {})

    async def redo(self) -> CommandResult:
        return CommandResult(
            ok=False,
            error="Redo is not available through MCP dispatch — the dispatch command clears the redo stack",
        )

    # --- Freehand LISP execution ---

    async def execute_lisp(self, code: str, expected_doc: str | None = None) -> CommandResult:
        """Execute arbitrary AutoLISP code via temp file.

        File persists for session; cleaned up by _cleanup_stale_files().
        expected_doc: when provided, assert_doc() runs before the IPC file is written.
        """
        # No vlax- guard: on LT 2027 (2026-10-08) vlax-get-acad-object through
        # this path returned the document name without hanging. Inline code is
        # loaded from a temp file exactly like (load "x.lsp"), so both are covered.
        request_id = uuid.uuid4().hex[:12]
        code_file = self._ipc_dir / f"autocad_mcp_lisp_{request_id}.lsp"
        code_file.write_text(code, encoding="utf-8")
        return await self._dispatch("execute-lisp", {
            "code_file": str(code_file).replace("\\", "/")
        }, expected_doc=expected_doc)

    async def assert_doc(self, expected_name: str) -> CommandResult:
        """Verify the active drawing matches expected_name before a batch.

        Calls (mcp:assert-doc ...) from mcp_probes.lsp.
        Returns ok:True  when names match (case-insensitive, per the LISP impl).
        Returns ok:False with error "wrong_doc" when they differ, carrying
        {"expected": ..., "actual": <raw LISP payload>} in payload.
        Propagates any execute_lisp error (e.g. probes not loaded) unchanged.
        """
        result = await self.execute_lisp(f'(mcp:assert-doc "{expected_name}")')
        if not result.ok:
            return result
        if result.payload == "OK":
            return CommandResult(ok=True, payload={"doc": expected_name})
        return CommandResult(
            ok=False,
            error="wrong_doc",
            payload={"expected": expected_name, "actual": str(result.payload)},
        )

    # --- Library loading and preflight ---

    #: Libraries the Startup Suite does not have to carry, in load order.
    #: mcp_select.lsp calls into mcp_probes.lsp (mcp:fmt, mcp:bb-union,
    #: mcp:ent-bbox-data), so the order is a dependency, not a preference.
    LIBRARIES = ("mcp_probes.lsp", "mcp_select.lsp")

    #: `(type sym)` rather than `(member 'sym (atoms-family 1))`.
    #:
    #: atoms-family with format 1 returns a list of STRINGS, so testing it
    #: with a quoted symbol compares a symbol against strings and is nil
    #: whether or not the function exists. That check was used on 2026-08-22
    #: and answered "NOT LOADED"; it would have answered "NOT LOADED" for a
    #: loaded file too. An unbound symbol evaluates to nil in AutoLISP rather
    #: than erroring, so (type ...) is both safe and actually discriminating.
    _PREFLIGHT = (
        '(strcat "{\\"dispatch\\":" (if (type c:mcp-dispatch) "true" "false")'
        ' ",\\"probes\\":" (if (type mcp:text-dump-in) "true" "false")'
        ' ",\\"select\\":" (if (type mcp:sel-dump) "true" "false")'
        ' ",\\"reactor_fn\\":" (if (type mcp:on-cmd) "true" "false")'
        ' ",\\"dwg\\":\\"" (getvar "DWGNAME") "\\""'
        ' ",\\"ctab\\":\\"" (getvar "CTAB") "\\""'
        ' ",\\"tilemode\\":" (itoa (getvar "TILEMODE"))'
        ' ",\\"pickfirst\\":" (itoa (getvar "PICKFIRST")) "}")'
    )

    def _load_forms(self) -> str:
        """`(load ...)` guarded by `(findfile ...)` for each library.

        Unguarded, a missing file makes `load` raise and takes the preflight
        down with it — which would report nothing about the libraries that
        *are* there.
        """
        forms = []
        for name in self.LIBRARIES:
            path = str(LISP_DIR / name).replace("\\", "/")
            forms.append(f'(if (findfile "{path}") (load "{path}"))')
        return "".join(forms)

    async def load_libraries(self) -> CommandResult:
        """Load the helper libraries, then report what is live.

        One round trip, not three: the preflight is the last form, so its
        value is the payload.
        """
        return await self.preflight(load_first=True)

    async def preflight(self, load_first: bool = False) -> CommandResult:
        code = (self._load_forms() if load_first else "") + self._PREFLIGHT
        result = await self.execute_lisp(code)
        if not result.ok:
            return result
        # The dispatcher hands back the last form's value as a string. Parsing
        # it here means callers get a dict rather than each writing their own
        # ad-hoc parse of the same shape.
        try:
            return CommandResult(ok=True, payload=json.loads(result.payload))
        except (TypeError, ValueError):
            return CommandResult(ok=True, payload={"raw": result.payload})

    # --- Entity operations ---

    async def create_line(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        return await self._dispatch("create-line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer})

    async def create_circle(self, cx, cy, radius, layer=None) -> CommandResult:
        return await self._dispatch("create-circle", {"cx": cx, "cy": cy, "radius": radius, "layer": layer})

    async def create_polyline(self, points, closed=False, layer=None) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("create-polyline", {
            "points_str": pts_str, "closed": "1" if closed else "0", "layer": layer
        })

    async def create_rectangle(self, x1, y1, x2, y2, layer=None) -> CommandResult:
        return await self._dispatch("create-rectangle", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer})

    async def create_arc(self, cx, cy, radius, start_angle, end_angle, layer=None) -> CommandResult:
        return await self._dispatch("create-arc", {"cx": cx, "cy": cy, "radius": radius, "start_angle": start_angle, "end_angle": end_angle, "layer": layer})

    async def create_ellipse(self, cx, cy, major_x, major_y, ratio, layer=None) -> CommandResult:
        return await self._dispatch("create-ellipse", {"cx": cx, "cy": cy, "major_x": major_x, "major_y": major_y, "ratio": ratio, "layer": layer})

    async def create_mtext(self, x, y, width, text, height=2.5, layer=None) -> CommandResult:
        return await self._dispatch("create-mtext", {"x": x, "y": y, "width": width, "text": text, "height": height, "layer": layer})

    async def create_hatch(self, entity_id, pattern="ANSI31") -> CommandResult:
        return await self._dispatch("create-hatch", {"entity_id": entity_id, "pattern": pattern})

    async def entity_list(self, layer=None) -> CommandResult:
        return await self._dispatch("entity-list", {"layer": layer})

    async def entity_count(self, layer=None) -> CommandResult:
        return await self._dispatch("entity-count", {"layer": layer})

    async def entity_get(self, entity_id) -> CommandResult:
        return await self._dispatch("entity-get", {"entity_id": entity_id})

    async def entity_erase(self, entity_id) -> CommandResult:
        return await self._dispatch("entity-erase", {"entity_id": entity_id})

    async def entity_copy(self, entity_id, dx, dy) -> CommandResult:
        return await self._dispatch("entity-copy", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def entity_move(self, entity_id, dx, dy) -> CommandResult:
        return await self._dispatch("entity-move", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def entity_rotate(self, entity_id, cx, cy, angle) -> CommandResult:
        return await self._dispatch("entity-rotate", {"entity_id": entity_id, "cx": cx, "cy": cy, "angle": angle})

    async def entity_scale(self, entity_id, cx, cy, factor) -> CommandResult:
        return await self._dispatch("entity-scale", {"entity_id": entity_id, "cx": cx, "cy": cy, "factor": factor})

    async def entity_mirror(self, entity_id, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("entity-mirror", {"entity_id": entity_id, "x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def entity_offset(self, entity_id, distance) -> CommandResult:
        return await self._dispatch("entity-offset", {"entity_id": entity_id, "distance": distance})

    async def entity_array(self, entity_id, rows, cols, row_dist, col_dist) -> CommandResult:
        return await self._dispatch("entity-array", {"entity_id": entity_id, "rows": rows, "cols": cols, "row_dist": row_dist, "col_dist": col_dist})

    async def entity_fillet(self, entity_id1, entity_id2, radius) -> CommandResult:
        return await self._dispatch("entity-fillet", {"id1": entity_id1, "id2": entity_id2, "radius": radius})

    async def entity_chamfer(self, entity_id1, entity_id2, dist1, dist2) -> CommandResult:
        return await self._dispatch("entity-chamfer", {"id1": entity_id1, "id2": entity_id2, "dist1": dist1, "dist2": dist2})

    # --- Layer operations ---

    async def layer_list(self) -> CommandResult:
        return await self._dispatch("layer-list", {})

    async def layer_create(self, name, color="white", linetype="CONTINUOUS") -> CommandResult:
        return await self._dispatch("layer-create", {"name": name, "color": color, "linetype": linetype})

    async def layer_set_current(self, name) -> CommandResult:
        return await self._dispatch("layer-set-current", {"name": name})

    async def layer_set_properties(self, name, color=None, linetype=None, lineweight=None) -> CommandResult:
        return await self._dispatch("layer-set-properties", {"name": name, "color": color, "linetype": linetype, "lineweight": lineweight})

    async def layer_freeze(self, name) -> CommandResult:
        return await self._dispatch("layer-freeze", {"name": name})

    async def layer_thaw(self, name) -> CommandResult:
        return await self._dispatch("layer-thaw", {"name": name})

    async def layer_lock(self, name) -> CommandResult:
        return await self._dispatch("layer-lock", {"name": name})

    async def layer_unlock(self, name) -> CommandResult:
        return await self._dispatch("layer-unlock", {"name": name})

    # --- Block operations ---

    async def block_list(self) -> CommandResult:
        return await self._dispatch("block-list", {})

    async def block_insert(self, name, x, y, scale=1.0, rotation=0.0, block_id=None) -> CommandResult:
        return await self._dispatch("block-insert", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "block_id": block_id})

    async def block_insert_with_attributes(self, name, x, y, scale=1.0, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("block-insert-with-attributes", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "attributes": attributes or {}})

    async def block_get_attributes(self, entity_id) -> CommandResult:
        return await self._dispatch("block-get-attributes", {"entity_id": entity_id})

    async def block_update_attribute(self, entity_id, tag, value) -> CommandResult:
        return await self._dispatch("block-update-attribute", {"entity_id": entity_id, "tag": tag, "value": value})

    async def block_define(self, name, entities) -> CommandResult:
        return await self._dispatch("block-define", {"name": name, "entities": entities})

    # --- Annotation ---

    async def create_text(self, x, y, text, height=2.5, rotation=0.0, layer=None) -> CommandResult:
        return await self._dispatch("create-text", {"x": x, "y": y, "text": text, "height": height, "rotation": rotation, "layer": layer})

    async def create_dimension_linear(self, x1, y1, x2, y2, dim_x, dim_y) -> CommandResult:
        return await self._dispatch("create-dimension-linear", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "dim_x": dim_x, "dim_y": dim_y})

    async def create_dimension_aligned(self, x1, y1, x2, y2, offset) -> CommandResult:
        return await self._dispatch("create-dimension-aligned", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "offset": offset})

    async def create_dimension_angular(self, cx, cy, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("create-dimension-angular", {"cx": cx, "cy": cy, "x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def create_dimension_radius(self, cx, cy, radius, angle) -> CommandResult:
        return await self._dispatch("create-dimension-radius", {"cx": cx, "cy": cy, "radius": radius, "angle": angle})

    async def create_leader(self, points, text) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("create-leader", {"points_str": pts_str, "text": text})

    # --- P&ID ---

    async def pid_setup_layers(self) -> CommandResult:
        return await self._dispatch("pid-setup-layers", {})

    async def pid_insert_symbol(self, category, symbol, x, y, scale=1.0, rotation=0.0) -> CommandResult:
        return await self._dispatch("pid-insert-symbol", {"category": category, "symbol": symbol, "x": x, "y": y, "scale": scale, "rotation": rotation})

    async def pid_list_symbols(self, category) -> CommandResult:
        return await self._dispatch("pid-list-symbols", {"category": category})

    async def pid_draw_process_line(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("pid-draw-process-line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def pid_connect_equipment(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("pid-connect-equipment", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def pid_add_flow_arrow(self, x, y, rotation=0.0) -> CommandResult:
        return await self._dispatch("pid-add-flow-arrow", {"x": x, "y": y, "rotation": rotation})

    async def pid_add_equipment_tag(self, x, y, tag, description="") -> CommandResult:
        return await self._dispatch("pid-add-equipment-tag", {"x": x, "y": y, "tag": tag, "description": description})

    async def pid_add_line_number(self, x, y, line_num, spec) -> CommandResult:
        return await self._dispatch("pid-add-line-number", {"x": x, "y": y, "line_num": line_num, "spec": spec})

    async def pid_insert_valve(self, x, y, valve_type, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-valve", {"x": x, "y": y, "valve_type": valve_type, "rotation": rotation, "attributes": attributes or {}})

    async def pid_insert_instrument(self, x, y, instrument_type, rotation=0.0, tag_id="", range_value="") -> CommandResult:
        return await self._dispatch("pid-insert-instrument", {"x": x, "y": y, "instrument_type": instrument_type, "rotation": rotation, "tag_id": tag_id, "range_value": range_value})

    async def pid_insert_pump(self, x, y, pump_type, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-pump", {"x": x, "y": y, "pump_type": pump_type, "rotation": rotation, "attributes": attributes or {}})

    async def pid_insert_tank(self, x, y, tank_type, scale=1.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-tank", {"x": x, "y": y, "tank_type": tank_type, "scale": scale, "attributes": attributes or {}})

    # --- View ---

    async def zoom_extents(self) -> CommandResult:
        return await self._dispatch("zoom-extents", {})

    async def zoom_window(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("zoom-window", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def get_screenshot(
        self,
        max_dimension: int | None = SCREENSHOT_MAX_DIMENSION,
        quality: int | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> CommandResult:
        if self._screenshot_provider:
            try:
                data = self._screenshot_provider.capture(
                    max_dimension=max_dimension, quality=quality, region=region
                )
            except ValueError as e:
                return CommandResult(ok=False, error=str(e))
            if data:
                return CommandResult(ok=True, payload=data)
        return CommandResult(ok=False, error="Screenshot capture failed")
