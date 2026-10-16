# Troubleshooting

Read this when something is actually wrong. It lives here rather than in `SKILL.md`
because at most two or three of these fire in a session, and `SKILL.md` is re-read by
every request in it.

## Retry rule

The server re-sends once on its own when a command was never claimed
(`timeout_not_dispatched`), because that cannot double-apply. Every other retry is yours:
a read-only call can just be re-sent; a mutating one only after checking state
(`entity(count)`, `entity(get)`, `drawing(info)`). Developer detail: `docs/error-model.md`.

## Error payloads

Exact signatures — match these without re-reading the LSP files:

- **`WRONG-DOC`** — `{"ok":false,"error":"WRONG-DOC","wanted_dwg":…,"wanted_tab":…,"actual_dwg":…,"actual_tab":…}`.
  The body did not run. Retarget to `actual_dwg`/`actual_tab`; no re-probe needed.
- **`UNRESOLVED-REF`** — `{"ok":false,"error":"UNRESOLVED-REF","ref":…}`. The reference
  coordinate was not in the set; check the `(mcp:sel-dump)` coordinates before retrying.
- **Probe truncation** — `"truncated":true,"truncated_reason":"max_entities"|"time"`. Narrow
  the scan: the `-in` form with a specific layer, or a tighter region.
- **sel-dump truncation** — `"truncated":true` when count > `*mcp-max-selection*` (200).
  Re-select a tighter set.
- **`timeout_mutating`**, `may_have_applied: true` — may have run. Check state before re-sending.

## Symptoms

