---
name: title-block-text
description: >-
  Place, resize, or reflow text inside the ECSI title block on SMID panel drawings.
  Load before touching any TITLE-layer MTEXT: when fitting a title, updating a
  revision number, or diagnosing text that appears outside the title block border.
---

# Title block text — placing and fitting

## When to load this skill

Any time you are:
- Editing text in the title block (title, job, customer, revision, drawing no., date)
- Diagnosing text that appears outside the title block border
- Setting MTEXT char height to fit multiple lines in a cell

Do **not** open the ECSI title block geometry from a probe call — it is constant across
all drawings in this project and is listed below.

---

## Rule 0: one preflight, then guard every edit

`system(operation="init")` before any `execute_lisp`. Every LISP snippet that mutates the
drawing must open with `mcp:guard`. The guard does a **right-suffix match** on `DWGNAME`
(the full filename including `.dwg` extension).

| Drawing | Guard suffix to pass |
|---|---|
| `SMID Well 8 AI-01 Draft 1.dwg` | `"AI-01 Draft 1.dwg"` |
| `SMID Well 8 AI-01 Draft 1_recover.dwg` | `"AI-01 Draft 1_recover.dwg"` |
| `SMID Treatment Area DI-02 Draft 1.dwg` | `"DI-02 Draft 1.dwg"` |

General pattern: `"<drawing-id> Draft <n>[_recover].dwg"` — include `_recover` when the
preflight `dwg` field ends in `_recover.dwg`. A WRONG-DOC response returns `actual_dwg` —
copy its right portion to form the correct suffix.

---

## Rule 1: find content entities by position, not by handle

Handles change across AutoCAD recovery files. Never cache a handle in memory or in this
skill — always locate the entity fresh via ssget:

```lisp
; Find the Title content MTEXT (TC-attached, TITLE layer, 11x17 paper space)
(setq ss (ssget "_C"
                '(14.0 -0.45)   ; lower-left corner of Title cell
                '(15.5 -0.19)   ; upper-right corner of Title cell
                (list (cons 8 "TITLE") (cons 0 "MTEXT") (cons 410 "11x17"))))
(setq ent (ssname ss 0))
```

Adjust the corner coords to match the target cell from the table in Rule 2. If HS was
already run (`mcp:sel` holds the entity), use `(handent (car (mcp:sel)))` directly —
faster and equally reliable within a session, just not across sessions.

---

## Rule 2: title block cell geometry (hardcoded — do not re-probe)

**Scope:** every coordinate in this skill is for the **11x17 SMID panel tab**. The 36x24 DO
sheets use `Layout1` and different cells. Those values are in the `ecsi-do-three-drawings-inventory`
memory, so do not reuse the numbers below there.

**Paper space tab:** `11x17` (`tilemode=0`)  
**Layer:** `TITLE`  
**Attachment on content MTEXTs:** top-center (DXF group 71 = 2) for the two wide content
columns; top-left for the narrow right column. Read group 71 once if unsure.

### Left content column (Title / Job / Customer)

| Cell | Content insertion (TC) | Cell top y | Cell bottom y | Cell height |
|---|---|---|---|---|
| Customer | [14.4836, 0.3281] | 0.4748 | 0.1415 | 0.333 |
| Job | [14.4836, −0.0052] | 0.1415 | −0.1918 | 0.333 |
| Title | [14.4836, −0.2113] | −0.1918 | −0.4386 | **0.247** |

Cell width: **2.00** (x: 13.494 to 15.494). Correct MTEXT reference width (group 41)
for this column: **1.907** (verified from the original pre-recovery .dwg). If group 41
is larger than ~2.1, the MTEXT box overruns the cell — reset it to 1.907.

### Right content column (Drawing No. / Revision No. / Date)

| Cell | Content insertion | Cell top y | Cell bottom y | Cell height |
|---|---|---|---|---|
| Date | [15.9836, 0.3281] | 0.4748 | 0.1415 | 0.333 |
| Drawing No. | [15.9836, −0.0059] | 0.1415 | −0.1918 | 0.333 |
| Revision No. | [15.9836, −0.3386] | −0.1918 | −0.4386 | 0.247 |

Cell width: **1.09** (x: 15.494 to 16.584). These MTEXTs typically hold short strings
(e.g. "AI-01", "DRAFT 1") at h=0.100 on a single line — no fitting calculation needed
unless the revision string grows long.

---

## Rule 3: diagnosing and fixing the multi-column overflow

### Symptom
One or more lines of the title text appear **outside the title block border** to the
right (typically at x ≈ 17.7). The title cell itself looks partially or fully empty.

