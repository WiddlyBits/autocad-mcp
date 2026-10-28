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

**`assert_doc` does not escape its argument.** The expected name is spliced into
`(mcp:assert-doc "<name>")` as-is, so a name containing `"` or `\` breaks the LISP form.
`DWGNAME` is a bare filename, so this needs an unusual name to hit.

## LT 2027 limits (measured)

- `entmakex` is unsupported for non-graphical objects (XRECORD and other named objects) in LT;
  graphical MTEXT works (title-block rebuild, 2026-09-09).
- `vlax-get-acad-object` and `vla-get-ActiveDocument` work through `execute_lisp`: the probe
  returned `"Drawing1.dwg"` with no hang (2026-10-08), so the server needs no `vlax-` guard.
- `vl-file-sizep` and `pcacdb_plotter_list` do not exist (2026-09-18). Use `(findfile …)` for existence.
- `acaddoc.lsp` does not autoload, even on the support path with `SECURELOAD=0`; the APPLOAD
  Startup Suite does.
- `DWGNAME` is the filename only, never a path; `DWGPREFIX` holds the directory.
