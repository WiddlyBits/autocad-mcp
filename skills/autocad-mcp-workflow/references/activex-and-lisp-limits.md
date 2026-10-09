# ActiveX limits, hang recovery, and useful AutoLISP patterns

Verified on AutoCAD **LT** 2027 (R33) specifically — full AutoCAD may not have these
restrictions, but assume LT's limits apply unless told otherwise.

## Object-creation calls hang the dispatcher

Calls that *create* objects — `vla-AddPViewport`, `vla-Add` on a Layers collection — hang
the dispatcher in an active command state: nothing applied, no return. Getters and setters on
*existing* objects work (`vla-get-*`, `vla-put-Width`, `-Height`, `-Center`, `-Plottable`).
Create through the MCP tools (`layer` create, `entity` ops) or `(command "_.-LAYER" ...)`,
`_.RECTANG`, `_.TEXT` — never `vla-Add`.

**Recovery signature:** `execute_lisp` times out but screenshots still work — a hang, not a
dead link. Ask the user to click into the drawing and press Esc a few times, then
`drawing(info)` before anything else; a blind retry can double-apply.

## Batching to one `.scr` (Rule 4)

```lisp
(setq f (open "C:/temp/batch.scr" "w"))
(write-line "_.LAYER _S 0 " f)
(write-line "_.MOVE all _ 0,0 10,10 " f)
(write-line "_.LAYER _S A-WALL " f)
(close f)
(command "_.SCRIPT" "C:/temp/batch.scr")
```

A script that errors partway leaves the drawing partly applied; verify with `drawing(info)`.

## Three ways a single call gets thrown away

Each cost a real round trip on 2026-08-22.

**`vla-getboundingbox` output symbols collide with locals.** The conventional call is
`(vla-getboundingbox obj 'mn 'mx)`, which *sets* the symbols `mn` and `mx`. If the same
function also uses `mn`/`mx` as ordinary variables — and `mn` for "model count" is a very
natural name — the next comparison gets a safearray:

```
; bad argument type for compare: 1 #<safearray...>
```
Use `'bbmn` / `'bbmx`, or avoid ActiveX entirely and take the box from group codes via
`(mcp:ent-bbox-data (entget ename) 0)`. Some entities also make `vla-getboundingbox`
raise outright, so wrap it in `vl-catch-all-apply` if you do use it.

**Not every entity has every property.** `vla-get-Width` on a `TEXT` returns
`"unknown name: Width"` — Width is MTEXT-only. Group code 10 is a LINE's start point, a
CIRCLE's centre and a TEXT's insertion point. Branch on `(cdr (assoc 0 data))` first, or
just call `(mcp:sel-dump)`, which already does.

**`(member 'sym (atoms-family 1))` cannot tell you whether a function is defined.**
Format 1 returns a list of *strings*, so a quoted symbol never matches and the answer is
nil either way. This was used to check for the probe library and answered `NOT LOADED`,
which was not evidence. Use `(type mcp:sel-dump)` — an unbound symbol evaluates to nil in
AutoLISP rather than erroring, so it is safe and it actually discriminates.

## Rebuilding an entity: the space trap, the silent revert, and the escape hatch

Found on 2026-09-05 converting DI-02's 32 MTEXT labels to Title Case. All three bit in one
session, and the first one shipped a "verified" save of a broken drawing.

**`entmakex` appends to the current space, not the space of the entity you copied.** Reading a
model-space MTEXT's group codes, `entdel`-ing it, and `entmakex`-ing a replacement while the
`11x17` layout tab is current puts the replacement in *paper* space. Every cheap check passes —
text, width, layer, insertion point, and the `ssget "_X"` layer count are all identical —
because the only field that changed is the one nothing was looking at:

```
EF0  67=0 410=Model     ; original
1DB3 67=1 410=11x17     ; the "identical" replacement
```

On a sheet whose model space is viewed through a scaled viewport, that renders the label at
paper scale — `0.25` height becomes 0.25 *inches on the sheet*, roughly 30x oversized and
parked off the drawing. Wrap a model-space rebuild in `(mcp:in-model-space (lambda () ...))`
(Rule 2) and check `(cdr (assoc 67 (entget ne)))` is `0` on the result.

**`entmod` on MTEXT group 41 can report success and silently revert.** Four labels held a
defined width of `9.24774` that would not move. `entmod` with a substituted `(41 . 3.37387)`
returned without error; `vla-put-Width` — a property setter on an existing object, the kind
that normally works fine in LT — also returned success. Both read back as `9.24774` after
`REGENALL`. The entities carried no xdata, no extension dictionary and no column groups to
explain it, and `41` matched `42` exactly on entities whose text differed in length, so the
value was not content-derived either. Delete-and-recreate is the only fix that holds — which
is exactly why the space trap above matters.

The visible symptom of a stuck-wide MTEXT is **a fragment of the text split off and floating**
elsewhere on the sheet: on DI-02 the trailing word `Open` rendered out in the ladder area,
detached from its label. Text content and entity count both read clean while that is on
screen — only pixels or group `41` catch it.

