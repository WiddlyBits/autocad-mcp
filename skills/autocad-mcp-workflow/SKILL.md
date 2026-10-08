---
name: autocad-mcp-workflow
description: >-
  Align text, batch LISP, DXF probe, selection handoff, screenshot planning, or
  troubleshoot autocad-mcp (tools named mcp__autocad-mcp__ system, drawing,
  entity, layer, view, block, annotation). Load before any mcp__autocad-mcp__ call
  and whenever the user refers to a selection ("the lines I have selected", "move these
  circles", "duplicate these labels") — grip selection does not survive the MCP link and
  Rule 1 is how it gets handed over.
---

# Working in AutoCAD LT via autocad-mcp

**Every `execute_lisp` is one API request that re-reads the whole current context.** Round-trip
count is the cost that dominates this workflow, not screenshots. Each rule below exists to remove
round trips or the mistakes that cause them. Evidence: `references/why-this-skill-exists.md`.

## Rule 0: one preflight call, before anything else

`system(operation="init")` loads `mcp_probes.lsp` and `mcp_select.lsp` into the current
document by absolute path and returns a `preflight` block:

```
dispatch, probes, select  — which .lsp libraries are live
dwg, ctab, tilemode       — which document and space are in front
pickfirst                 — whether grip selection is even enabled
```

`system(operation="status")` returns the same block without re-loading. Run `init` at the
start of a session, after any AutoCAD restart or `.lsp` edit, and on the first visit to each
document — it loads into the current document only, and re-loading is also the fix for stale
definitions that answer plausibly.

**Stop unless `system(status)` says `backend: "file_ipc"`.** `auto` can lose a startup race
and pick ezdxf, which answers `ok:true` against an empty in-memory drawing while AutoCAD sits
open. If `probes` or `select` comes back false, say so and stop too — without them the session
is hand-rolled AutoLISP.

## Rule 1: the selection contract

**Gianni's grip selection cannot be pulled. He has to hand it over.** The dispatcher works
by typing `(c:mcp-dispatch)` at the command line, and starting a command is exactly what
discards the implied selection. `(ssget "_I")` is *always* nil here — never call it, and
never ask him to re-select in the hope that this time it survives.

1. **Ask for the handoff, with the exact keystrokes.** Preferred: with the objects
   selected, type `HS` then Enter — it prints how many it stashed. Fallback if
   `mcp_select.lsp` is not loaded: type `SELECT`, Enter, then Enter again, which fills the
   previous-selection set. Never make him ask which command you meant.
2. **`(mcp:sel-dump)` — once.** One payload gives count, source, document, space, the
   union bbox, and per entity: handle, type, layer, space, and type-correct geometry
   including the literal contents of any text. Do not follow it with a second probe of the
   same set.
3. **Echo before you mutate.** `(mcp:sel-show)` zooms to the set and highlights it, at
   zero token cost. Say what you counted ("16 TEXT on layer LABEL, model space").
   **If the `count` does not match what the user described, halt** and ask — a set that is
   wrong before the edit is wrong after it.
4. **Operate by handle list.** Handles survive an intervening command, the dispatch
   boundary, and the ~128 open-selection-set ceiling. `(ssget "_P")` survives none of
   those.

The verbs each take `(handles dwg tab ...)` and report what they changed:

```
(mcp:line-uniform (mcp:sel) "DI-02" "Model" 10.8 "right")   ; anchor: left/right/top/bottom
(mcp:align-axis   (mcp:sel) "DI-02" "Model" "X" "topmost")  ; ref: a number or *most
(mcp:sel-move     (mcp:sel) "DI-02" "Model" -9.5697 0.0)
(mcp:text-sub     (mcp:sel) "DI-03" "Model" "Bank 1" "Bank 3")
```

Anything they do not cover is hand-written AutoLISP — but read the dump first and write
patterns against strings you have actually read. A `wcmatch` pattern written from memory
matched 0 of 152 labels.

## Rule 2: name the document and the space on every edit

**autocad-mcp routes to whatever window holds UI focus**, not the one you meant, and
`vla-Add`/`vla-Open` do not reliably move focus even when `vla-get-ActiveDocument` says they
did. `mcp:guard` makes the wrong-document edit impossible rather than merely checked-for: pass
the document suffix (`"DI-02"`) and the tab (`"Model"` or a layout name) and the verb refuses
with `WRONG-DOC` instead of editing. **Every LISP snippet that mutates the drawing — named
verb or hand-written — must begin with the guard:**

```lisp
(if (not (mcp:guard "DI-02" "Model"))
  (mcp:wrong-doc "DI-02" "Model")
  (progn
    ; ... edit goes here
  ))
```

When the user genuinely must click a tab, give the whole sequence up front rather than one
round trip per tab.

Space is not a detail, and it bites edits as hard as probes:

| Operation | Follows | Trap |
|---|---|---|
| `ssget "_X"` | **neither** — spans model *and* paper | a "move everything" swept the border too |
| `ssget "_C"`, `PASTECLIP`, `ZOOM` | `CTAB` | pasted 16 labels into the `11x17` tab |
| `mcp:bbox-by-layer-in`, `mcp:grid-map-in`, `mcp:text-dump-in` | their argument | a bare call means model space |
| `entmake`, `entmakex` | `CTAB` | 8 MTEXT rebuilt from model-space originals landed in paper space |

Filter `ssget "_X"` with `(cons 410 "Model")` — or `(67 . 0)` model / `(67 . 1)` paper — or
call `(mcp:space-ss "Model")`. **On a layout tab, always use the `-in` form**
(`(mcp:grid-map-in 24 16 "Layout1")`); the bare form silently scans model space.

**`entmakex` follows `CTAB`, not the space of the entity you copied.** Wrap it in
`(mcp:in-model-space (lambda () (entmakex d)))` only when the target is model space — a
paper-space rebuild (the title block) must stay on its tab. **Verify with group `67`/`410` on
the result, not with text, width and entity count** — all three read clean on an entity that
is in the wrong space. See `references/activex-and-lisp-limits.md`.

**To back out an edit, use `drawing(undo)`** — every verb opens one named UNDO group
(`mcp:undo-begin`/`mcp:undo-end`; use them in hand-written edits too), so one undo reverses
the whole batch. Never reverse a move by moving back: a re-move with a slightly different
offset leaves a drawing that is not the one you started with.

**Load the `autocad-save-verification` skill before any save, SAVEAS, `plot_pdf`, DXF export,
or new drawing from a `.dwt`.** Those writes can report ok and still not land.

## Rule 3: climb the capture ladder

A screenshot costs `ceil(width/28) * ceil(height/28)` visual tokens — dimensions only —
and is re-read by every request that follows it. Escalate only when the cheaper rung
genuinely cannot answer.

| Rung | Call | Answers | Cost |
|---|---|---|---|
| L0 | `system(status)` preflight | which backend, document, space, libraries | ~80 tok |
| L0 | `(mcp:whoami)` | which file, folder, tab, unsaved changes — **the** identity probe | ~80 tok |
| L0 | `drawing(info)`, `entity(count)` | did the edit apply | ~50 tok |
| L1 | `(mcp:sel-dump)`, `entity(get)` | what is selected; where is it; what does it say | ~100–400 tok |
| L2 | `(mcp:bbox-by-layer-in "Model")` | what occupies which region | ~200–600 tok |
| L3 | `(mcp:grid-map-in 24 16 "Model")` | does the layout read correctly, coarse | ~300–800 tok |
| L3b | `drawing(save_as_dxf)` + `python -m autocad_mcp.probe_dxf` | anything exactly, incl. all text | **~0 tok** |
| L4 | `view(get_screenshot, region=[l,t,r,b])` | this specific detail, visually | ~270 tok |
| L5 | `view(get_screenshot)` full window | overall read, match-to-reference-photo | 1,334 tok @1280 |

`entity(get)` returns real geometry and the contents of TEXT/MTEXT/ATTDEF, so **never
screenshot to read a label**. Angles come back in degrees.

**L3b is the rung for "what is actually in this drawing".** Parsing happens on this machine,
so only the answer enters the conversation. Run from `~/autocad-mcp` under `uv run`:

```
python -m autocad_mcp.probe_dxf out.dxf                 # the grid snapshot
python -m autocad_mcp.probe_dxf out.dxf --text          # every string, with layer and point
python -m autocad_mcp.probe_dxf out.dxf --spaces        # the layout names
python -m autocad_mcp.probe_dxf out.dxf --text --space Layout1 --layer TITLE
                                    # title-block read-back: handle, height, MTEXT width + attach
```

`--text` reads model space unless `--space` names a layout; an unknown name lists the real ones.

**Screenshot parameters, in order of effect.** `region=[left,top,right,bottom]` crops
*before* the downscale, so a 500x400 detail crop is ~270 tokens and *sharper* than the
whole window — **always pass it for a detail check.** `save_to="….png"` writes to disk and
attaches nothing. `max_dimension` (default 1280, clamped 64–2576); halving it roughly
quarters the cost. Every capture reports `est_tokens`. Non-levers: `quality` (JPEG) shrinks
bytes, not tokens, and nothing above 2576 px survives the vision API's own downscale.

Reserve a full-window capture for what only pixels answer — layout, overlap, match to a
reference photo — and batch it to a checkpoint after a group of edits, not after each one.

`view(zoom_window)` takes **drawing coordinates, not screen pixels**; pixel-like guesses
don't move the view and cost another screenshot to notice. Prefer `(mcp:sel-show)`, or
compute the window from entity coordinates.

## Rule 4: batch, and stop at phase boundaries

Several sequential command-line operations belong in **one** `.scr` run via
`(command "_.SCRIPT" "C:/temp/batch.scr")`, not one `execute_lisp` each — one request
instead of fifteen. Snippet in `references/activex-and-lisp-limits.md`. Any `execute_lisp`
body longer than ~400 bytes, or with quotes nested beyond 2 levels, goes in a temp `.lsp`
and runs as `(load "path")`; inlined, it breaks the JSON envelope.

When a phase is confirmed complete (cleanup → template → layout → detailing), suggest the
next one start as a fresh chat with a short written handoff.

## When something is wrong

`references/troubleshooting.md` maps every symptom and error payload (`WRONG-DOC`,
`UNRESOLVED-REF`, `truncated`, the `timeout_*` codes) to the action. **Never re-send a
mutating call blindly** — check state first; a blind retry can double-apply.

## References

- `autocad-save-verification` (sibling skill) — which save verb to reach for, how each one
  fails silently, and what counts as proof that a write landed
- `references/troubleshooting.md` — symptoms, error payloads, retry rules
- `references/tool-reference.md` — what the tool schemas don't say, `mcp_select.lsp`'s API,
  and selection techniques for 20–30 K entity drawings
- `references/activex-and-lisp-limits.md` — LT's ActiveX limits, hang recovery, `.scr`
  batching, raw-AutoLISP traps, and rebuilding an entity safely
- `references/setup-and-autoload.md` — Startup Suite, restarts, stale definitions
- `references/why-this-skill-exists.md` — the measurements behind the rules
