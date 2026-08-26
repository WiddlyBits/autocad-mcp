# ActiveX limits, hang recovery, and useful AutoLISP patterns

Verified on AutoCAD **LT** 2027 (R33) specifically — full AutoCAD may not have these
restrictions, but assume LT's limits apply unless told otherwise.

## Object-creation calls hang the dispatcher

AutoCAD LT has restricted ActiveX support. Calls that *create* new objects — for
example `vla-AddPViewport`, or `vla-Add` on a Layers collection — hang the dispatcher
and leave AutoCAD stuck in an active command state. Nothing gets applied, but the
call doesn't return either.

Property getters and setters on *existing* objects work fine:
`vla-put-Width`, `vla-put-Height`, `vla-put-Center`, `vla-put-Plottable`,
`vla-get-*`, etc.

**Preferred approach:** for creating things, use the MCP's own higher-level tools
(`layer` create, `entity` ops) which route through the AutoCAD command line rather
than raw ActiveX creation. When you do need raw AutoLISP for creation, prefer
command-line functions — `_.RECTANG`, `_.-LAYER`, `_.MOVE`, `_.TEXT`, or `(command
...)` — over `vla-Add`-style ActiveX object creation.

**Recovery signature:** if `execute_lisp` starts timing out but screenshots still
work, that combination means a hang, not a dead connection. Recovery: ask the user to
click into the drawing area and press Esc a few times, then re-verify state with
`drawing(info)` before retrying anything — a blind retry could double-apply whatever
the hung call was doing.

## Batch a sequence of edits into one script file instead of many execute_lisp calls

Each `execute_lisp` round-trip has its own MCP tool-call overhead. When a task needs
several sequential command-line operations (move a batch of entities, run the same
edit across many layers, a multi-step cleanup), write them as an AutoCAD script file
and run it in one dispatch instead of one `execute_lisp` per step:

```lisp
(setq f (open "C:/temp/batch.scr" "w"))
(write-line "_.LAYER _S 0 " f)
(write-line "_.MOVE all _ 0,0 10,10 " f)
(write-line "_.LAYER _S A-WALL " f)
(close f)
(command "_.SCRIPT" "C:/temp/batch.scr")
```

This is also the standard LT workaround for automation that would otherwise need
ActiveX object-creation (which hangs — see above): drive it through `(command ...)`
lines in a script instead of `vla-Add`. Still verify afterward with `drawing(info)` —
a script that errors partway through can leave the drawing in a partially-applied
state, same as any other batch operation.

## Four ways a single call gets thrown away

Each of these cost a real round trip on 2026-08-22. At ~108 K cache-read per request they
are worth reading before writing raw AutoLISP rather than after.

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

**Write `wcmatch` patterns against text you have read, not text you remember.**
`"Bank #, Point #"` matched 0 of 152 labels that read as `Bank 1, Point 0` on screen —
there is a non-obvious character after the comma; `"Bank #*Point #"` matched all 16. Dump
the literal strings first (`(mcp:sel-dump)` or `(mcp:text-dump-in "Model")`), then write
the pattern.

## Working AutoLISP patterns

**Unlock all locked layers before a mass delete** (a locked layer will fail
`vla-Delete` with "Automation Error. On locked layer"):
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

## Duplicating a drawing safely

AutoCAD's own `_.OPEN`/`_.SAVEAS` commands can silently fail — the file dialog pops
up and gets auto-cancelled (e.g. because `FILEDIA` wasn't suppressed), and the tool
can still report success. `(command "_.OPEN" path)` has failed silently this way.

The reliable pattern is OS-level copy + ActiveX open, not AutoCAD's native
open/save-as commands:
```powershell
Copy-Item "source.dwg" "destination.dwg"   # source must be closed/saved-clean first
```
```lisp
(vla-open (vla-get-Documents (vlax-get-acad-object)) "C:/path/destination.dwg")
```
Then confirm the copy actually opened as the active/focused document (see Rule 2 in
the main skill) before editing it.