| Symptom | Likely cause | Fix |
|---|---|---|
| `preflight` says `select: false` or `probes: false` (or `reactor_fn: false`) | libraries not loaded — normal before `init` in every session, or after a restart | `system(operation="init")` |
| `system(status)` says `backend: "ezdxf"` with AutoCAD open | `auto` lost the startup race | `system(operation="init")`, then status again; stop if it is still not `file_ipc` |
| `system(status)` says dispatch not loaded | `mcp_dispatch.lsp` isn't in the *current* document | `setup-and-autoload.md` — APPLOAD Startup Suite, not `acaddoc.lsp` |
| Any call returns `command_active`, or a fresh `init` says `command_active` / "not loaded" | a command is waiting for input (LINE, a prompt, a dialog); the dispatcher is loaded but refuses every read | `system(operation="command_line")` shows the prompt and `document`; answer it or Esc, then retry. On a fresh server only `command_line` works until the command ends |
| `system` payload has `dispatcher.stale: true` | `mcp_dispatch.lsp` changed on disk after the resident copy was loaded; its definitions are old and may answer plausibly | `(load "C:/Users/Gianni/autocad-mcp/lisp-code/mcp_dispatch.lsp")` in the active document, then `system(init)`; `stale` must read `false` |
| `dispatcher.stale: null` with "unknown … reload once" | the resident dispatcher predates the load-time field, so freshness cannot be judged | load `mcp_dispatch.lsp` once; later checks are exact |
| `multi_window` block in `command_line` / `status` | more than one AutoCAD window is open; the call acts on `using`, which is the window the server cached, not necessarily the front one | close the extra windows, or compare `document` with the window being worked in before trusting the read or any edit |
| `command_line` shows `options_hidden: true` with `option_count: N` | the `[...]` option names are unnamed WPF buttons UIA cannot read; N is how many there are (measured: `-LAYER` 21, `TEXT` 2) | the names appear in the scrollback once the prompt is answered or cancelled; while waiting, read the command's documented options |
| `entity` move / rotate / scale returns `ok:false` "left the entity unchanged" | the command ran but the entity data did not change (wrong handle in a locked or other-space context, or a refused command) | `entity(get)` the handle and check layer lock and space before retrying; do not re-send blindly |
| `entity` fillet / chamfer returns `ok:false` "changed neither entity and added nothing" | AutoCAD refused it (radius or distances too large for the segments, lines that cannot meet) and raised inside the command | shrink the radius / distances or pick segments that meet; `command_line` history has AutoCAD's own wording. Parallel lines do not fail: fillet joins them with a semicircle |
| `drawing(undo)` returns `ok:false` "UNDO changed nothing" | no entity or layer state differs after UNDO: nothing to undo, or the last step changed only a view, variable or block table | `drawing(info)` to see current state; do not undo again hoping for an effect, a second UNDO reverses an earlier step. `verified:false` means over 5000 entities, so no comparison was possible |
| `drawing(purge)` returns `removed: 0` | nothing was purgeable, or purge was blocked by references; before/after table counts (layers, blocks, linetypes, text styles, dim styles) are in the payload | compare the counts; a nonzero `removed` is the only proof something was purged |
| `(mcp:sel)` returns nothing | nothing was handed over | ask for `HS` + Enter (Rule 1). Don't ask him to re-select in the hope the grip selection survives — it never does |
| A verb reports a high `skipped` count | the set isn't what you assumed — non-LINE entities in a line operation, chunked MTEXT, text without the substring | `(mcp:sel-dump)` and read the types before re-running |
| Dispatch timeout / "Screenshot capture failed" after an AutoCAD restart | stale window handle — normally re-acquired transparently (`IsWindow()` check before each dispatch) | `system(operation="init")`; if every dispatch returns `autocad_not_found` immediately, AutoCAD is not running |
| `execute_lisp` times out but screenshots still work | an ActiveX object-*creation* call hung the dispatcher | `activex-and-lisp-limits.md` recovery: Esc in AutoCAD, then `drawing(info)` before anything else |
| First `execute_lisp` after the user clicks another tab returns `timeout_mutating` | intermittent after a focus change (2026-10-08: 2 of 3 tab switches) | `system(status)` — `idle` plus the expected `preflight.dwg` means the dispatcher is fine. Read-only call: retry. Mutating: check state first. With dispatcher v2 (`(list *mcp-dispatcher-version* (mcp:whoami))` → first element 2; loads via APPLOAD of `mcp_dispatch.lsp` or a restart) an unclaimed trigger comes back as `timeout_not_dispatched` instead, which is safe to re-send. The `init` payload never shows the dispatcher version |
| `no function definition: MCP:WHOAMI` (or any `mcp:` verb) on a document just opened or switched to | `init` loaded the libraries into the *previous* document only | `system(operation="init")` once on this document. Nothing ran — safe to re-send |
| `start_tab_active` from any call or `init` | AutoCAD is running with the Start tab in front, so there is no drawing to dispatch to; the error lists the open drawings. Before 2026-10-16 this silently became the ezdxf backend (`system(status)` → `backend: ezdxf`, entity_count 0, then `Not supported on this backend`) | `system(operation="switch_doc", data={"dwg": "<one of the listed titles>"})`. Nothing ran — safe to re-send |
| `switch_doc` → `no_such_document` | the title is not exactly one open tab (titles are bare filenames, matched case-insensitively) | pick a title from `payload.open` |
| `switch_doc` → `switch_not_confirmed` or `dwgname_mismatch` | the MDI tab did not move, or it moved but AutoCAD's document manager still reports another `DWGNAME` | do not edit. `system(status)` for `preflight.dwg`; if it still disagrees, ask Gianni to click the tab |
| `execute_lisp` payload is an opaque `<<ccr:…,262B>>` | result replaced by a reference in transit; nothing here resolves it (seen once, 2026-10-08) | Re-ask for less: `(list (getvar "DWGNAME") (getvar "DWGPREFIX") (getvar "DBMOD"))` |
| `drawing(operation="save_as")` → "Unknown drawing operation" | that operation doesn't exist | `drawing(operation="save", data={"path": …})` |
| `entity(get)` returns `Entity not found: <h>` | the handle never existed or was erased | treat as "gone"; confirm with a `drawing(info)` count diff |
| `entity(get)` returns `bad argument type: stringp nil` | the handle was erased and the document holds a `mcp_dispatch.lsp` from before 2026-10-08 | same as above; reopen the drawing so the Startup Suite loads the current dispatcher |
| `entity(count)` on a layer you just "deleted" isn't zero, or a purge removes more/less than expected | `CLAYER` or a nested block definition | `activex-and-lisp-limits.md` |
| A probe answers plausibly but with the behaviour you just fixed | old definitions still resident | `system(operation="init")` re-loads probes and select; `mcp_dispatch.lsp` needs APPLOAD or a restart |
| The same write fails twice | something structural, not flakiness | stop; the `autocad-save-verification` skill's diagnosis order |