### Root cause
MTEXT stores a defined height (DXF group 43). When the text needs more vertical space
than group 43 allows, lines overflow into a phantom second column whose left edge is
at `insertion.x + group41 + gutter`. This happens even with groups 75/76 nil (no explicit
column extension) because AutoCAD uses the defined height as a column height.

**The trigger:** `n_lines × char_height > group43`

At h=0.100 with group 43=0.272 (the template default): 2 lines fit (2×0.100=0.200),
3 lines do not (3×0.100=0.300 > 0.272). Line 3 escapes to the phantom column.

### Why entmod alone cannot fix this

AutoCAD LT maintains a locked ratio `group43 / group40 = constant`. When you change
group 40 (char height) via `entmod`, AutoCAD silently recalculates group 43 proportionally.
When you then try to set group 43 directly via `entmod`, AutoCAD silently ignores it and
it snaps back to `old_group43 × (new_h / old_h)`. Verified 2026-09-09 on AI-01: the ratio
was 2.72 — meaning 3 lines (ratio 3.0) will overflow at any char height via this path.

**Do not attempt Option B (increase group 43 via entmod) — it will not take.**

### Fix: reduce char height and rebuild with entmakex

Reduce char height so `n_lines × h` comfortably fits. For the Title cell (n=3):
use **h = 0.08** (3 × 0.08 = 0.24 ≤ cell height 0.247 ✓).

Because `entmod` for group 43 is silently refused, **rebuild the entity via `entmakex`**.
AutoCAD will auto-set group 43 based on the new rendered text height — which will be ≥ n×h
so no phantom column forms. Write this to `C:/temp/fix_title_mtext.lsp` (it is >400 bytes):

```lisp
; Run as: (load "C:/temp/fix_title_mtext.lsp")
(if (not (mcp:guard "<SUFFIX>" "11x17"))
  (mcp:wrong-doc "<SUFFIX>" "11x17")
  (progn
    (setq ent (handent (car (mcp:sel))))
    (setq d (entget ent))
    (setq d (subst (cons 40 0.08)  (assoc 40 d) d))
    (setq d (subst (cons 41 1.907) (assoc 41 d) d))
    (setq d (subst (cons 43 0.244) (assoc 43 d) d))  ; AutoCAD ignores this but include anyway
    (setq d (vl-remove-if
              (function (lambda (pair)
                (member (car pair) (list -1 -2 5 102 330 360))))
              d))
    (mcp:undo-begin "title-fit")  ; one group, so one drawing(undo) reverses create+delete
    (setq newent (entmakex d))
    (if newent (entdel ent))
    (mcp:undo-end)
    (if newent
      (list "ok"
            (cdr (assoc 40 (entget newent)))
            (cdr (assoc 41 (entget newent)))
            (cdr (assoc 43 (entget newent))))
      "entmakex-failed")))
```

Expected result: `("ok" 0.08 1.907 <auto-value>)` where auto-value ≥ 0.24.
The old handle is gone — re-locate via ssget (see Rule 1) for any subsequent edits.

To revert: `drawing(undo)` once (reverses entdel + entmakex as one group).

---

## Rule 4: standard char heights by line count

These targets use the formula `h = floor(cell_height / n / 0.01) × 0.01` and are
pre-computed for the two constrained cells (height ≈ 0.247):

| Lines | Char height | Notes |
|---|---|---|
| 1 | 0.10 | Template default — do not reduce unless the text overflows the cell width |
| 2 | 0.12 | 2 × 0.12 = 0.24 ≤ 0.247 |
| 3 | 0.08 | 3 × 0.08 = 0.24 ≤ 0.247 |
| 4 | 0.06 | 4 × 0.06 = 0.24 ≤ 0.247; matches the small label height — use sparingly |

For the taller cells (Customer, Job, Drawing No., Date — height ≈ 0.333): the template
default h=0.100 fits up to 3 lines without adjustment.

---

## Rule 5: verify without a full screenshot

After the entmakex, climb the ladder before reaching for pixels:

1. `entity(get)` on the handle — confirms the new group 40 value was written
2. **Read every title-block string at once with no LISP:** `drawing(save_as_dxf)`, then
   `uv run python -m autocad_mcp.probe_dxf out.dxf --text --space <tab> --layer TITLE`
   (from `~/autocad-mcp`). That gives handle, insert, height, and for MTEXT the wrap width
   and attach point, all at ~0 context. Use it instead of `mcp:text-dump-in`, which has no
   handle or width. In DO Draft 2 this read-back took 22 calls and two hand-LISP errors.
3. `(mcp:sel-show)` — zooms to the entity; if the bbox no longer extends to x≈17.7 the
   overflow is gone
