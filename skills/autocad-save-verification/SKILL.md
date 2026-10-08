---
name: autocad-save-verification
description: >-
  Save .dwg in place, SAVEAS to a new path, create a .dwg from a .dwt template,
  plot_pdf, DXFOUT/DXF export, drawing open/close, or verify that any write
  actually landed. Load before any save, plot, export, template, or
  file-identity-critical step — including when drawing(save), drawing(plot_pdf),
  drawing(save_as_dxf), QSAVE, SAVEAS, or DXFOUT returns ok and that ok is about
  to be believed, and before the final save after plotting.
---

# Knowing a write actually landed

**`{"ok":true}` is a report that a call was made, not that a file changed.** Every rule
here comes from a measured case where the two came apart.

## Hard stop: `(mcp:whoami)` before any SAVEAS, DXFOUT, open, close, or destructive command

It returns file, folder (`prefix`), tab, and dirty state in one call. `DWGNAME` alone cannot
tell two drawings with the same name in different folders apart — the ordinary case here.
Commands go to whichever window holds focus (workflow skill, Rule 2), so confirm *which*
document before anything that cannot be undone.

## Reach for the right verb

| Goal | Call | What it does to the active document |
|---|---|---|
| Save in place | `drawing(save)` → QSAVE | leaves it where it is; `DBMOD` → 0 |
| Save to a new path | `drawing(save, path)` → SAVEAS | **retargets** — you are now editing the new file |
| Export geometry to read back | `drawing(save_as_dxf)` → DXFOUT | leaves the document alone |
| Export to hand over | `drawing(plot_pdf)` | leaves the document in place, may dirty it (below) |

**After a successful SAVEAS you are no longer in the document you started in** (measured
2026-08-22: `DWGNAME` reported the new basename). When SAVEAS *fails*, the original stays
active — the signature behind `drawing(save)` returning ok while the SAVEAS failed. So for
SAVEAS the check is free: `DWGNAME` afterwards is the new basename (it worked) or the old
one (it did not).

### Creating a drawing from a `.dwt`

SAVEAS's first answer is the file format, and `""` keeps the current one — Template, from an
open `.dwt`, which wrote nothing (2026-10-08, errno 4). `drawing(save, path)` now passes
`"2018"` whenever `DWGNAME` ends in `.dwt`; a `.dwg` still passes `""`, so an older drawing is
never upgraded behind your back. Verified live 2026-10-08: file created, `DWGNAME`
retargeted, template mtime unchanged.

Do the SAVEAS **before any edit** so the template can never take the changes. Then
`(mcp:whoami)` should name the new file and the template's mtime should be unchanged.
Repeat from the template tab for each new drawing.

### Plot first, save last

The `plot_pdf` handler's viewport-layer entmod dirties the drawing. The payload reports
`dbmod_before`/`dbmod_after`, plus `dbmod_restored` when `acad-push-dbmod` exists to undo
it — **LT 2027 has it** (2026-10-08: `dbmod_before` 0, `dbmod_after` 0,
`dbmod_restored:true`), and a `hint` appears whenever the plot left the drawing dirty.
End a build as **plot, then `drawing(save)`, then confirm `DBMOD` is 0**; any later
edit-then-replot needs a save again.

## The one call that does the whole check

```
(mcp:verify-write "C:/temp/x.dxf" (list "_.DXFOUT" "C:/temp/x.dxf" "16"))
(mcp:verify-write "C:/t/a.dwg"    (list "_.SAVEAS" "" "C:/t/a.dwg" "_Y"))
(mcp:verify-write "C:/t/b.dwg"    (list "_.SAVEAS" "2018" "C:/t/b.dwg" "_Y"))  ; from a .dwt
```

It forces `FILEDIA` to 0 so the command takes its path from the argument list instead of
stopping on a dialog nobody can see, runs it under `vl-catch-all-apply`, restores `FILEDIA`
to **what it was**, and reports `ok` from whether the file on disk moved —
`existed_before`, `exists_after`, `mtime_before`, `mtime_after`, `size`, `cmdactive`,
`dbmod`, and any caught `error`. Requires `system(operation="init")`.

The server's own check behind `save_unverified` compares the file's `(mtime, size)` before
and after, so a newly created file counts. (AutoCAD stamps `.dwg` mtime to the whole
second; an older server compared it to the call's start time and false-alarmed on new files.)

## Never take the verdict from the caught error

Measured 2026-08-22: `_.DXFOUT` aimed at a nonexistent directory returned **`err no-error`**
from `vl-catch-all-apply` and wrote nothing. Every SAVEAS probe that day also returned
`no-error`, including the ones that did nothing. **The evidence is the file**, in order of
strength:

1. `mtime` moved, or the file exists where nothing existed before — `mcp:verify-write`
2. `size` changed — catches a rewrite inside the same second
3. `DWGNAME` retargeted — SAVEAS only
4. `DBMOD` → 0 — QSAVE only

`DBMOD` says dirty, not saved: 0 on a clean drawing, 1 after one `entmake`, 0 after QSAVE.
It is the right check **before** an open or close and after a QSAVE. It is not evidence
for SAVEAS or DXFOUT — a document with nothing pending is already at 0 either way.

When a save matters, cross the boundary: `drawing(save)` ok and `drawing(info)` are two
answers from the same path. Check the file with `mcp:verify-write`, or read a DXF back:

```bash
uv run python -m autocad_mcp.probe_dxf out.dxf --text   # from ~/autocad-mcp
```

A DXF that parses is the strongest proof the export is real and complete.

## Hard stop after two failed write attempts

**If the same write fails twice without a structural change between them, stop. Do not
issue a third call.** Session `8f540404` (2026-08-25) issued four identical
`drawing(save_as_dxf)` calls at ~108 K cache-read each. Diagnose in this order, once each:

- Does the **parent directory** exist? This is the measured `no-error` silent failure.
- Is `FILEDIA` 1? The command is waiting on a dialog.
- Is `CMDACTIVE` nonzero? A half-finished command eats the next call's input — a bare
  `(vl-cmdf)` flushes it.
- Is the file **locked** — open in another AutoCAD window, or holding a `.dwl`?
- Is this the document you think it is? `(mcp:whoami)`.
