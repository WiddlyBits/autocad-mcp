---
name: autocad-mcp-workflow
description: >-
  How to work efficiently and safely in AutoCAD LT via the autocad-mcp MCP server
  (tools named mcp__autocad-mcp__ system, drawing, entity, layer, view, block,
  annotation, pid). Use this any time Gianni is doing CAD or drawing work through
  autocad-mcp: editing a .dwg, rebuilding a panel layout, cleaning up a border or
  template, or troubleshooting the MCP connection itself, even if he does not say
  "skill" or name the tool explicitly. Use it especially when he refers to what he
  has selected — "the lines I have selected", "move these circles", "duplicate
  these labels" — because grip selection does not survive the MCP link and Rule 1
  is how it gets handed over. Also consult it before the first autocad-mcp call of
  a session, and whenever a drawing, save, or open operation needs verifying, a
  screenshot is about to be taken, or AutoCAD seems stuck after an execute_lisp call.
---

# Working in AutoCAD LT via autocad-mcp

**Every `execute_lisp` is one API request at roughly 108 K cache-read tokens.** That is
the cost that dominates this workflow — not screenshots. Round-trip count is the lever;
each rule below exists to remove round trips or to remove the mistakes that cause them.
(For the measurements behind that, see `references/why-this-skill-exists.md`.)

## Rule 0: one preflight call, before anything else

`system(operation="init")` loads `mcp_probes.lsp` and `mcp_select.lsp` into the current
document by absolute path and returns a `preflight` block:

```
dispatch, probes, select  — which .lsp libraries are live
dwg, ctab, tilemode       — which document and space are in front
pickfirst                 — whether grip selection is even enabled
```

`system(operation="status")` returns the same block without re-loading. Run `init` at the
start of a session and after any AutoCAD restart or `.lsp` edit; it re-loads every time on
purpose, which is also the fix for edited-on-disk files whose old definitions stay
resident and answer plausibly. If `probes` or `select` comes back false, say so and stop —
the rungs below are not available and hand-rolled AutoLISP is what you are about to spend
the session on.

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
   including the literal contents of any text. It answers everything; do not follow it
   with a second probe of the same set.
3. **Echo before you mutate.** `(mcp:sel-show)` zooms to the set and highlights it, at
   zero token cost — he is already looking at the screen. Say what you counted
   ("16 TEXT on layer LABEL, model space"). A set that does not match what he described
   is the moment to stop, and it is the check that catches stray entities *before* they
   are edited rather than twenty minutes later.
4. **Operate by handle list.** Handles survive an intervening command, the dispatch
   boundary, and the ~128 open-selection-set ceiling. `(ssget "_P")` survives none of
   those — it is whatever command ran last, which is not necessarily what he meant.

The verbs each take `(handles dwg tab ...)` and report what they changed:

```
(mcp:line-uniform (mcp:sel) "DI-02" "Model" 10.8 "right")   ; anchor: left/right/top/bottom
(mcp:align-axis   (mcp:sel) "DI-02" "Model" "X" "topmost")  ; ref: a number or *most
(mcp:sel-move     (mcp:sel) "DI-02" "Model" -9.5697 0.0)
(mcp:text-sub     (mcp:sel) "DI-03" "Model" "Bank 1" "Bank 3")
```

Anything they do not cover is still hand-written AutoLISP — but read the dump first and
write the pattern against strings you have actually read. A `wcmatch` pattern written from
memory matched 0 of 152 labels and cost three round trips to correct.

## Rule 2: name the document and the space on every edit

`mcp:guard` is not a courtesy — it is what makes the wrong-document edit impossible rather
than merely checked-for. Pass the document suffix (`"DI-02"`) and the tab (`"Model"`, or a
layout name) and the verb refuses with `WRONG-DOC` instead of editing, naming what it
found. **Fold the assertion into the edit; do not spend a turn asking him to confirm focus
and another verifying it.** When he genuinely must click a tab, give him the whole sequence
up front rather than one round trip per tab.

Space is not a detail, and it bites edits as hard as probes:

| Operation | Follows | Trap |
|---|---|---|
| `ssget "_X"` | **neither** — spans model *and* paper | a "move everything" swept the border too |
| `ssget "_C"`, `PASTECLIP`, `ZOOM` | `CTAB` | pasted 16 labels into the `11x17` tab |
| `mcp:bbox-by-layer-in`, `mcp:grid-map-in`, `mcp:text-dump-in` | their argument | a bare call means model space |

Filter `ssget "_X"` with `(cons 410 "Model")` — or `(67 . 0)` model / `(67 . 1)` paper — or
call `(mcp:space-ss "Model")`. Start from `(getvar "CTAB")` if you don't know what's live.

**To back out an edit, use `drawing(undo)`** — every verb here opens one named UNDO group,
so one undo reverses the whole batch exactly. Never reverse a move by moving back: a
re-move with a slightly different offset leaves a drawing that is not the one you started
with.