4. **Fit check from the plot, not a screenshot.** After `drawing(plot_pdf)`, open the PDF with
   pymupdf and compare `page.get_text("words")` boxes against the cell rectangle. Work out
   the scale from the PDF instead of assuming one. `plot_pdf` plots the LIMMIN–LIMMAX
   window with Fit + Center onto ARCH D. So with `W, H` as the limits size and `pw, ph` as
   `page.rect` size: `s = min(pw/W, ph/H)`, `ox = (pw − W·s)/2`, `oy = (ph − H·s)/2`.
   A drawing point then maps to `(ox + (x − LIMMIN.x)·s, ph − oy − (y − LIMMIN.y)·s)`,
   because the PDF's y axis points down. Any word box outside the mapped cell is an overflow.
   This costs no images, and the PDF is the deliverable anyway.
5. Only if you need visual confirmation of layout: `view(get_screenshot, region=[l,t,r,b])`
   cropped to the title block area (roughly screen pixels for x=[title-block-left, border-right],
   y=[title-block-top, bottom]) — saves ~1000 tokens vs a full window capture

---

## Rule 6: fixing bottom overflow after the entmakex rebuild

After the entmakex fix, the phantom column is gone but the text block may still
**leak below the cell bottom border**. This is a separate problem from the right-side
overflow: the MTEXT insertion sits below the cell top, leaving less vertical room than the
cell height implies.

**Title cell geometry (from Rule 2):**
- Insertion y = −0.2113 (TC anchor)
- Cell bottom y = −0.4386
- Available from insertion to bottom: **0.227 units**

At h=0.08 with default line spacing (group 73=1 "at least", group 44=1.0), 3 lines render
≈ 0.346 tall — leaking ~0.12 units below the cell. AutoCAD auto-sets group 43 ≈ 0.351 to
match, which confirms the overflow is ~0.12 units.

### Fix: tighten line spacing via entmod (this DOES work, unlike group 43)

Groups 73 and 44 are not proportionally locked and respond correctly to `entmod`. Get the
new handle via ssget (Rule 1) — the entmakex created a new entity, old handle is gone.

```lisp
(if (not (mcp:guard "<SUFFIX>" "11x17"))
  (mcp:wrong-doc "<SUFFIX>" "11x17")
  (progn
    (setq ent (handent "<handle>"))
    (setq d (entget ent))
    (if (assoc 44 d)
      (setq d (subst (cons 44 0.80) (assoc 44 d) d))
      (setq d (append d (list (cons 44 0.80)))))
    (if (assoc 73 d)
      (setq d (subst (cons 73 2) (assoc 73 d) d))
      (setq d (append d (list (cons 73 2)))))
    (entmod d)
    (entupd ent)
    (list (cdr (assoc 44 (entget ent)))
          (cdr (assoc 73 (entget ent)))
          (cdr (assoc 43 (entget ent))))))
```

Verified 2026-09-09 on AI-01: group 73=2, group 44=0.80 reduced group 43 from 0.351 to
0.320, bringing "AI 16XI HF" inside the cell bottom. If more tightening is needed, reduce
group 44 further (0.75 is the next reasonable step). Do not go below ~0.65 — the lines
will look cramped against each other.

| group 44 | group 43 result | Fits in 0.227? |
|---|---|---|
| 1.00 (default) | ≈ 0.351 | No — leaks ~0.12 |
| 0.80 | ≈ 0.320 | Borderline — last line sits at cell border |
| 0.70 | ≈ 0.28 | Yes |
| 0.65 | ≈ 0.26 | Yes — tightest comfortable spacing |

---

## Quick reference: group codes for MTEXT editing

| Group | Meaning | Typical value | entmod writable? |
|---|---|---|---|
| 40 | Char height | 0.100 (or reduced — see Rule 4) | Yes — but triggers group 43 recalc |
| 41 | Reference rectangle width (MTEXT box) | 1.907 for left-column content | Yes |
| 43 | Defined height (column height overflow point) | auto-set by AutoCAD LT | **No** — silently ignored; use entmakex |
| 71 | Attachment point (1=TL … 2=TC … 7=BL) | 2 (TC) for left-column content | Yes |
| 73 | Line spacing type (1=at least, 2=exact) | 1 | Yes |
| 44 | Line spacing factor | 1.0 | Yes |

Read them: `(setq d (entget ent))` then `(assoc <group> d)`.  
Write them: `(setq d (subst (cons <group> <value>) (assoc <group> d) d))` then `(entmod d)`.  
**Exception:** group 43 requires `entmakex` (see Rule 3).
