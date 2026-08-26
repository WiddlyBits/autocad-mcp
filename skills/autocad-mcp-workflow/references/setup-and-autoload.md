# Setup, autoload, and MCP server registration

Environment this was verified against: AutoCAD LT 2027 (release R33), Windows,
autocad-mcp using the `file_ipc` backend.

## Getting mcp_dispatch.lsp to load automatically

AutoCAD LT 2027 does **not** auto-load `acaddoc.lsp`, even when it's correctly placed
on the support path with `SECURELOAD=0`. This was tested and confirmed dead — don't
spend time re-diagnosing it or re-creating an `acaddoc.lsp` for this purpose.

The mechanism that actually works is the **APPLOAD Startup Suite**, which loads its
listed applications into every drawing on open:

1. Run `APPLOAD` in AutoCAD.
2. Click **Startup Suite → Contents...**
3. Click **Add...** and select `C:\Users\Gianni\autocad-mcp\lisp-code\mcp_dispatch.lsp`.

**Only `mcp_dispatch.lsp` needs to be in the Startup Suite.** `mcp_probes.lsp` and
`mcp_select.lsp` are loaded by `system(operation="init")`, which sends `(load ...)` with
the absolute paths the MCP server already knows from `config.LISP_DIR`. This is
deliberate: the Startup Suite is manual and invisible, and a library missing from it
fails by being *absent* rather than by erroring — the probe ladder silently degrades to
hand-rolled AutoLISP and nothing says so. That is the most likely explanation for the
2026-08-22 sessions, where the Startup Suite carried only the dispatcher and no probe was
called all day. Run `init` at the start of a session, and check the `preflight` block it
returns.

This persists in the registry
(`HKCU:\SOFTWARE\Autodesk\AutoCAD LT\R33\ACADLT-A101:409\Profiles\<<Unnamed Profile>>\Dialogs\Appload\Startup`
as `NumStartup`/`1Startup`), so it survives restarts. Reading those registry values is
a fast way to confirm it's still configured without opening the AutoCAD UI.

Also worth knowing: there's a first-party `AutoCAD-MCP-Server-2027.bundle`
ApplicationPlugin already present at
`C:\Program Files\Autodesk\ApplicationPlugins\AutoCAD-MCP-Server-2027.bundle`, but its
`PackageContents.xml` is version-gated to `SeriesMin/Max = R26.0` (the 2026 line),
while this LT install is R33 — so AutoCAD skips it entirely and it never loads. Don't
edit that signed package to widen the range; it's not the thing to fix.

## After an AutoCAD restart

The MCP server caches a window handle (hwnd) for the AutoCAD process. A restart
invalidates it. Symptoms: dispatch timeouts and "Screenshot capture failed" even
though `mcp_dispatch.lsp` is loaded and the Startup Suite entry is intact. Fix: call
`system(operation="init")` to re-acquire the handle before doing anything else.

## After editing the .lsp files — the stale-definition trap

The Startup Suite loads `mcp_dispatch.lsp` **at AutoCAD startup**. Editing it on disk
changes nothing in a session that is already running: the old definitions stay resident,
and AutoCAD gives no indication that what is loaded and what is on disk have diverged.

For `mcp_probes.lsp` and `mcp_select.lsp` this is already handled —
`system(operation="init")` re-loads them every time it is called, precisely so that an
edit on disk can be made live without restarting AutoCAD. The trap below still applies in
full to `mcp_dispatch.lsp`, which cannot load itself.

This is worse than a plain staleness bug because **the failure usually looks like a
result.** A probe you just fixed answers with the old behaviour, and the answer is
well-formed. Nothing errors. It is the same shape as the ezdxf silent-fallback trap:
a false pass on untested code.

**So whenever a session follows work that touched the .lsp files, prove the new code is
live before trusting anything.** The cheap tell is calling something that only exists in
the new version — a name that isn't loaded returns *"no function definition"* rather than a
plausible answer:

```
execute_lisp "(mcp:grid-map-in 24 16 \"Model\")"
```

If that errors on the name, the running AutoCAD has the old file. Fix: re-run `APPLOAD` on
both .lsp files, or restart AutoCAD — and after a restart, `system(operation="init")` per
the section above. Don't proceed against stale definitions; a validation run against the
code you were trying to replace is worse than no validation run.

## Registering/re-registering the MCP server itself in PowerShell

If you ever need to run `claude mcp add`/`remove` for autocad-mcp (or anything else)
from PowerShell on this machine, the plain `claude` command mis-parses arguments that
come after the `--` separator. Confirmed failures:

```
claude mcp add autocad-mcp -e AUTOCAD_MCP_BACKEND=auto -- "...\python.exe" -m autocad_mcp
# error: unknown option '-m'

claude mcp add --% autocad-mcp -e AUTOCAD_MCP_BACKEND=auto -- "...\python.exe" -m autocad_mcp
# error: unknown option '--%'
```

The fix is to resolve and invoke the real `claude.exe` binary directly instead of the
bare `claude` command:

```powershell
$claudeExe = "C:\Users\Gianni\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
& $claudeExe mcp add autocad-mcp -s user -e AUTOCAD_MCP_BACKEND=auto -- "C:\Users\Gianni\autocad-mcp\.venv\Scripts\python.exe" -m autocad_mcp
```

Use `-s user` scope so the server is available across all projects, not just
whichever directory it was registered from. Verify with
`& $claudeExe mcp list` / `& $claudeExe mcp get autocad-mcp` — a healthy registration
shows `√ Connected`.