`vla-Add`/`vla-Open` do not reliably transfer real UI focus even when
`vla-get-ActiveDocument` says they did, and **a bare `{"ok":true}` from save or open is not
evidence** — `drawing(save)` has returned ok while the underlying SAVEAS failed and the
original document stayed active. Follow either with `drawing(info)`, or use
`(mcp:verify-write path cmdlist)`, which runs the write and reports whether the bytes on
disk actually moved. Do not settle for the caught error: DXFOUT aimed at a directory that
does not exist returns `no-error` and writes nothing. The whole procedure — which of
`drawing(save)` / `save_as_dxf` / `SAVEAS` to reach for, and how each one fails — is in the
`autocad-save-verification` skill.

## Rule 3: climb the capture ladder

A screenshot costs `ceil(width/28) * ceil(height/28)` visual tokens — dimensions only —
and is re-read by every request that follows it. Escalate only when the cheaper rung
genuinely cannot answer.

| Rung | Call | Answers | Cost |
|---|---|---|---|
| L0 | `system(status)` preflight | which document, space, libraries | ~80 tok |
| L0 | `(mcp:whoami)` | which file, which tab, unsaved changes | ~80 tok |
| L0 | `drawing(info)`, `entity(count)` | did the edit apply | ~50 tok |
| L1 | `(mcp:sel-dump)`, `entity(get)` | what is selected; where is it; what does it say | ~100–400 tok |
| L2 | `(mcp:bbox-by-layer-in "Model")` | what occupies which region | ~200–600 tok |
| L3 | `(mcp:grid-map-in 24 16 "Model")` | does the layout read correctly, coarse | ~300–800 tok |
| L3b | `drawing(save_as_dxf)` + `python -m autocad_mcp.probe_dxf` | anything exactly, incl. all text | **~0 tok** |
| L4 | `view(get_screenshot, region=[l,t,r,b])` | this specific detail, visually | ~270 tok |
| L5 | `view(get_screenshot)` full window | overall read, match-to-reference-photo | 1,334 tok @1280 |

`entity(get)` returns real geometry and the contents of TEXT/MTEXT/ATTDEF, so **never
screenshot to read a label**. Angles come back in degrees.

**L3b is the rung to reach for when the question is "what is actually in this drawing".**
`drawing(save_as_dxf)` writes the file, and parsing happens on this machine, so only the
answer enters the conversation:

```
python -m autocad_mcp.probe_dxf out.dxf                 # the grid snapshot
python -m autocad_mcp.probe_dxf out.dxf --text          # every string, with layer and point
python -m autocad_mcp.probe_dxf out.dxf --spaces        # the layout names
```

Run it from `~/autocad-mcp` under `uv run`. `--space` reads a layout instead of model space,
and an unknown name lists the real ones rather than costing a second run.

**Screenshot parameters, in order of effect.** `region=[left,top,right,bottom]` crops
*before* the downscale, so a 500x400 detail crop is ~270 tokens and *sharper* than the
whole window at 1280 — use it for any detail check. `save_to="….png"` writes to disk and
attaches nothing, costing no context. `max_dimension` (default 1280, clamped 64–2576);
halving it roughly quarters the cost. Every capture reports `est_tokens`. Two non-levers:
`quality` (JPEG) shrinks bytes, not tokens, and nothing above 2576 px survives the vision
API's own downscale.

Reserve a full-window capture for what only pixels answer — does the layout read
correctly, is there unwanted overlap, does it match a reference photo. Batch to a
checkpoint after a group of edits, not after each one.

`view(zoom_window)` takes **drawing coordinates, not screen pixels**. Guessing pixel-like
values doesn't move the view, and the only way to notice is another screenshot — so it
silently doubles image cost. `(mcp:sel-show)` zooms to a real bounding box instead; prefer
it, or compute the window from entity coordinates.

## Rule 4: batch, and stop at phase boundaries

Several sequential command-line operations belong in **one** `.scr` run via
`(command "_.SCRIPT" "C:/temp/batch.scr")`, not one `execute_lisp` each — at ~108 K per
round trip that is the difference between one request and fifteen. See
`references/activex-and-lisp-limits.md`.

When a phase is confirmed complete (cleanup → template → layout → detailing), suggest the
next one start as a fresh chat with a short written handoff. A long thread that compacts
itself repeatedly is more expensive and lossier than a clean handoff.

## When something is wrong

Three that come up most: `preflight` reporting `select: false` or `probes: false` →
`system(operation="init")`. `(mcp:sel)` empty → nothing was handed over, ask for `HS` +
Enter, don't guess. A `WRONG-DOC` refusal → it already names the actual `dwg`/`ctab`,
so use those rather than spending a round trip re-probing.

Everything else — dispatcher hangs, stale definitions, purge and `CLAYER` oddities,
`claude mcp add` in PowerShell — is in `references/troubleshooting.md`.

## References

- `autocad-save-verification` (sibling skill) — which save verb to reach for, how each one
  fails silently, and what counts as proof that a write landed
- `references/tool-reference.md` — the full tool surface, `mcp_select.lsp`'s API, and
  selection techniques for 20–30 K entity drawings
- `references/activex-and-lisp-limits.md` — LT's ActiveX limits, hang recovery, `.scr`
  batching, and four ways a raw AutoLISP call gets thrown away
- `references/setup-and-autoload.md` — APPLOAD, restarts, MCP registration
- `references/troubleshooting.md` — symptom table
- `references/why-this-skill-exists.md` — the measurements behind every rule above
