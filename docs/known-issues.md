# Known issues

Open defects and AutoCAD LT limits, each with its evidence. Resolved items are deleted, not
struck through — git history keeps them. Any claim about LT capability carries a date or says
UNTESTED.

## Open

**The crossing grid treats a solid HATCH as a filled region; AutoCAD does not.** Measured
2026-10-29: `ssget "_C"` hits a solid hatch along its boundary and along its fill's internal
triangulation edges, and misses windows inside it that touch neither. No group code describes
that triangulation, so the Python grid marks the whole interior and the live grid marks a
diagonal pattern. Snapshots of a drawing with a hatch differ in those rows only.

**Live snapshots are not byte-identical to an ezdxf-generated golden.** Two causes, neither
in the bbox code (2026-10-29, `bbox_types.dxf`, every `layer` line identical): AutoCAD writes
`$EXTMIN`/`$EXTMAX` on open, so `extents-header` is set live and `none` from ezdxf; and it
creates the layout's ID 1 viewport on open, which adds an unmeasured entity on layer `0`.

**`mcp:in-model-space` has no call sites.** It wraps a thunk in `CTAB "Model"` and restores the
tab. Use it only when the target is model space; a paper-space rebuild must not be wrapped.

**`expected_doc` is backend-only.** No tool sets the `expected_doc` argument of
`_dispatch`/`execute_lisp`; only backend code can. A tool caller gets the same check by calling
`system(assert_doc)` first, at the cost of one extra round trip.

**`redo`'s refusal gives a reason that is false for reads.** The message says the dispatch
command clears the redo stack. Measured 2026-10-28: `U` (undoing a dispatched `create_line`
group), then a dispatched `entity(count)`, then `REDO` at the command line answered
"Everything has been redone" and the line came back. One "Nothing to redo" had an autosave
between `U` and `REDO`; that cause is not isolated. Still unmeasured: a mutating dispatch in
between, and `REDO` sent through a dispatch.

## Live-verified on LT 2027 (2026-10-28)

- `entity(erase)` through `_.ERASE`, then `drawing(undo)`: count 21 → 20 → 21, and
  `entity(get)` returned the same handle and endpoints. Reads dispatched in between did not
  take the undo step.
- `command_active` with the claim step: `create_line` sent while `LINE` waited for a point
  returned `{"error":"command_active","active":"LINE"}` and drew nothing.
- `timeout_not_dispatched`: with the `UNITS` dialog open, `entity(count)` returned it with
  `may_have_applied: false`. When the dialog closed, both queued triggers (the send and the one
  re-send) ran and printed "No pending commands", so the withdrawal held.
- `drawing(save, path)` to a new `.dwg`: `ok`, no `save_unverified`; the file exists (563,020 bytes,
  `AC1032` header) and `DWGNAME` retargeted to it.
- Bboxes for DIMENSION, HATCH and VIEWPORT (2026-10-29): a production sheet's floating viewport
  on its border layer reads `exact true` with the hand-computed box, and `bbox_types.dxf`'s
  `layer` lines match `bbox_types.snap` byte for byte in Model and Layout1.
- FILEDIA restore (2026-10-29): `FILEDIA` 0, then `drawing(save, path)` to a new `.dwg`, left
  `FILEDIA` at 0 (it used to come back 1); file written (565,152 bytes), `DWGNAME` retargeted, `DBMOD` 0.
- Erase payload (2026-10-29): erase by handle returned `{"entity_type":"LINE","handle":"3D92E","layer":"0"}`,
  and `"last"` returned the CIRCLE it removed. A second erase of the same handle returned
  `ok:false` "Entity not found". This branch's `mcp_dispatch.lsp` has to be loaded by full path:
  `ping` cannot tell it apart from a main-branch copy, because it predates `*mcp-dispatcher-loaded*`.

## LT 2027 limits (measured)

- `entmakex` is unsupported for non-graphical objects (XRECORD and other named objects) in LT;
  graphical MTEXT works (title-block rebuild, 2026-09-09).
- `vlax-get-acad-object` and `vla-get-ActiveDocument` work through `execute_lisp`: the probe
  returned `"Drawing1.dwg"` with no hang (2026-10-08), so the server needs no `vlax-` guard.
- `vl-file-sizep` and `pcacdb_plotter_list` do not exist (2026-09-18). Use `(findfile …)` for existence.
- `acaddoc.lsp` does not autoload, even on the support path with `SECURELOAD=0`; the APPLOAD
  Startup Suite does.
- `DWGNAME` is the filename only, never a path; `DWGPREFIX` holds the directory.
- LT registers no COM class: `GetActiveObject("AutoCAD.Application")` raises
  `-2147221005 Invalid class string` (2026-10-28). `_is_autocad_busy` treats that as not busy,
  so `autocad_busy` never fires on LT; `command_active` covers the busy-prompt case.
- Trigger keystrokes posted while a modal dialog is open are queued, not dropped, and run
  when it closes (2026-10-28).
