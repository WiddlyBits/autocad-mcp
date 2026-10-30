# Known issues

Open defects and LT limits on `main`, each with its evidence. Resolved items are deleted, not
struck through — git history keeps them. Any claim about LT capability carries a date or says
UNTESTED.

## Open

**Entity edit payloads are bare strings.** `move`/`rotate`/`scale` return `"moved"` etc. with
no handle, type or layer; `erase` returns `"erased <h>"`. Callers must confirm with
`entity(get)` or `drawing(info)`. The handle is only read from top-level `entity_id`;
`data.handle` is ignored. The `move`/`rotate`/`scale` fix (`{entity_type, handle, layer}`, both
backends) is on `series/10b-command-claim` and lands when that merges (decided 2026-10-28), for
the same reason as the create-verb `layer` fix below; `erase` stays a bare string there too.
The series carries no `skills/`, so the merge must also update the `entity` line in
`skills/autocad-mcp-workflow/references/tool-reference.md`, which still says `"moved"`.

**`mcp:ent-bbox-data` has no branch for DIMENSION, HATCH or VIEWPORT**, so `bbox-by-layer`
reports `bbox none` for them. VIEWPORT is the type on the ECSI `border line 02` layer (found
2026-10-28). Crossing-mode grid maps still draw them, because `ssget` sees geometry the bbox code
cannot measure. Fixed on the series (`series/07-probes`, "fix(probes): measure DIMENSION, HATCH
and VIEWPORT bboxes", LISP and Python, live-checked on LT 2027); main takes it with the series
merge rather than a port.

**ezdxf `create_*` payloads have no `layer`.** They return `{"entity_type", "handle"}`; the
fix (every create verb reports its layer, both backends) is on the `series/10b-command-claim`
stack and lands when that merges (decided 2026-10-28), so `mcp_dispatch.lsp` takes the
change once instead of from two parents.

**`drawing(plot_pdf)` does not check the PDF on disk, and paper is fixed to ARCH D.**
`mcp-cmd-drawing-plot-pdf` returns `{path, dbmod_before, dbmod_after, dbmod_restored}` once
`-PLOT` finishes; it never runs `findfile` on the output, so a failed plot still returns ok.
Confirm with `(findfile path)` or the `autocad-save-verification` skill. The `-PLOT` sequence
hard-codes `DWG To PDF.pc3`, `ARCH D (36.00 x 24.00 Inches)` and landscape; other sheet sizes
need a code change. Shipped this way in v3.2.0 (2026-10-30).

**`expected_doc` is backend-only.** No tool sets the `expected_doc` argument of
`_dispatch`/`execute_lisp`; only backend code can. A tool caller gets the same check by calling
`system(assert_doc)` first, at the cost of one extra round trip.

## Not yet run live (UNTESTED on LT 2027)

Covered by unit tests only. All four were run live on 2026-10-28 against the
`series/10c-docs` dispatcher, whose `entity-erase`, `undo` and `drawing-save` handlers differ
from this branch's, so they still count as untested here:

- `entity(erase)` through `_.ERASE`, then `U` restoring the entity.
- `redo` is refused and points at the command line; the old reason (dispatch clears the redo
  stack) was dropped as unproven. On the series
  dispatcher a dispatched `entity(count)` between `U` and `REDO` did not clear it
  ("Everything has been redone"); this branch's dispatch path was not measured.
- `timeout_not_dispatched`. `command_active` was verified live on 2026-10-14.
- Saving to a new `.dwg` without a false `save_unverified`.

## LT 2027 limits (measured)

- `entmakex` is unsupported for non-graphical objects (XRECORD and other named objects) in LT;
  graphical MTEXT works (title-block rebuild, 2026-09-09).
- `vlax-get-acad-object` and `vla-get-ActiveDocument` work through `execute_lisp`: the probe
  returned `"Drawing1.dwg"` with no hang (2026-10-08), so the server-side `vlax-` guard was removed.
- `vl-file-sizep` and `pcacdb_plotter_list` do not exist (2026-09-18). Use `(findfile …)` for existence.
- `acaddoc.lsp` does not autoload, even on the support path with `SECURELOAD=0`; the APPLOAD
  Startup Suite does.
- `DWGNAME` is the filename only, never a path; `(mcp:whoami)` adds `DWGPREFIX`.
- LT registers no COM class: `GetActiveObject("AutoCAD.Application")` raises
  `-2147221005 Invalid class string` (2026-10-28). `_is_autocad_busy` treats that as not busy,
  so `autocad_busy` never fires on LT; `command_active` covers the busy-prompt case.
- Trigger keystrokes posted while a modal dialog is open are queued, not dropped, and run
  when it closes (2026-10-28).
