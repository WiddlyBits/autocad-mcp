# Known issues

Open defects and LT limits on `main`, each with its evidence. Resolved items are deleted, not
struck through — git history keeps them. Any claim about LT capability carries a date or says
UNTESTED.

## Open

**`entity(get)` on an erased handle raises instead of reporting not-found.** `handent` returns
an ename for an erased entity and `mcp-cmd-entity-get` (`mcp_dispatch.lsp`) only tests
`(not ent)`, so `entget` is nil and the `strcat` fails: `bad argument type: stringp nil`
(field-verified 2026-09-18). `mcp-cmd-entity-erase` already tests `(entget ent)`; get should too.

**`command_active` never fires for IPC calls.** The dispatcher checks `CMDNAMES`
(`mcp_dispatch.lsp`, `mcp-dispatch-command`), but from the dispatch context it reads `""` even
while the user has `LINE` waiting for input (2026-09-18: `create_line` during an active `LINE`
returned ok and may have fed its points to the user's command). Python's `autocad_busy`
(`IsQuiescent`) is the real protection.

**Entity edit payloads are bare strings.** `move`/`rotate`/`scale` return `"moved"` etc. with
no handle, type or layer; `erase` returns `"erased <h>"`. Callers must confirm with
`entity(get)` or `drawing(info)`. The handle is only read from top-level `entity_id`;
`data.handle` is ignored.

**`mcp:ent-bbox-data` has no branch for some entity types** (DIMENSION, HATCH, and the type on
the ECSI `border line 02` layer, 2026-08-15), so `bbox-by-layer` reports `bbox none` for them.
Crossing-mode grid maps still draw them, because `ssget` sees geometry the bbox code cannot measure.

**`mcp:in-model-space` has no call sites.** It wraps an `entmakex` in `CTAB "Model"` and restores
the tab. Use it only when the target is model space; a paper-space rebuild must not be wrapped.

## LT 2027 limits (measured)

- `entmakex` is unsupported for non-graphical objects (XRECORD and other named objects) in LT;
  graphical MTEXT works (title-block rebuild, 2026-09-09).
- `vlax-get-acad-object` and `vla-get-ActiveDocument` work through `execute_lisp`: the probe
  returned `"Drawing1.dwg"` with no hang (2026-10-08), so the server-side `vlax-` guard was removed.
- `vl-file-sizep` and `pcacdb_plotter_list` do not exist (2026-09-18). Use `(findfile …)` for existence.
- `acaddoc.lsp` does not autoload, even on the support path with `SECURELOAD=0`; the APPLOAD
  Startup Suite does.
- `DWGNAME` is the filename only, never a path; `(mcp:whoami)` adds `DWGPREFIX`.
