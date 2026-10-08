# File-IPC error model and retry policy

Developer reference. The agent-facing summary (payload signatures and what to do) lives in
`skills/autocad-mcp-workflow/references/troubleshooting.md`; keep the two consistent.

## IPC protocol overview

The MCP server communicates with AutoCAD through a file-based IPC channel: Python writes a JSON
request file to a temp directory, AutoCAD's LISP dispatcher picks it up, executes the command,
and writes a JSON result file back. Errors originate at four points:

- **Python-dispatch** — before any file is written; AutoCAD is unreachable or the request cannot
  be safely sent (AutoCAD busy).
- **Python-verify** — after the LISP layer returned `ok:true`; a post-hoc check finds the side
  effect (a file write) did not land.
- **LISP-dispatch** — inside the dispatcher, before the command runs.
- **LISP-guard** — per-call guards inside individual LISP routines.

In the result envelope, `ok: false` always comes with `error: <code>`. The code dialects are not
yet unified: snake_case from Python, UPPER-KEBAB from LISP, plus one free-form timeout string,
so `wrong_doc` and `WRONG-DOC` name the same condition. Planned: one envelope
`{"ok":false,"error":<snake_code>,"hint":…}` with LISP codes mapped at the Python boundary.

## Retry policy

**The server applies exactly one retry itself:** it re-sends once on `timeout_not_dispatched`,
because a command that never started cannot double-apply (`_dispatch_unlocked` in
`backends/file_ipc.py`). Every other decision is the caller's, since the server cannot know
whether a geometry-creating command committed a partial result before timing out.

| Error | May retry? | Condition |
|---|---|---|
| `autocad_not_found` | Yes | After AutoCAD launches or becomes visible. |
| `autocad_busy` | Yes | After a short delay (backoff from 250 ms, cap 2 s, give up at 10 s). |
| Plain timeout (non-mutating) | Yes | No mutation was attempted. |
| `wrong_doc` / `WRONG-DOC` | Yes | After switching focus to the correct document. |
| `UNRESOLVED-REF` | Yes | After rebuilding the selection set. |
| `command_active` | Yes | After the active command completes. |
| `timeout_not_dispatched` | Yes | The server already re-sent once; check focus / `(mcp:whoami)` first. |
| `timeout_mutating` | **Never blindly** | May have been applied; inspect drawing state first. |
| `save_unverified` | **Never blindly** | Inspect the file and `drawing(info)` before re-saving. |

This mirrors the IMessageFilter pattern in COM-based AutoCAD automation: retrying rejected or
busy calls is safe; retrying calls that may have committed is not.

### `_MUTATING_COMMANDS`

The frozenset in `backends/file_ipc.py` that classifies which IPC commands are potentially
non-idempotent. A timeout on any of them returns `timeout_mutating` (`may_have_applied: true`)
instead of a plain timeout:

| Category | Commands |
|---|---|
| Geometry creation | `create-line`, `-circle`, `-polyline`, `-rectangle`, `-arc`, `-ellipse`, `-mtext`, `-hatch`, `-text`, all `create-dimension-*`, `create-leader` |
| Entity mutation | `entity-erase`, `-move`, `-copy`, `-rotate`, `-scale`, `-mirror`, `-offset`, `-array`, `-fillet`, `-chamfer` |
| Block/layer changes | `block-insert`, `block-insert-with-attributes`, `block-update-attribute`, `block-define`, all `layer-*` |
| Drawing I/O | `drawing-save`, `drawing-save-as-dxf`, `drawing-plot-pdf`, `drawing-purge` |
| Freehand LISP | `execute-lisp` (opaque, so treated as mutating) |
| P&ID LISP | `pid-*` — the `pid` MCP tool is pruned from `server.py`; the dispatcher commands remain |

A duplicate entity created by a blind retry cannot be told apart from a legitimate second call
and has no automatic rollback.

### `assert_doc` vs `WRONG-DOC`

