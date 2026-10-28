# Known issues

Open defects and AutoCAD LT limits, each with its evidence. Resolved items are deleted, not
struck through — git history keeps them. Any claim about LT capability carries a date or says
UNTESTED.

## Open

**Entity edit payloads are bare strings.** `move`/`rotate`/`scale` return `"moved"` etc. with
no handle, type or layer; `erase` returns `"erased <h>"`. Callers must confirm with
`entity(get)` or `drawing(info)`. The handle is only read from top-level `entity_id`;
`data.handle` is ignored.

**`mcp:ent-bbox-data` has no branch for some entity types** (DIMENSION, HATCH, and the type on
one production drawing's border layer, 2026-08-15), so `bbox-by-layer` reports `bbox none` for
them. Crossing-mode grid maps still draw them, because `ssget` sees geometry the bbox code cannot
measure.

**`mcp:in-model-space` has no call sites.** It wraps a thunk in `CTAB "Model"` and restores the
tab. Use it only when the target is model space; a paper-space rebuild must not be wrapped.

**`expected_doc` is backend-only.** No tool sets the `expected_doc` argument of
`_dispatch`/`execute_lisp`; only backend code can. A tool caller gets the same check by calling
`system(assert_doc)` first, at the cost of one extra round trip.

## Not yet run live (UNTESTED on LT 2027)

Covered by unit tests only:

- `entity(erase)` through `_.ERASE`, then `U` restoring the entity.
- `redo` is refused because the dispatch command is said to clear the redo stack. Nobody has
  measured that.
- `timeout_not_dispatched` and `autocad_busy` from this code. `command_active` was verified live
  on 2026-10-14, before the claim step existed.
- Saving to a new `.dwg` without a false `save_unverified`.

## LT 2027 limits (measured)

- `entmakex` is unsupported for non-graphical objects (XRECORD and other named objects) in LT;
  graphical MTEXT works (title-block rebuild, 2026-09-09).
- `vlax-get-acad-object` and `vla-get-ActiveDocument` work through `execute_lisp`: the probe
  returned `"Drawing1.dwg"` with no hang (2026-10-08), so the server needs no `vlax-` guard.
- `vl-file-sizep` and `pcacdb_plotter_list` do not exist (2026-09-18). Use `(findfile …)` for existence.
- `acaddoc.lsp` does not autoload, even on the support path with `SECURELOAD=0`; the APPLOAD
  Startup Suite does.
- `DWGNAME` is the filename only, never a path; `DWGPREFIX` holds the directory.
