# autocad-mcp tool surface — what the schemas don't say

Tools are named `mcp__autocad-mcp__<name>`; each tool's docstring lists its operations and
parameters. This file holds only what those don't:

- **system** — `init` (re)loads `mcp_probes.lsp` + `mcp_select.lsp` into the *current*
  document; `status` returns `backend` and the `preflight` block (Rule 0). `execute_lisp`
  takes `data.code`.
- **drawing** — there is **no** `save_as`; use `save` with `data.path`. `info` returns
  extents `{min,max}` (null when empty) — the cheap verification call. `open` mostly cannot
  switch documents on the AutoCAD backend.
- **entity** — pass the handle as top-level `entity_id`, not `data.handle`. `list` has no
  coordinates and no paging: filter by `layer`. `move`/`rotate`/`scale` return a bare string
  (`"moved"`), so confirm with `entity(get)` or `drawing(info)`.
- **view** — `zoom_window` takes **drawing coordinates**. `get_screenshot` cost parameters:
  Rule 3 in the main skill.

## Selection-set techniques for large drawings

Drawings in this workflow can have 20,000–30,000+ entities, so a per-entity bbox pass is
expensive. Two cheaper techniques:

- **Strip/band probing** — `ssget "_C"` (crossing) on a thin horizontal or vertical strip and
  read the count. Repeat across strips for a coarse density map. `_C` sees the current space only.
- **Windowed selection excluding a boundary** — `ssget "_W"` selects only entities completely
  inside the window, so it grabs a row or band without sweeping in a full-height border.

`ssget "X"` (no window) skips frozen layers and doesn't reach entities nested inside block
*definitions* — see `activex-and-lisp-limits.md` for the block-definition scan.

## mcp_select.lsp — selection handoff and guarded mutation

Loaded by `system(operation="init")`. Rationale in Rule 1 of the main skill; this is the surface.

- `c:HS` — what **Gianni** types (with objects selected: `HS` + Enter). It stashes handles,
  which survive the next command.
- `(mcp:sel)` — the handed-over selection as a handle list: reactor snapshot, then the `HS`
  stash, then `(ssget "_P")`, dropping dead handles at each rung.
- `(mcp:sel-dump)` — JSON: `source`, `count`, `shown`/`truncated`, `dwg`, `ctab`, union `bbox`,
  and per entity `handle`, `type`, `layer`, `space` plus type-correct geometry — LINE
  `p1`/`p2`/`length`/`angle`, CIRCLE/ARC `center`/`radius`, TEXT `text`/`insertion`/`align`/
  `height`/`hjust`, MTEXT `text`/`insertion`, INSERT `name`/`insertion`/`xscale`/`rotation`,
  LWPOLYLINE `vertices`/`closed`. Capped at `*mcp-max-selection*` (200).
- `(mcp:sel-show)` — zooms to the set's bbox, then highlights it. Costs no tokens.
- `(mcp:by-handles lst)` — handle list to selection set, skipping erased handles.
- `(mcp:guard dwg tab)` / `(mcp:wrong-doc dwg tab)` — `dwg` matches as a **suffix**, `tab`
  matches `CTAB`; `nil` or `""` skips that check.
- `(mcp:undo-begin label)` / `(mcp:undo-end)` — one named UNDO group around a hand-written edit.
- `(mcp:reactor-init)` — registers the command reactor if this AutoCAD has one. Returns
  `NO-REACTORS` / `REGISTERED` / `ALREADY-REGISTERED` / `FAILED`. **UNTESTED on LT 2027**
  whether the event fires before AutoCAD discards the implied selection.

Verbs, all `(handles dwg tab ...)`, each in its own named UNDO group, each returning counts:

- `(mcp:line-uniform handles dwg tab len anchor)` — anchor `"left"`/`"right"`/`"top"`/
  `"bottom"` is the end that does **not** move. Non-LINE entities are reported as `skipped`.
- `(mcp:align-axis handles dwg tab axis ref)` — `axis` `"X"`/`"Y"`; `ref` a number or
  `"topmost"`/`"bottommost"`/`"leftmost"`/`"rightmost"`. Moves TEXT group **11** as well as
  10 — for any non-left justification, 11 is what positions the glyphs.
- `(mcp:sel-move handles dwg tab dx dy)` — via `_.MOVE`, so every entity type works.
- `(mcp:text-sub handles dwg tab old new)` — literal substring replace in TEXT and
  single-chunk MTEXT. MTEXT split across group 3 chunks is **skipped**, because writing
  group 1 alone would truncate it.