`assert_doc()` is a Python pre-batch guard that fires before any IPC file is written: on
`wrong_doc` no command in the batch was sent, so the whole batch is safe to retry. A mid-batch
`WRONG-DOC` (LISP) means only that one call was refused — the calls before it in the batch ran.

## Error codes

### `autocad_not_found`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | The AutoCAD window handle is absent from the initial search, or `IsWindow()` returns false on a cached handle. |
| **Payload** | None beyond `ok`/`error`. |
| **Notes** | Fires on initial connection failure and mid-session window loss. |

### `autocad_busy`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | `_is_autocad_busy()` (COM `ActiveDocument.IsQuiescent` is False) just before sending. |
| **Payload** | None beyond `ok`/`error`. |
| **Notes** | Added in 70fa56b. Best-effort: returns not-busy on any COM exception or off Windows. |

### `timeout_mutating`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | Timed out waiting for a result file on a call in `_MUTATING_COMMANDS`. |
| **Payload** | `may_have_applied: true`, `request_id: <str>` |
| **Notes** | Pair with an out-of-band read (`entity(get)`, `entity(count)`, `drawing(info)`) before re-issuing. |

### `timeout_not_dispatched`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | Timed out, and the command file was still unclaimed, so Python could delete it. Requires dispatcher v2 (`*mcp-dispatcher-version*` 2), which renames `cmd_<id>` to `run_<id>` before running anything. |
| **Payload** | `may_have_applied: false`, `request_id: <str>` |
| **Notes** | Reaches the caller only after the server's one automatic re-send also went unclaimed. Usual cause: the trigger keystroke went to a window not at the command prompt (tab switch, dialog). Takes precedence over `timeout_mutating`. With a v1 dispatcher, behaviour is unchanged. |

### *(plain string timeout)*

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | Timed out on a non-mutating call. |
| **Payload** | `error` is the free-form string `"Timeout waiting for result (request_id=…)"`. |
| **Notes** | Distinguished from `timeout_mutating` only by command classification. Callers matching on `error` must handle both forms. |

### `save_unverified`

| Field | Value |
|---|---|
| **Origin** | Python-verify |
| **Trigger** | After `drawing(save, path)`, `save_as_dxf` or `plot_pdf` returns `ok:true`, the target's `(mtime, size)` is unchanged from before the call, or the file does not exist. |
| **Payload** | `detail`: `"file not found"` or `"timestamp unchanged"`; `path` |
| **Notes** | Comparing the file's own before/after state (not `time.time()`) is what lets a newly created file pass — AutoCAD stamps `.dwg` mtime to the whole second. A SAVEAS may have targeted another document; check `DWGNAME` before concluding the save is lost. |

### `wrong_doc`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch (`assert_doc`) |
| **Trigger** | The active document name does not match the caller's `expected`. |
| **Payload** | `expected: <str>`, `actual: <str>` |
| **Notes** | Same root cause as LISP `WRONG-DOC`, caught earlier. |

### `command_active`

| Field | Value |
|---|---|
| **Origin** | LISP-dispatch |
| **Trigger** | `CMDNAMES` is non-empty when the dispatcher runs. |
| **Payload** | `active: <cmdnames>` |
| **Notes** | Inert for IPC calls — see `known-issues.md`. `autocad_busy` catches the condition earlier. |

### `WRONG-DOC`

| Field | Value |
|---|---|
| **Origin** | LISP-guard (`mcp:guard`) |
| **Trigger** | The document suffix or tab passed to the guard does not match `DWGNAME`/`CTAB` at execution time. |
| **Payload** | `wanted_dwg`, `wanted_tab`, `actual_dwg`, `actual_tab` |
| **Notes** | The body did not run. Use `actual_dwg`/`actual_tab` directly; no re-probe needed. |

### `UNRESOLVED-REF`

| Field | Value |
|---|---|
| **Origin** | LISP-guard |
| **Trigger** | A reference coordinate passed to a selection verb is not in the set. |
| **Payload** | `ref: <coordinate>` |
| **Notes** | No entity was changed. Usually stale coordinates from an earlier selection. |
