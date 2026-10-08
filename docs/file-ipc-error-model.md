# File-IPC Error Model

## IPC Protocol Overview

The MCP server communicates with AutoCAD through a file-based IPC channel: Python writes a JSON
request file to a temp directory, AutoCAD's LISP reactor picks it up, executes the command, and
writes a JSON result file back. Errors can originate at four distinct points in this pipeline:

- **Python-dispatch** — before any file is written; AutoCAD is unreachable or the request cannot
  be safely sent (e.g., AutoCAD is busy or a known-incompatible feature is requested on LT).
- **Python-verify** — after the LISP layer has returned `ok:true`; a post-hoc check finds that
  the side effect (file save, document change) did not actually land.
- **LISP-dispatch** — inside the reactor, before the command runs; a guard condition is violated
  (active command, wrong document).
- **LISP-guard** — per-call guards embedded in individual LISP routines, independent of the
  reactor's top-level dispatch check.

In the result envelope, `ok: false` is always accompanied by `error: <code>` (a string). Some
codes carry additional payload fields described below. The retry policies below are from the
caller's perspective; `never-retry` means the command must be assumed potentially applied.

---

## Error Codes

### `autocad_not_found`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | The AutoCAD window handle (`hwnd`) is absent from the initial search, or `IsWindow()` returns false on a previously cached handle. |
| **Payload** | None beyond `ok`/`error`. |
| **Retry policy** | `safe-to-retry` — no command was sent; retry after AutoCAD launches or its window becomes visible. |
| **Notes** | Fires on both initial connection failure and mid-session window loss (e.g., AutoCAD closed while a request was in flight). |

---

### `autocad_busy`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | `_is_autocad_busy()` returns true (`IsQuiescent` is False) at the time the request is about to be sent. |
| **Payload** | None beyond `ok`/`error`. |
| **Retry policy** | `transient-retry-with-recheck` — safe to retry after a short delay; AutoCAD may become quiescent once the current command completes. |
| **Notes** | Added in commit 70fa56b. Prevents writes to the IPC drop directory while AutoCAD is mid-command, which would corrupt the queue. |

---

### `timeout_mutating`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | The IPC channel timed out waiting for a result file on a call classified as `_MUTATING_COMMANDS`. |
| **Payload** | `may_have_applied: true`, `request_id: <str>` |
| **Retry policy** | `never-retry` — the command may have been applied to the drawing without producing a result file. Caller must inspect drawing state before deciding to retry or roll back. |
| **Notes** | The `may_have_applied` flag is the caller's signal that idempotent retry is unsafe. Always pair with an out-of-band state check (e.g., `entity(get)`, `drawing(info)`) before re-issuing. |

---

### `timeout_not_dispatched`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | The IPC channel timed out, and the command file was still unclaimed, so Python could delete it. Requires dispatcher v2 (ping reports `"dispatcher":2`). That dispatcher renames `cmd_<id>` to `run_<id>` before it runs anything. |
| **Payload** | `may_have_applied: false`, `request_id: <str>` |
| **Retry policy** | `safe-to-retry`. The server already re-sends once on its own (`_dispatch_unlocked`), so this code reaches the caller only after two unclaimed attempts. The usual cause is that the trigger keystroke went to a window that was not at the command prompt (a tab switch, or a dialog). |
| **Notes** | Takes precedence over `timeout_mutating`. With a v1 dispatcher, behaviour is unchanged. |

---

### *(plain string timeout)*

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | The IPC channel timed out on a non-mutating call. |
| **Payload** | `error` is a human-readable string of the form `"Timeout waiting for result (request_id=…)"` — not a structured code. |
| **Retry policy** | `safe-to-retry` — no mutation was attempted; the request_id can be re-sent after the timeout interval. |
| **Notes** | Distinguished from `timeout_mutating` solely by the command classification, not by a separate code field. Callers that pattern-match on `error` must handle both the structured code and this free-form string. |

---

### `save_unverified`

