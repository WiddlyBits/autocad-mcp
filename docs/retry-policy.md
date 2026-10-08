# Retry Policy

This document states the fork's explicit retry policy for IPC errors. The rules here are **never
auto-applied by the server** — the MCP server returns the error code and leaves retry decisions to
the caller. This is intentional: the server cannot know whether a geometry-creating command
committed a partial result before timing out.

Cross-reference [docs/file-ipc-error-model.md](file-ipc-error-model.md) for full payload shapes
and origin points for every error code mentioned below.

---

## Decision table

| Error | May retry? | Condition |
|---|---|---|
| `autocad_not_found` | Yes | After AutoCAD launches or becomes visible. |
| `autocad_busy` | Yes | After a short delay; recheck `IsQuiescent` before sending. |
| Plain timeout (non-mutating) | Yes | After the timeout interval; no mutation was attempted. |
| `WRONG-DOC` / `wrong_doc` | Yes | After switching focus to the correct document. |
| `UNRESOLVED-REF` | Yes | After rebuilding the selection set. |
| `command_active` | Yes | After the active command completes (same as `autocad_busy`). |
| `timeout_not_dispatched` | Yes | Already retried once by the dispatcher; check focus / `(mcp:whoami)` first. |
| `timeout_mutating` | **Never** | Command may have been applied; inspect drawing state first. |
| `save_unverified` | **Never** | LISP reported success; inspect `drawing(info)` before re-saving. |
| `vlax_not_supported_in_lt` | **Never** | Rewrite the LISP to remove `vlax-` dependency. |

---

## Why geometry-creating calls are excluded from retry

When a mutating IPC call times out — the Python side gave up waiting for the result file — the
LISP reactor may have already executed the command and simply failed to write the result file in
time. Re-issuing the call would create a duplicate entity with no way to distinguish it from a
legitimate retry. The error code `timeout_mutating` is the caller's signal that idempotent retry
is **unsafe**. Geometry-creating commands are the most common source of this condition.

This mirrors the IMessageFilter pattern in COM-based AutoCAD automation (khs0927): retrying
rejected/busy-calls is safe; retrying calls that may have committed is not.

### `_MUTATING_COMMANDS` — the fork's IMessageFilter equivalent

`_MUTATING_COMMANDS` (defined in `src/autocad_mcp/backends/file_ipc.py`) is the frozenset that
classifies which IPC command strings are treated as potentially non-idempotent. Any command in this
set receives `timeout_mutating` (with `may_have_applied: true`) instead of a plain timeout. The
set spans five categories:

| Category | Commands |
|---|---|
| Geometry creation | `create-line`, `create-circle`, `create-polyline`, `create-rectangle`, `create-arc`, `create-ellipse`, `create-mtext`, `create-hatch`, `create-text`, all `create-dimension-*`, `create-leader` |
| Entity mutation | `entity-erase`, `entity-move`, `entity-copy`, `entity-rotate`, `entity-scale`, `entity-mirror`, `entity-offset`, `entity-array`, `entity-fillet`, `entity-chamfer` |
| Block/layer changes | `block-insert`, `block-insert-with-attributes`, `block-update-attribute`, `block-define`, all `layer-*` |
| Drawing I/O | `drawing-save`, `drawing-save-as-dxf`, `drawing-plot-pdf`, `drawing-purge` |
| Freehand LISP | `execute-lisp` (treated as mutating because content is opaque) |
| P&ID | All `pid-*` commands |

**Policy: geometry-creating commands are never safe to auto-retry on timeout.** After a
`timeout_mutating` from any `create-*` command, inspect drawing state with `entity(count)` or
`drawing(info)` before re-issuing. A duplicate entity created by a blind retry cannot be
distinguished from a legitimate second call and has no automatic rollback path.

---

## Safe-to-retry codes (detail)

### `autocad_busy`

No IPC file was written. AutoCAD's `Document.IsQuiescent` returned false, meaning a command was
mid-execution. Retry after a short polling interval; `IsQuiescent` will return true once the
command completes. Recommended: exponential backoff starting at 250 ms, cap at 2 s, give up after
10 s total.

### `autocad_not_found`

No IPC file was written. The AutoCAD window handle was absent or invalid. Retry after confirming
AutoCAD is running and the LISP reactor has loaded.

### Plain timeout (non-mutating)

The request_id is encoded in the error string (`"Timeout waiting for result (request_id=…)"`).
Because the call was non-mutating, the same request can be re-sent after the timeout interval.
Note that the free-form string format means callers that match on `error` must handle both this
form and the structured `timeout_mutating` code.

### `WRONG-DOC` / `wrong_doc`

The command body did not execute: the Python-layer (`wrong_doc`) or LISP-layer (`WRONG-DOC`) guard
caught a document mismatch before the command ran. Switch focus to the correct document and retry.
The LISP-layer variant provides `actual_dwg`/`actual_tab` directly in the payload — no re-probe
needed. See [docs/file-ipc-error-model.md](file-ipc-error-model.md) for payload shapes.

---

## Never-retry codes (detail)

### `timeout_mutating`

The IPC channel timed out on a call in the `_MUTATING_COMMANDS` set. The payload includes
`may_have_applied: true` and `request_id`. Before re-issuing, use an out-of-band read —
`entity(get)`, `entity(count)`, `drawing(info)` — to determine whether the effect landed.

### `save_unverified`

The LISP reactor returned `ok:true` but the post-hoc file check found either that the target path
does not exist or that its `mtime` has not advanced. Re-issuing the save blindly risks overwriting
a partially written file. Confirm `drawing(info).filename` matches the intended path and check the
filesystem state before retrying.

---

## Relationship to `assert_doc`

`assert_doc()` is a Python-layer pre-batch guard that fires before any IPC file is written. When
it returns `wrong_doc`, no command in the pending batch has been sent, so the entire batch is safe
to retry after focus is corrected. This is distinct from a mid-batch `WRONG-DOC` (LISP-layer),
which means the guard fired after dispatch but before execution — also safe to retry, but only for
the individual call that returned the error, not the calls that preceded it in the same batch.
