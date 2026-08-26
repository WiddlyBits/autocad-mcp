# autocad-mcp tool surface

Tools are named `mcp__autocad-mcp__<name>`. Read the live schema with ToolSearch
(`select:mcp__autocad-mcp__<name>`) if you need exact parameter names — this is a
summary of the operations that exist, not a substitute for the schema.

- **system** — `status` (connection check, **plus the `preflight` block**: `dispatch`,
  `probes`, `select` for which .lsp libraries are live, and `dwg`, `ctab`, `tilemode`,
  `pickfirst` for what is in front of you — one call in place of six), `init` (re-acquire
  the window handle after a restart **and (re)load `mcp_probes.lsp` + `mcp_select.lsp` by
  absolute path**, so the APPLOAD Startup Suite only has to carry `mcp_dispatch.lsp`;
  re-loading every time is also the fix for the stale-definition trap),
  `execute_lisp` (run raw AutoLISP, `data.code`)
- **drawing** — `info` (entity_count + layers + **extents** `{min:[x,y],max:[x,y]}`, null when
  empty — the cheap verification call), `save` (`data.path` optional; omit to save in place),
  `save_as_dxf` (`data.path` — writes a full text representation to disk, then read it locally
  for **zero** MCP cost), `plot_pdf`, `purge`. There is **no** `save_as` operation — use `save`
  with a `data.path`.
- **entity** — `list` (type/handle/layer only, **no coordinates**, and unpaginated — filter by
  `layer` or it returns everything), `count` (optionally filtered by `layer`), `get`, `erase`.
  `get` returns real geometry: start/end, center/radius, arc angles, polyline vertices,
  insertion points, and **the contents of TEXT/MTEXT/ATTDEF**. `entity_id` accepts `"last"`.
- **layer** — `list`, `freeze`, `thaw`, `create`
- **view** — `zoom_extents`, `zoom_window` (takes **drawing coordinates**, `x1 y1 x2
  y2` — not screen pixels), `get_screenshot`. Cost parameters in order of effect:
  `region=[left,top,right,bottom]` (pixel rect of the capture, cropped **before** the
  downscale, so it keeps native sharpness — the strongest lever); `save_to` (writes the PNG
  to disk and attaches **no image**, costing no context); `max_dimension` (default 1280,
  clamped 64-2576, `None` = full res); `quality` 1-95 switches to JPEG but does **not**
  reduce token cost. Every capture reports `width`, `height`, and `est_tokens`.
  See Rule 1 in the main skill before calling this.
- **block**, **annotation**, **pid** — less frequently used; check the live schema
  when a task needs them

## Selection-set techniques for large drawings

Drawings in this workflow can have 20,000–30,000+ entities. Full bounding-box
iteration over everything is expensive. Two cheaper techniques:

- **Strip/band probing** — to cheaply map where content sits across a large drawing
  without an expensive per-entity bbox pass, run `ssget "_C"` (crossing) on a thin
  horizontal or vertical strip and read the resulting count. Repeat across strips to
  build a coarse density map.
- **Windowed selection excluding a boundary** — `ssget "_W"` (fully-enclosed window,
  as opposed to `_C` crossing) selects only entities completely inside the window.
  This is useful for grabbing "everything in this row/band" while automatically
  excluding a full-height boundary or baseplate polyline that a crossing selection
  would always sweep in.

Remember `ssget "X"` (no window) skips frozen layers and doesn't reach entities
nested inside block *definitions* — see `activex-and-lisp-limits.md` for the
block-definition scan pattern when that matters. It also **spans model and paper
space both**, which is not a nuance on a composed sheet: a "move everything" built
on a bare `ssget "_X"` took the border with it. Filter with `(cons 410 "Model")`,
or `(67 . 0)` model / `(67 . 1)` paper, or call `(mcp:space-ss "Model")`.

## mcp_select.lsp — selection handoff and guarded mutation

Loaded by `system(operation="init")`. Full rationale in Rule 1 of the main skill; this
is the surface.

- `c:HS` — what **Gianni** types (with objects selected: `HS` + Enter) to hand the
  selection over. It stashes handles, which survive the next command; `(ssget "_P")`
  does not.
- `(mcp:sel)` — the handed-over selection as a handle list. Reads the reactor snapshot,
  then the `HS` stash, then `(ssget "_P")`, dropping dead handles at each rung.
- `(mcp:sel-dump)` — **the one call that replaces four.** JSON: `source`, `count`,
  `shown`/`truncated`, `dwg`, `ctab`, union `bbox`, and per entity `handle`, `type`,
  `layer`, `space` plus type-correct geometry — LINE `p1`/`p2`/`length`/`angle`,
  CIRCLE/ARC `center`/`radius`, TEXT `text`/`insertion`/`align`/`height`/`hjust`,
  MTEXT `text`/`insertion`, INSERT `name`/`insertion`/`xscale`/`rotation`, LWPOLYLINE
  `vertices`/`closed`. Capped at `*mcp-max-selection*` (200).
- `(mcp:sel-show)` — zooms to the set's bbox, then highlights it. Costs no tokens.
  The zoom is the point: highlighting alone is invisible if the set is off-screen.
- `(mcp:by-handles lst)` — handle list to selection set, skipping erased handles.
- `(mcp:guard dwg tab)` / `(mcp:wrong-doc dwg tab)` — `dwg` matches as a **suffix**
  (`"DI-02"`), `tab` matches `CTAB`; `nil` or `""` means "don't check this one". The
  refusal payload names the actual `dwg`/`ctab`, so a `WRONG-DOC` needs no follow-up
  probe.
- `(mcp:reactor-init)` — registers the command reactor if this AutoCAD has one. Returns
  `NO-REACTORS` / `REGISTERED` / `ALREADY-REGISTERED` / `FAILED`. **Unverified on LT
  2027** — the symbol existing would not prove the event fires before AutoCAD discards
  the implied selection; only a live grip-select test proves that.

Verbs, all `(handles dwg tab ...)`, each in its own named UNDO group, each returning
counts:

- `(mcp:line-uniform handles dwg tab len anchor)` — anchor `"left"`/`"right"`/`"top"`/
  `"bottom"` is the end that does **not** move. Non-LINE entities are reported as
  `skipped`, not silently ignored.
- `(mcp:align-axis handles dwg tab axis ref)` — `axis` `"X"`/`"Y"`; `ref` a number or
  `"topmost"`/`"bottommost"`/`"leftmost"`/`"rightmost"`. Moves TEXT group **11** as well
  as 10 — for any non-left justification, 11 is what positions the glyphs.
- `(mcp:sel-move handles dwg tab dx dy)` — via `_.MOVE`, so every entity type works.
- `(mcp:text-sub handles dwg tab old new)` — literal substring replace in TEXT and
  single-chunk MTEXT. MTEXT split across group 3 chunks is **skipped**, because writing
  group 1 alone would truncate it.