**`entdel` toggles, and that is the undo.** Calling `entdel` on an already-erased entity
*restores* it, and erased entities survive an intervening `QSAVE` for the rest of the session.
That is what made a bad rebuild recoverable: un-erase the eight originals, delete the eight
replacements, lose nothing. Note the asymmetry it creates — `handent` on an erased entity
returns a usable ename whose `entget` is `nil`, so a loop doing
`(cdr (assoc 41 (entget (handent h))))` dies with `bad argument type: numberp: nil` rather
than reporting a missing entity. Test `(null (entget e))` to tell erased from live.

## Working AutoLISP patterns

**`vlax-get-acad-object` works on LT 2027** (2026-10-08: returned the document name through
`execute_lisp`, no hang), so the server no longer refuses `vlax-` code. Field-proven on LT 2027
the same day, all through `execute_lisp` on a scratch drawing:
- `vlax-for` over Layers: 4 entries = `vla-get-Count` = `layer(list)`; per-layer `ssget`
  counts summed to `entity_count` (20).
- `vla-Delete`: 4 lines in model space, one on a locked layer. Without the unlock it raised
  the error below and the entity stayed live; after the unlock pattern, the delete pattern
  reported `deleted=4` and model space counted 0.
- `vla-open`: opened the copy (`FullName` returned, Documents 2 → 3), focus did not move.
  `vla-Add` likewise created `Drawing2.dwg` without moving focus.

**Unlock all locked layers before a mass delete** (a locked layer will fail
`vla-Delete` with "Automation Error. On locked layer" — measured 2026-10-08):
```lisp
(setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))
(setq layers (vla-get-Layers doc))
(setq result "")
(vlax-for lyr layers
  (if (= (vla-get-Lock lyr) :vlax-true)
    (progn (vla-put-Lock lyr :vlax-false)
           (setq result (strcat result (vla-get-Name lyr) " ")))))
(princ (strcat "unlocked: " result))
```

**Delete all Model Space entities** — more reliable than `ssget "X"`, which skips
frozen layers and can't reach entities nested inside block definitions:
```lisp
(setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))
(setq ms (vla-get-ModelSpace doc))
(setq cnt 0) (setq n (vla-get-Count ms)) (setq i 0)
(while (< i n)
  (setq ent (vla-item ms 0)) (vla-Delete ent) (setq cnt (1+ cnt)) (setq i (1+ i)))
(princ (strcat "deleted=" (itoa cnt)))
```

**Search inside block definitions** — `ssget "X"` only searches top-level model/paper
space, not entities nested inside a BLOCK definition. To find text inside blocks
(e.g. hunting for a mystery string), scan block definitions directly:
```lisp
(setq result "") (setq bt (tblnext "BLOCK" T))
(while bt
  (setq bname (cdr (assoc 2 bt))) (setq benth (cdr (assoc -2 bt)))
  (while benth
    (setq edata (entget benth)) (setq etyp (cdr (assoc 0 edata)))
    (if (member etyp (list "TEXT" "MTEXT" "ATTDEF"))
      (progn (setq txt (cdr (assoc 1 edata)))
             (if (and txt (wcmatch (strcase txt) "*X*"))
               (setq result (strcat result "[block=" bname " " etyp ":" txt "] | ")))))
    (setq benth (entnext benth)))
  (setq bt (tblnext "BLOCK")))
(princ result)
```

**A layer resists PURGE even though it's empty** — PURGE never removes the *current*
layer (`CLAYER`), even with zero entities on it:
```lisp
(getvar "CLAYER")          ; check what it's set to
(setvar "CLAYER" "0")      ; switch off the layer you're trying to purge
; then re-run drawing(operation="purge")
```

**Check whether a layer is genuinely unused vs. intentionally hidden** before
deleting it — a layer being off/frozen doesn't mean it's junk:
```lisp
(setq doc (vla-get-ActiveDocument (vlax-get-acad-object)))
(setq layers (vla-get-Layers doc)) (setq result "")
(vlax-for lyr layers
  (setq nm (vla-get-Name lyr)) (setq frz (vla-get-Freeze lyr)) (setq on (vla-get-LayerOn lyr))
  (setq ss (ssget "X" (list (cons 8 nm)))) (setq cnt (if ss (sslength ss) 0))
  (setq result (strcat result nm ": frozen=" (vl-princ-to-string frz)
                        " on=" (vl-princ-to-string on) " count=" (itoa cnt) " | ")))
(princ result)
```
If a hidden layer turns out to hold something like an as-built/record-drawing stamp
convention, that's a deliberate feature, not clutter — ask before removing it.

## Duplicating a drawing

OS copy, then ActiveX open — `(command "_.OPEN" path)` has failed silently (why: the
`autocad-save-verification` skill). The source must be closed or saved clean first:
```powershell
Copy-Item "source.dwg" "destination.dwg"
```
```lisp
(vla-open (vla-get-Documents (vlax-get-acad-object)) "C:/path/destination.dwg")
```
`vla-open` does not reliably move focus (Rule 2; 2026-10-08 it opened the file and focus
stayed on the source) — `(mcp:whoami)` before editing, and expect to ask Gianni to click the
new tab.
