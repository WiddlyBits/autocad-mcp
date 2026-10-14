"""Read the AutoCAD command line without going through the dispatcher.

Measured 2026-10-13 on LT 2027 with LINE waiting for a point:

- The dispatcher refuses every read (`command_active`), so (getvar "LASTPROMPT")
  is unreachable exactly when it is wanted.
- LOGFILEON is written per line but lags: it showed "Specify first point:" and
  never the click or the pending "Specify next point". Unusable for a live prompt.
- The command line is WPF, so WM_GETTEXT on its Win32 children returns nothing.
- UI Automation works. The scrollback is a FlowDocumentScrollViewer with a
  TextPattern; the live prompt is a row of TextBlocks in the CLI PaletteSet.

Read-only: nothing here sends keystrokes or touches the drawing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys

from autocad_mcp.backends.base import CommandResult

DEFAULT_LINES = 10
MAX_LINES = 200
TIMEOUT_S = 15

# {hwnd} is the only substitution; it is an int from find_autocad_window().
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$root = $AE::FromHandle([IntPtr]{hwnd})
$flow = New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, 'FlowDocumentScrollViewer')
$e = $root.FindFirst($TS::Descendants, $flow)
$history = ''
if ($e) {
  $history = $e.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(-1)
}
$set = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, 'CLI PaletteSet')
$cli = $root.FindFirst($TS::Descendants, $set)
$live = @()
if ($cli) {
  $txt = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Text)
  foreach ($t in $cli.FindAll($TS::Descendants, $txt)) { $live += $t.Current.Name }
}
@{ found_history = [bool]$e; found_palette = [bool]$cli; history = $history; live = $live } | ConvertTo-Json -Compress
"""


def build_command(hwnd: int) -> list[str]:
    encoded = base64.b64encode(_SCRIPT.replace("{hwnd}", str(int(hwnd))).encode("utf-16-le")).decode()
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def parse_output(raw: str, n: int) -> dict:
    """Shape the PowerShell JSON into `{lines, prompt, live}`.

    `lines` is the last `n` non-blank scrollback lines. `live` is the raw
    TextBlock strings of the live prompt row, in order; `prompt` is those
    joined, which is what the user would read ("LINE Specify next point or [ ]: ").
    """
    data = json.loads(raw)
    history = [ln.rstrip() for ln in (data.get("history") or "").splitlines() if ln.strip()]
    live = data.get("live") or []
    if isinstance(live, str):  # ConvertTo-Json collapses a one-element array
        live = [live]
    prompt = "".join(live).strip()
    out = {"lines": history[-n:], "prompt": prompt, "live": live, "history_total": len(history)}
    # The option text sits in an unnamed WPF Button that UIA cannot read
    # (2026-10-14: no Name, HelpText, or children), so the live row shows "[]".
    # AutoCAD echoes the full prompt into the scrollback once anything is
    # typed, so recover it from there when it is available.
    m = re.search(r"^(.*?)\[\s*\](.*)$", prompt)
    if m:
        head = m.group(1).strip()
        head = head[len(live[0]):].strip() if live and head.startswith(live[0]) else head
        for ln in reversed(history):
            r = re.match(re.escape(head) + r"\s*(\[[^\]]+\])", ln) if head else None
            if r:
                out["prompt"] = prompt.replace("[]", r.group(1), 1)
                return out
        out["options_hidden"] = True
    return out


async def read_command_line(hwnd: int | None, n: int = DEFAULT_LINES) -> CommandResult:
    if sys.platform != "win32":
        return CommandResult(ok=False, error="command_line needs Windows UI Automation")
    if not hwnd:
        return CommandResult(ok=False, error="No AutoCAD window handle; run system(init)")
    n = max(1, min(int(n), MAX_LINES))
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_command(hwnd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return CommandResult(ok=False, error=f"command_line_timeout after {TIMEOUT_S}s")
    except OSError as exc:
        return CommandResult(ok=False, error=f"command_line_spawn_failed: {exc}")
    if proc.returncode != 0:
        return CommandResult(ok=False, error="command_line_failed: " + err.decode("utf-8", "replace").strip()[:300])
    try:
        payload = parse_output(out.decode("utf-8", "replace").strip(), n)
    except (ValueError, AttributeError) as exc:
        return CommandResult(ok=False, error=f"command_line_unparsable: {exc}")
    return CommandResult(ok=True, payload=payload)
