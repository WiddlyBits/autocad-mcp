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

### Fix

**Option A — reduce char height** so all n lines fit within the existing group 43:

```
char_height ≤ group43 / n_lines
```

Round down to 2 decimal places. For the Title cell (group 43 ≈ 0.272, n=3):
`0.272 / 3 = 0.0907` → use **h = 0.08** (3 × 0.08 = 0.24 ≤ 0.272 ✓ and ≤ cell height 0.247 ✓).

**Option B — increase group 43** to fit n lines at the current char height, capped by
the cell height (0.247 for Title/Revision No.):

```
new_group43 = min(n_lines × char_height + small_margin, cell_height)
```

For n=3, h=0.09: new_group43 = min(0.27+0.005, 0.247) = 0.247. But 0.27 > 0.247, so
you still need to reduce h. Use Option A first.

### LISP pattern for the fix

Write LISP over ~400 bytes or with nested quotes to a temp .lsp file, not inline.
Below is the atomic entmod pattern (single entity, under 400 bytes):

```lisp
(if (not (mcp:guard "AI-01 Draft 1.dwg" "11x17"))
  (mcp:wrong-doc "AI-01 Draft 1.dwg" "11x17")
  (progn
    (setq ent (handent (car (mcp:sel))))
    (setq d (entget ent))
    (setq d (subst (cons 40 0.08)     (assoc 40 d) d))  ; char height
    (setq d (subst (cons 41 1.907)    (assoc 41 d) d))  ; ref width (reset if bloated)
    (setq d (subst (cons 43 0.240)    (assoc 43 d) d))  ; defined height (3 lines × 0.08)
    (entmod d)
    (entupd ent)
    "done"))
```

After this, take a crop screenshot to confirm the third line has rejoined the title cell.
Use `drawing(undo)` to revert if the text is still wrong — entmod opens one UNDO group.

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

After the entmod, climb the ladder before reaching for pixels:

1. `entity(get)` on the handle — confirms the new group 40 value was written
2. `(mcp:sel-show)` — zooms to the entity; if the bbox no longer extends to x≈17.7 the
   overflow is gone
3. Only if you need visual confirmation of layout: `view(get_screenshot, region=[l,t,r,b])`
   cropped to the title block area (roughly screen pixels for x=[title-block-left, border-right],
   y=[title-block-top, bottom]) — saves ~1000 tokens vs a full window capture

---

## Quick reference: group codes for MTEXT editing

| Group | Meaning | Typical value |
|---|---|---|
| 40 | Char height | 0.100 (or reduced — see Rule 4) |
| 41 | Reference rectangle width (MTEXT box) | 1.907 for left-column content |
| 43 | Defined height (column height overflow point) | ≥ n × char_height |
| 71 | Attachment point (1=TL … 2=TC … 7=BL) | 2 (TC) for left-column content |
| 73 | Line spacing type (1=at least, 2=exact) | 1 |
| 44 | Line spacing factor | 1.0 |

Read them: `(setq d (entget ent))` then `(assoc <group> d)`.  
Write them: `(setq d (subst (cons <group> <value>) (assoc <group> d) d))` then `(entmod d)`.
