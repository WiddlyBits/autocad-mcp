---
name: autocad-save-verification
description: >-
  Save .dwg in place, SAVEAS to a new path, DXFOUT/DXF export, drawing open/close,
  or verify that any write actually landed. Load before any save, export, or
  file-identity-critical step — including when drawing(save), drawing(save_as_dxf),
  QSAVE, SAVEAS, or DXFOUT returns ok and that ok is about to be believed.
---

# Knowing a write actually landed

**`{"ok":true}` is a report that a call was made, not that a file changed.** Every rule
here comes from a measured case where the two came apart.

## Rule 0: confirm identity before anything destructive

**Before any SAVEAS, DXFOUT, or destructive command: call `(mcp:whoami)`.**
It returns file, folder, tab, and dirty state in one call. `DWGNAME` alone cannot
distinguish two drawings with the same name in different folders.

## Reach for the right verb

| Goal | Call | What it does to the active document |
|---|---|---|
| Save in place | `drawing(save)` → QSAVE | leaves it where it is; `DBMOD` → 0 |
| Save to a new path | `SAVEAS` | **retargets** — you are now editing the new file |
| Export geometry to read back | `drawing(save_as_dxf)` → DXFOUT | leaves the document alone |
| Export to hand over | `drawing(plot_pdf)` | leaves the document alone |

The second row is the one that surprises people. Measured 2026-08-22:
`(vl-cmdf "_.SAVEAS" "" "C:/temp/mcp_probe_c.dwg" "_Y")` left `DWGNAME` reporting
`mcp_probe_c.dwg`. **After a successful SAVEAS you are no longer in the document you
started in** — subsequent edits land in the new file. When SAVEAS *fails*, the original
stays active, which is precisely the signature behind the standing warning that
`drawing(save)` has returned ok while the underlying SAVEAS failed.

So for SAVEAS the check is free and unambiguous: `DWGNAME` afterwards is either the new
basename (it worked) or the old one (it did not).

## The one call that does the whole check

```
(mcp:verify-write "C:/temp/x.dxf" (list "_.DXFOUT" "C:/temp/x.dxf" "16"))
(mcp:verify-write "C:/t/a.dwg"    (list "_.SAVEAS" "" "C:/t/a.dwg" "_Y"))
```

It stashes `FILEDIA`, forces it to 0 so the command takes its path from the argument list
instead of stopping on a dialog nobody can see, runs the command under
`vl-catch-all-apply`, restores `FILEDIA` to **what it was**, and reports `ok` from whether
the file on disk moved — `existed_before`, `exists_after`, `mtime_before`, `mtime_after`,
`size`, `cmdactive`, `dbmod`, and any caught `error`.

Requires `mcp_select.lsp` loaded — `system(operation="init")`.

## Never take the verdict from the caught error

Measured 2026-08-22: `_.DXFOUT` aimed at `C:/temp/no_such_dir_zzz/probe.dxf` returned
**`err no-error`** from `vl-catch-all-apply` and wrote nothing at all. An error that is
never raised cannot be caught. Every SAVEAS probe that day also returned `no-error`,
including the ones that did nothing.

**The evidence is the file.** In order of strength:

1. `mtime` moved, or the file exists where nothing existed before — `mcp:verify-write` does this
2. `size` changed — catches a rewrite fast enough to land inside the same second
3. `DWGNAME` retargeted — SAVEAS only
4. `DBMOD` → 0 — QSAVE only, and see the caveat below

## `DBMOD` says dirty, not saved

Measured: 0 on a clean drawing, 1 immediately after one `entmake`, back to 0 after QSAVE.
It is a reliable answer to *"are there unsaved changes"* — which makes it the right check
**before** an open or a close, and a good confirmation after a QSAVE.

It is not evidence for SAVEAS or DXFOUT. Both left `DBMOD` at 0 in the probes above whether
or not they wrote anything, because a document with nothing pending is already at 0.

## Verify against a different mechanism than the one that wrote

`drawing(save)` reporting ok and `drawing(info)` reporting the document are two answers
from the same path. When a save matters, cross the boundary: check the file with
`mcp:verify-write` or read the bytes back with the DXF probe (see below).

## Geometry export verification

For any DXF export, confirm the write with:

```bash
uv run python -m autocad_mcp.probe_dxf out.dxf          # grid snapshot
uv run python -m autocad_mcp.probe_dxf out.dxf --text   # all strings, with layer and point
```

Run from `~/autocad-mcp`. A DXF that parses successfully is the strongest available proof
that the export is real and complete — parsing happens on this machine, so only the answer
enters the conversation.

## Hard stop after two failed write attempts

**If the same write call fails twice without a structural change between them, stop. Do
not issue a third call.** A write that fails twice the same way is not flaky — something
structural is wrong. Session `8f540404` on 2026-08-25 issued four consecutive
`drawing(save_as_dxf)` calls unchanged, costing ~108 K cache-read each time.

Diagnose in this order, once each:

- Does the **parent directory** exist? This is the measured `no-error` silent-failure case.
- Is `FILEDIA` where you expect? A stray 1 means the command is waiting on a dialog.
- Is `CMDACTIVE` nonzero? A half-finished command eats the next call's input — a bare
  `(vl-cmdf)` flushes it.
- Is the file **locked** — open in another AutoCAD window, or holding a `.dwl`?
- Is this even the document you think it is? `(mcp:whoami)` confirms file, folder, tab,
  and dirty state. autocad-mcp routes to whatever window has UI focus.

## Before anything destructive

Confirm identity first, and confirm it with `(mcp:whoami)` rather than `DWGNAME` alone —
two drawings with the same name in different folders is the ordinary case, and `DWGNAME`
cannot tell them apart. `prefix` is the folder; `dirty` is whether unsaved work would be
lost.

To back out an edit, use `drawing(undo)` — every `mcp_select.lsp` verb opens one named UNDO
group, so one undo reverses the whole batch exactly. Never reverse a move by moving back.
