# Troubleshooting

Read this when something is actually wrong. It lives here rather than in `SKILL.md`
because at most two or three of these fire in a session, and `SKILL.md` is re-read by
every request in it.

| Symptom | Likely cause | Fix |
|---|---|---|
| `preflight` says `select: false` or `probes: false` | libraries not loaded in this document | `system(operation="init")` |
| `system(status)` says dispatch not loaded | `mcp_dispatch.lsp` isn't in the *current* document | `setup-and-autoload.md` — APPLOAD Startup Suite, not `acaddoc.lsp` |
| `(mcp:sel)` returns nothing | nothing was handed over | ask for `HS` + Enter (Rule 1). Don't guess, and don't ask him to re-select in the hope the grip selection survives this time — it never does |
| A verb returns `WRONG-DOC` | focus is on another document or tab | the payload names the actual `dwg`/`ctab` — use those, don't spend a round trip re-probing |
| A verb reports a high `skipped` count | the set isn't what you assumed — non-LINE entities in a line operation, chunked MTEXT, text that didn't contain the substring | `(mcp:sel-dump)` and read the types before re-running |
| Dispatch timeout / "Screenshot capture failed" right after an AutoCAD restart | cached window handle went stale | `system(operation="init")` re-acquires it |
| `execute_lisp` times out but screenshots still work | an ActiveX object-*creation* call hung the dispatcher | `activex-and-lisp-limits.md` for the recovery steps — don't just retry, a blind retry can double-apply |
| `drawing(operation="save_as")` errors "Unknown drawing operation" | that operation name doesn't exist | use `drawing(operation="save", data={"path": ...})` |
| `entity(count)` on a layer you just "deleted" isn't zero, or a purge removes more/less than expected | likely a `CLAYER` or nested-block-definition issue, not a real problem | `activex-and-lisp-limits.md` |
| A probe answers plausibly but with the behaviour you just fixed | old definitions still resident | `system(operation="init")` re-loads probes and select; `mcp_dispatch.lsp` needs APPLOAD or a restart |
| `claude mcp add ...` fails with "unknown option '-m'" or "'--%'" in PowerShell | bare `claude` mis-parses args after `--` | `setup-and-autoload.md` for the `claude.exe`-direct invocation |
