# Switching the active drawing in AutoCAD LT without a click

2026-10-16 · AutoCAD LT 2027 · result: `system(operation="switch_doc")`

## Problem

autocad-mcp talks to AutoCAD LT by writing a command file and typing a fixed trigger into
the command line of whichever drawing is active. Every edit therefore lands in the *active*
document, and the server had no way to change which document that was:

| Route | Why it fails on LT |
|---|---|
| COM automation (`Documents.Item(n).Activate()` from Python) | LT ships no COM server. |
| `(command "_.OPEN" ...)` from LISP | Refused: it would tear down the document whose LISP namespace is running the dispatcher. Returns no status, so it used to report `ok`. |
| `(vla-Activate doc)` | Returns normally; `DWGNAME` does not move. `vla-get-ActiveDocument` agrees with the call, AutoCAD's command line does not. |

So the agent's only option was to ask the user to click a tab, and multi-drawing work cost a
human round trip per switch.

## Hypotheses tried

**1. Ctrl+Tab via synthetic input — disproved on the Start tab.**
From a drawing, one Ctrl+Tab moved to the next tab (A → Start). From Start, five presses left
the frame title unchanged. The keystroke script ([`ctrltab.ps1`](2026-10-16-document-switching/ctrltab.ps1))
guards every press on `GetForegroundWindow() == AutoCAD` so no key can land in another app;
the presses were reaching AutoCAD and doing nothing.

**2. An Alt tap was needed to release the foreground lock — disproved.**
AutoCAD was already foreground for the failing presses, so the Alt tap was not on the path.
(A bare Alt inside AutoCAD also opens ribbon keytips, which is why the script only taps it when
another app is in front.)

A physical Ctrl+Tab on Start *did* advance. The difference between real and synthetic input
there is untested; the fix below makes it moot.

## The measurement that found the cause

[`acadwin_diag.ps1`](2026-10-16-document-switching/acadwin_diag.ps1) reads the GUI thread's
focus with `GetGUIThreadInfo` instead of assuming it:

| Active tab | `hwndFocus` class | Text |
|---|---|---|
| a drawing | AutoCAD's own MFC/WPF controls | — |
| Start | `Chrome_WidgetWin_1` | `AutoCAD AppHome 27.612.1` |

The Start tab is an embedded Chromium page, and it owns keyboard focus. Chromium consumes
Ctrl+Tab itself, so the key never reaches the MFC accelerator table that implements tab
cycling. Any keystroke-based switch is hostage to whatever has focus.

The same script enumerated the frame's window tree and showed what to use instead. The frame
(`AfxMDIFrame140u`) is a classic MFC MDI application: it has an `MDIClient` child, and every
open drawing is an MDI child whose window text is the bare filename, with one more child
titled `Start`.

## The fix

Send `WM_MDIACTIVATE (0x0222)` to the `MDIClient` with the target child, then read
`WM_MDIGETACTIVE (0x0229)` back. No keystroke, no focus requirement, so the Chromium widget
is irrelevant. Prototype: [`mdi_activate.ps1`](2026-10-16-document-switching/mdi_activate.ps1).

| Check after `WM_MDIACTIVATE` | Before | After |
|---|---|---|
| `WM_MDIGETACTIVE` | `Start` | `B.dwg` |
| frame title | names Start | names `B.dwg` |
| `(getvar "DWGNAME")` after re-init | — | `B.dwg` |

That last row is the one that matters. `vla-Activate` also moved an API's notion of the
active document; only `DWGNAME`, read through the dispatcher in the newly active drawing,
shows that AutoCAD's document manager followed.

`system(operation="switch_doc", data={"dwg": "B.dwg"})` in
[`doc_switch.py`](../../src/autocad_mcp/doc_switch.py) productionizes it:

1. Find the frame by window class plus an `MDIClient` child — not by a `.dwg` title, which
   the frame lacks while Start is in front.
2. Enumerate direct MDI children; require **exactly one** title match (case-insensitive; a full
   path is reduced to its filename). Otherwise fail and list the open titles. Match by title,
   never by a cached handle. `Start` is refused as a target.
3. `WM_MDIACTIVATE`, then poll `WM_MDIGETACTIVE` up to 2 s; `switch_not_confirmed` if it
   never moves.
4. Drop the backend and re-detect, exactly as `system(init)` does, so the window handle,
   dispatcher ping and helper libraries all belong to the new drawing.
5. Return `ok` only if `DWGNAME` equals the requested title; otherwise `dwgname_mismatch`.

## A second bug the investigation exposed

While Start was active, a call that re-initialized the backend came back
`Not supported on this backend`, and `system(status)` reported `backend: ezdxf`,
`entity_count: 0`, `save_path: null`. The server had silently switched from driving AutoCAD
to editing an empty in-memory drawing.

Cause, in [`config.py`](../../src/autocad_mcp/config.py): with `AUTOCAD_MCP_BACKEND=auto`,
detection looks for a window whose title contains `.dwg` or `Drawing`. With Start in front
the frame title has neither, so detection concluded "no AutoCAD" and chose the headless
backend. Any re-detection (`system(init)`, a server respawn) in that state flipped it.

Fix: when no drawing window is found but an AutoCAD frame exists, detection raises
`start_tab_active` with the list of open drawings and the `switch_doc` call to make. ezdxf is
still chosen when AutoCAD is genuinely absent.

## Verification

Unit tests ([`tests/test_doc_switch.py`](../../tests/test_doc_switch.py), 18 tests, fake
`win32gui` so they run on the Linux CI runner): title matching, full-path and case handling,
direct-children-only enumeration, Start refused, unknown title lists the open drawings,
unconfirmed switch fails, already-active sends nothing, source contains no keystroke APIs,
Start-tab detection raises instead of returning ezdxf, absent AutoCAD still returns ezdxf, and
the server op fails on a `DWGNAME` mismatch.

Live, read-only ([`scripts/verify_switch_doc.py`](../../scripts/verify_switch_doc.py)):
A → B → A, `DWGNAME` read independently of the `switch_doc` payload at each hop.

Run 2026-10-16, AutoCAD LT 2027, two unsaved drawings open (`B.dwg` active at start), exit 0:

| Hop | Requested | Before | After | `DWGNAME` (payload) | `DWGNAME` (independent) | Result |
|---|---|---|---|---|---|---|
| 1 | A.dwg | B.dwg | A.dwg | A.dwg | A.dwg | PASS |
| 2 | B.dwg | A.dwg | B.dwg | B.dwg | B.dwg | PASS |
| 3 | A.dwg | B.dwg | A.dwg | A.dwg | A.dwg | PASS |

Start-tab guard, same session: with the Start tab in front, backend detection raised `start_tab_active` (listing both open drawings) instead of falling through to ezdxf. The re-run then passed all three hops, hop 1 switching from `Start` to `A.dwg` directly, exit 0.

## Takeaways

- **Measure focus, don't infer it.** Two plausible keystroke hypotheses were tested and
  discarded before one `GetGUIThreadInfo` call showed where the keys were going.
- **Prefer the window manager's own protocol to simulated input.** MDI messages do not care
  about focus, foreground locks, or embedded browsers.
- **Verify against the system of record.** Three APIs agreed a switch had happened when it had
  not; `DWGNAME` from inside the target document is the only reading the edit path depends on.
- **A fallback that changes what you are talking to must be loud.** "AutoCAD present, no
  drawing active" and "AutoCAD absent" are different states and now produce different results.