| Field | Value |
|---|---|
| **Origin** | Python-verify |
| **Trigger** | After a `drawing(save*)` or `plot_pdf` call returns `ok:true` from the LISP layer, the post-hoc file check finds either that the target path does not exist or that its `mtime` has not changed. |
| **Payload** | `detail: <str>` (human-readable reason), `path: <str>` (the path that was checked) |
| **Retry policy** | `never-retry` — the LISP layer reported success, so re-issuing the save blindly risks overwriting a partially written file. Confirm drawing state with `drawing(info)` first. |
| **Notes** | The underlying SAVEAS may have silently targeted a different file (e.g., focus routed to the wrong document). Always verify `drawing(info).filename` matches the intended path before concluding the save is lost. |

---

### `vlax_not_supported_in_lt`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch |
| **Trigger** | An `execute_lisp` call whose body contains the substring `vlax-` is issued while `is_lt` is True (AutoCAD LT is the active backend). |
| **Payload** | None beyond `ok`/`error`. |
| **Retry policy** | `not-applicable` — the LISP must be rewritten to remove the `vlax-` dependency before it can run on LT. Retrying the same payload will always fail. |
| **Notes** | AutoCAD LT does not expose the Visual LISP ActiveX (`vlax-*`) interface. This check is a best-effort skip that fires before the request reaches the IPC channel, providing a cleaner error than an opaque LISP fault. |

---

### `wrong_doc`

| Field | Value |
|---|---|
| **Origin** | Python-dispatch (assert_doc) |
| **Trigger** | `assert_doc()` finds that the currently active AutoCAD document name does not match the name passed by the caller. |
| **Payload** | `expected: <str>`, `actual: <str>` |
| **Retry policy** | `safe-to-retry` — no command was sent; resolve focus to the correct document and retry. |
| **Notes** | Lowercase `wrong_doc` is the Python-layer guard. The LISP-layer equivalent is `WRONG-DOC` (uppercase), which fires inside the reactor after focus drift between dispatch and execution. Both indicate the same root cause but originate at different pipeline stages. |

---

### `command_active` *(LISP-dispatch)*

| Field | Value |
|---|---|
| **Origin** | LISP-dispatch |
| **Trigger** | `CMDNAMES` is non-empty when the LISP reactor attempts to dispatch the command. |
| **Payload** | None beyond `ok`/`error` in the JSON result file. |
| **Retry policy** | `transient-retry-with-recheck` — wait for the active command to complete (equivalent to `autocad_busy` at the Python layer) then retry. |
| **Notes** | **Known issue A-4:** this check is inert in the IPC reactor context because the reactor itself runs inside an AutoCAD command context. In practice the Python-layer `autocad_busy` check catches this condition earlier. This code is retained for completeness and for any direct LISP callers that bypass the Python layer. |

---

### `WRONG-DOC` *(LISP-guard)*

| Field | Value |
|---|---|
| **Origin** | LISP-guard |
| **Trigger** | `mcp:guard` detects a document name mismatch between the per-call guard header and the document that is actually active at LISP execution time. |
| **Payload** | `wanted_dwg: <str>`, `wanted_tab: <str>`, `actual_dwg: <str>`, `actual_tab: <str>` |
| **Retry policy** | `safe-to-retry` — the command body did not execute; switch focus to the correct document and retry. |
| **Notes** | This is the legacy per-call guard, separate from the Python-layer `assert_doc()`. It fires when focus drifts between the Python dispatch and the LISP execution, a window that is small but non-zero. Use `actual_dwg`/`actual_tab` directly from the payload; no re-probe is needed. |

---

### `UNRESOLVED-REF` *(LISP-guard)*

| Field | Value |
|---|---|
| **Origin** | LISP-guard |
| **Trigger** | A `mcp:sel` reference coordinate is not found in the current selection set during LISP execution. |
| **Payload** | `ref: <coordinate>` (the coordinate that could not be resolved) |
| **Retry policy** | `safe-to-retry` — the command did not execute on any entity; rebuild the selection set and retry. |
| **Notes** | Use `(mcp:sel-dump)` to inspect the selection set coordinates before retrying. The mismatch is usually caused by stale coordinates from a prior selection that was modified or cleared between calls. |
