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
| `(mcp:sel)` returns nothing | nothing was handed over | ask for `HS` + Enter (Rule 1). Don't ask him to re-select in the hope the grip selection survives — it never does |
| A verb reports a high `skipped` count | the set isn't what you assumed — non-LINE entities in a line operation, chunked MTEXT, text without the substring | `(mcp:sel-dump)` and read the types before re-running |
| Dispatch timeout / "Screenshot capture failed" after an AutoCAD restart | stale window handle — normally re-acquired transparently (`IsWindow()` check before each dispatch) | `system(operation="init")`; if every dispatch returns `autocad_not_found` immediately, AutoCAD is not running |
| `execute_lisp` times out but screenshots still work | an ActiveX object-*creation* call hung the dispatcher | `activex-and-lisp-limits.md` recovery: Esc in AutoCAD, then `drawing(info)` before anything else |
| First `execute_lisp` after the user clicks another tab returns `timeout_mutating` | intermittent after a focus change (2026-10-08: 2 of 3 tab switches) | `system(status)` — `idle` plus the expected `preflight.dwg` means the dispatcher is fine. Read-only call: retry. Mutating: check state first. With dispatcher v2 (`(list *mcp-dispatcher-version* (mcp:whoami))` → first element 2; loads via APPLOAD of `mcp_dispatch.lsp` or a restart) an unclaimed trigger comes back as `timeout_not_dispatched` instead, which is safe to re-send. The `init` payload never shows the dispatcher version |
| `no function definition: MCP:WHOAMI` (or any `mcp:` verb) on a document just opened or switched to | `init` loaded the libraries into the *previous* document only | `system(operation="init")` once on this document. Nothing ran — safe to re-send |
| `execute_lisp` payload is an opaque `<<ccr:…,262B>>` | result replaced by a reference in transit; nothing here resolves it (seen once, 2026-10-08) | Re-ask for less: `(list (getvar "DWGNAME") (getvar "DWGPREFIX") (getvar "DBMOD"))` |
| `drawing(operation="save_as")` → "Unknown drawing operation" | that operation doesn't exist | `drawing(operation="save", data={"path": …})` |
| `entity(get)` returns `bad argument type: stringp nil` | the handle was erased (`handent` still returns an ename; `entget` is nil) | treat as "gone"; confirm with a `drawing(info)` count diff |
| `entity(count)` on a layer you just "deleted" isn't zero, or a purge removes more/less than expected | `CLAYER` or a nested block definition | `activex-and-lisp-limits.md` |
| A probe answers plausibly but with the behaviour you just fixed | old definitions still resident | `system(operation="init")` re-loads probes and select; `mcp_dispatch.lsp` needs APPLOAD or a restart |
| The same write fails twice | something structural, not flakiness | stop; the `autocad-save-verification` skill's diagnosis order |
