;;; mcp_select.lsp - selection handoff and guarded mutation for AutoCAD MCP
;;;
;;; Load after mcp_probes.lsp; system(operation="init") does this for you with
;;; absolute paths, so the APPLOAD Startup Suite only ever needs
;;; mcp_dispatch.lsp. Then call through execute_lisp:
;;;
;;;   execute_lisp "(mcp:sel-dump)"
;;;   execute_lisp "(mcp:sel-show)"
;;;   execute_lisp "(mcp:line-uniform (mcp:sel) \"DI-02\" \"Model\" 10.8 \"right\")"
;;;
;;; ---------------------------------------------------------------------
;;; WHY THIS FILE EXISTS
;;;
;;; Measured on 2026-08-22, across five sessions of selection-driven editing:
;;; 129 of 193 tool calls were execute_lisp, each one a request costing about
;;; 108 K cache-read tokens. Characterising a single selection took four
;;; round-trips - count, then types, then coordinates, then lengths - with the
;;; same (while (< i n) ... strcat) boilerplate retyped each time.
;;;
;;; So the unit of work here is ONE CALL PER QUESTION, not one call per group
;;; code. mcp:sel-dump answers every question that ritual asked, in a single
;;; payload, and dispatches on entity type so that asking a TEXT for its Width
;;; - an MTEXT-only property, and a real failed round trip that day - cannot
;;; happen.
;;; ---------------------------------------------------------------------
;;;
;;; ---------------------------------------------------------------------
;;; THE SELECTION CANNOT BE PULLED. IT HAS TO BE HANDED OVER.
;;;
;;; The dispatcher works by typing "(c:mcp-dispatch)" at the command line, and
;;; starting a command is exactly what discards AutoCAD's implied (grip)
;;; selection. Measured that day: (ssget "_I") returned NONE on 4 of 4
;;; attempts and (ssgetfirst) returned (-1 -1), with PICKFIRST=1, PICKADD=2,
;;; CMDACTIVE=0. This is not a system-variable problem and no amount of
;;; retrying fixes it.
;;;
;;; mcp:sel therefore reads, in order:
;;;   1. *mcp-pickfirst*  - captured by a command reactor before the command
;;;                         started, if this AutoCAD has reactors at all
;;;   2. *mcp-sel*        - stashed by c:HS, which the user types deliberately
;;;   3. (ssget "_P")     - the previous selection set, which works only after
;;;                         SELECT + Enter + Enter and is clobbered by the
;;;                         next command to run
;;;
;;; Rungs 1 and 2 keep handle strings, not selection-set objects. A handle
;;; survives an intervening command, the dispatch boundary, and the ~128 open
;;; selection-set ceiling; an sset survives none of those reliably.
;;; ---------------------------------------------------------------------
;;;
;;; NO ACTIVEX, for the reasons mcp_probes.lsp gives: LT has no COM, vla-
;;; object creation hangs the dispatcher, and vla-getboundingbox with the
;;; conventional 'mn / 'mx output symbols collides with any local of the same
;;; name - another measured failure that day ("bad argument type for compare:
;;; 1 #<safearray...>"). Everything here reads and writes entget group codes.
;;;
;;; (command ...) IS used, unlike in mcp_probes.lsp: this file changes the
;;; drawing on purpose. Every mutating verb wraps itself in a named UNDO group
;;; so that reversing it is one UNDO rather than an inverse move - that day a
;;; wrong-space move was backed out by re-moving with a slightly different
;;; number, which is not the same drawing afterwards.
;;;
;;; EVERY MUTATING VERB NAMES ITS DOCUMENT AND ITS SPACE. Same reasoning as
;;; the -in suffix in mcp_probes.lsp, and the same two failures it prevents:
;;; a bare (ssget "_X") spans model AND paper space, and PASTECLIP follows
;;; CTAB. Both mis-fired that day. AutoLISP has no optional arguments, so the
;;; guard is written down at every call site rather than defaulted.
;;;
;;; Compatible with AutoCAD LT 2024+.

;; -----------------------------------------------------------------------
;; Tunables
;; -----------------------------------------------------------------------

;; Dump ceiling. A handed-over selection is tens of entities; a stray
;; (ssget "_P") after a select-all is not, and the 10 s IPC timeout does not
;; care which one you meant. Truncates and says so.
(setq *mcp-max-selection* 200)

;; Fraction of the set's bounding box added as margin when mcp:sel-show zooms.
;; Zero margin puts the outermost entity hard against the window edge, which
;; reads as "cut off" rather than "selected".
(setq *mcp-show-margin* 0.15)

;; -----------------------------------------------------------------------
;; State
;; -----------------------------------------------------------------------

;; Both hold lists of handle strings. A handle survives what a selection-set
;; object does not: an intervening command, the dispatch boundary, and the
;; ~128 open-sset ceiling.
(setq *mcp-sel* nil)
(setq *mcp-pickfirst* nil)
(setq *mcp-cmd-reactor* nil)

;; -----------------------------------------------------------------------
;; Handles - the durable identity
;; -----------------------------------------------------------------------

(defun mcp:handles-of (ss / n i out)
  "Selection set to list of handle strings, in selection order."
  (setq out nil)
  (if ss
    (progn
      (setq n (sslength ss) i 0)
      (while (< i n)
        (setq out (cons (cdr (assoc 5 (entget (ssname ss i)))) out))
        (setq i (1+ i))
      )
    )
  )
  (reverse out)
)

(defun mcp:by-handles (lst / ss ename)
  "Handle list to selection set, skipping handles whose entity is gone.

   Silently dropping a dead handle is right here: an erased entity is a fact
   about the drawing, not a malformed argument, and every caller reports the
   count it actually operated on."
  (setq ss (ssadd))
  (foreach h lst
    (setq ename (handent h))
    (if ename (setq ss (ssadd ename ss)))
  )
  (if (> (sslength ss) 0) ss nil)
)

(defun mcp:live-handles (lst / out)
  "The subset of lst whose entities still exist."
  (setq out nil)
  (foreach h lst (if (handent h) (setq out (cons h out))))
  (reverse out)
)

;; -----------------------------------------------------------------------
;; Handoff
;; -----------------------------------------------------------------------

(defun mcp:on-cmd (reactor args / gf)
  "Command reactor callback: stash the implied selection before the command
   that is about to start discards it.

   Whether this ever fires is an open question on LT - see mcp:reactor-init.
   It is written to be harmless if it does not: it only ever writes a global."
  (setq gf (ssgetfirst))
  (if (cadr gf) (setq *mcp-pickfirst* (mcp:handles-of (cadr gf))))
  (princ)
)

(defun mcp:reactor-init ( )
  "Register the command reactor if this AutoCAD has reactors at all.

   LT's VLISP support is partial, and the symbol existing would not prove the
   event fires early enough to still see the implied selection - only a live
   test with something grip-selected proves that. Returns what happened, so
   the answer is a fact rather than an assumption."
  (cond
    ((not (member 'vlr-command-reactor (atoms-family 1))) "NO-REACTORS")
    (*mcp-cmd-reactor* "ALREADY-REGISTERED")
    (t
     (setq *mcp-cmd-reactor*
           (vlr-command-reactor nil '((:vlr-commandWillStart . mcp:on-cmd))))
     (if *mcp-cmd-reactor* "REGISTERED" "FAILED")
    )
  )
)

(defun c:HS ( / gf n)
  "Hand off the current selection. Type HS + Enter with objects selected.

   Two keystrokes instead of SELECT + Enter + Enter, and unlike (ssget \"_P\")
   the stash is not clobbered by whatever command runs next - which matters,
   because the next command to run is always the dispatcher."
  (setq gf (ssgetfirst))
  (setq ss (cadr gf))
  (if (null ss) (setq ss (ssget "_I")))
  (if ss
    (progn
      (setq *mcp-sel* (mcp:handles-of ss))
      (setq n (length *mcp-sel*))
      (princ (strcat "\nHanded off " (itoa n) " object"
                     (if (= n 1) "" "s") " to the MCP."))
    )
    (princ "\nNothing selected - select objects first, then type HS.")
  )
  (princ)
)

(defun mcp:sel ( / ss)
  "The handed-over selection, as a list of handle strings.

   Reads the three rungs in order of how deliberate they are. Dead handles are
   dropped, so a stash that has gone stale degrades to the next rung instead
   of returning entities that no longer exist."
  (cond
    ((mcp:live-handles *mcp-pickfirst*))
    ((mcp:live-handles *mcp-sel*))
    ((setq ss (ssget "_P")) (mcp:handles-of ss))
    (t nil)
  )
)

(defun mcp:sel-source ( )
  "Which rung mcp:sel would answer from. Cheap, and it is the difference
   between \"you selected these\" and \"these happen to be left over\"."
  (cond
    ((mcp:live-handles *mcp-pickfirst*) "reactor")
    ((mcp:live-handles *mcp-sel*) "HS")
    ((ssget "_P") "previous")
    (t "none")
  )
)

;; -----------------------------------------------------------------------
;; mcp:sel-dump - one call, every question
;; -----------------------------------------------------------------------

(defun mcp:bbox-list (lst / box)
  "Union box over a handle list, or nil."
  (setq box nil)
  (foreach h lst
    (if (handent h)
      (setq box (mcp:bb-union box (mcp:ent-bbox-data (entget (handent h)) 0)))
    )
  )
  box
)

(defun mcp:ent-json (h / data kind out p1 p2 p10 p11 s)
  "One entity as a JSON object: identity, then geometry chosen by type.

   The type dispatch is the point. Group codes mean different things per
   entity - 10 is a LINE's start, a CIRCLE's centre and a TEXT's insertion -
   and reaching for a property the type does not have is the failure this
   replaces."
  (setq data (entget (handent h)))
  (setq kind (cdr (assoc 0 data)))
  (setq out (strcat "{\"handle\":\"" h "\""
                    ",\"type\":\"" kind "\""
                    ",\"layer\":\"" (mcp:esc (cdr (assoc 8 data))) "\""
                    ",\"space\":\"" (mcp:esc (mcp:space-name (cdr (assoc 410 data)))) "\""))
  (cond
    ((= kind "LINE")
     (setq p1 (cdr (assoc 10 data)) p2 (cdr (assoc 11 data)))
     (setq out (strcat out
                       ",\"p1\":[" (mcp:fmt (car p1)) "," (mcp:fmt (cadr p1)) "]"
                       ",\"p2\":[" (mcp:fmt (car p2)) "," (mcp:fmt (cadr p2)) "]"
                       ",\"length\":" (mcp:fmt (distance p1 p2))
                       ",\"angle\":" (mcp:fmt (/ (* 180.0 (angle p1 p2)) pi))))
    )
    ((= kind "CIRCLE")
     (setq p10 (cdr (assoc 10 data)))
     (setq out (strcat out
                       ",\"center\":[" (mcp:fmt (car p10)) "," (mcp:fmt (cadr p10)) "]"
                       ",\"radius\":" (mcp:fmt (cdr (assoc 40 data)))))
    )
    ((= kind "ARC")
     (setq p10 (cdr (assoc 10 data)))
     (setq out (strcat out
                       ",\"center\":[" (mcp:fmt (car p10)) "," (mcp:fmt (cadr p10)) "]"
                       ",\"radius\":" (mcp:fmt (cdr (assoc 40 data)))
                       ",\"start_angle\":" (mcp:fmt (cdr (assoc 50 data)))
                       ",\"end_angle\":" (mcp:fmt (cdr (assoc 51 data)))))
    )
    ((= kind "TEXT")
     ;; Group 11 is the alignment point, and for any justification other than
     ;; left it - not group 10 - is what positions the glyphs. Reporting only
     ;; 10 is how an aligned column of text moves and does not appear to.
     (setq p10 (cdr (assoc 10 data)) p11 (cdr (assoc 11 data)))
     (setq out (strcat out
                       ",\"text\":\"" (mcp:esc (cdr (assoc 1 data))) "\""
                       ",\"insertion\":[" (mcp:fmt (car p10)) "," (mcp:fmt (cadr p10)) "]"
                       ",\"align\":[" (mcp:fmt (car p11)) "," (mcp:fmt (cadr p11)) "]"
                       ",\"height\":" (mcp:fmt (cdr (assoc 40 data)))
                       ",\"hjust\":" (itoa (mcp:group data 72 0))
                       ",\"vjust\":" (itoa (mcp:group data 73 0))))
    )
    ((= kind "MTEXT")
     (setq p10 (cdr (assoc 10 data)))
     (setq s (mcp:mtext-string data))
     (setq out (strcat out
                       ",\"text\":\"" (mcp:esc s) "\""
                       ",\"insertion\":[" (mcp:fmt (car p10)) "," (mcp:fmt (cadr p10)) "]"
                       ",\"height\":" (mcp:fmt (cdr (assoc 40 data)))))
    )
    ((= kind "INSERT")
     (setq p10 (cdr (assoc 10 data)))
     (setq out (strcat out
                       ",\"name\":\"" (mcp:esc (cdr (assoc 2 data))) "\""
                       ",\"insertion\":[" (mcp:fmt (car p10)) "," (mcp:fmt (cadr p10)) "]"
                       ",\"xscale\":" (mcp:fmt (mcp:group data 41 1.0))
                       ",\"yscale\":" (mcp:fmt (mcp:group data 42 1.0))
                       ",\"rotation\":" (mcp:fmt (/ (* 180.0 (mcp:group data 50 0.0)) pi))))
    )
    ((= kind "LWPOLYLINE")
     (setq out (strcat out
                       ",\"vertices\":" (itoa (mcp:group data 90 0))
                       ",\"closed\":" (if (= 1 (logand 1 (mcp:group data 70 0))) "true" "false")))
    )
  )
  ;; Every type gets a box, including the ones with no branch above. An entity
  ;; this file has no opinion about still has a position, and that is usually
  ;; the question.
  (strcat out ",\"bbox\":" (mcp:bb-json (mcp:ent-bbox-data data 0)) "}")
)

(defun mcp:sel-dump ( / lst n shown out first box i h)
  "The handed-over selection, fully described, in one round trip.

   Replaces the count / types / coordinates / lengths sequence outright: this
   is all four, and the literal contents of any text - so a wcmatch pattern
   gets written against strings that have been read rather than guessed at.
   That guess cost three round trips on 2026-08-22 and still matched 0 of 152."
  (mcp:begin-output)
  (setq lst (mcp:sel))
  (setq n (length lst))
  (setq shown (min n *mcp-max-selection*))
  (setq out "" first T box nil i 0)
  (while (< i shown)
    (setq h (nth i lst))
    (setq out (strcat out (if first "" ",") (mcp:ent-json h)))
    (setq first nil)
    (setq i (1+ i))
  )
  (setq box (mcp:bbox-list lst))
  (setq out (strcat "{\"source\":\"" (mcp:sel-source) "\""
                    ",\"count\":" (itoa n)
                    ",\"shown\":" (itoa shown)
                    ",\"truncated\":" (if (> n shown) "true" "false")
                    ",\"dwg\":\"" (mcp:esc (getvar "DWGNAME")) "\""
                    ",\"ctab\":\"" (mcp:esc (getvar "CTAB")) "\""
                    ",\"bbox\":" (mcp:bb-json box)
                    ",\"entities\":[" out "]}"))
  (mcp:end-output)
  out
)

;; -----------------------------------------------------------------------
;; mcp:sel-show - the echo that costs nothing
;; -----------------------------------------------------------------------

(defun mcp:sel-show ( / lst ss box dx dy pad p1 p2 n)
  "Highlight the handed-over selection AND zoom to it.

   The zoom is not a nicety. Highlighting alone was tried on 2026-08-22 on two
   stray entities sitting outside the current view; the reply was \"I don't
   see it selected\", and the confirmation step confirmed nothing.

   Costs no tokens - the user is already looking at the screen. Run it before
   any mutation and say what you counted."
  (mcp:begin-output)
  (setq lst (mcp:sel))
  (setq ss (mcp:by-handles lst))
  (setq n (if ss (sslength ss) 0))
  (if ss
    (progn
      (setq box (mcp:bbox-list lst))
      (if box
        (progn
          (setq dx (- (caddr box) (car box)) dy (- (cadddr box) (cadr box)))
          (setq pad (* *mcp-show-margin* (max dx dy 1.0)))
          (setq p1 (list (- (car box) pad) (- (cadr box) pad)))
          (setq p2 (list (+ (caddr box) pad) (+ (cadddr box) pad)))
          (command "_.ZOOM" "_W" p1 p2)
        )
      )
      ;; sssetfirst AFTER the zoom: ZOOM is a command, and a command clears
      ;; the implied selection - the same mechanism this whole file works
      ;; around. Highlighting first would highlight nothing.
      (sssetfirst nil ss)
    )
  )
  (mcp:end-output)
  (strcat "{\"shown\":" (itoa n)
          ",\"source\":\"" (mcp:sel-source) "\"}")
)

;; -----------------------------------------------------------------------
;; Guarding - refuse rather than edit the wrong thing
;; -----------------------------------------------------------------------

(defun mcp:guard (dwg tab / name)
  "T when the active document and space are the ones named, nil otherwise.

   dwg matches as a SUFFIX, because that is how these drawings get referred to
   - \"the one ending in DI-02\" - and requiring the whole path means a caller
   has to know a path it was never told.

   nil or \"\" for either argument means \"do not check this one\", which is
   the honest answer when a caller genuinely does not care. Passing that
   deliberately is fine; reaching it by forgetting the argument is not
   possible, since AutoLISP has no optional arguments."
  (setq name (getvar "DWGNAME"))
  (and
    (or (null dwg)
        (= dwg "")
        (and (>= (strlen name) (strlen dwg))
             (= (strcase dwg)
                (strcase (substr name (- (strlen name) (strlen dwg) -1))))))
    (or (null tab) (= tab "") (= (strcase tab) (strcase (getvar "CTAB"))))
  )
)

(defun mcp:wrong-doc (dwg tab)
  "The refusal payload. Says what was asked for AND what is actually in front
   of it, because \"WRONG-DOC\" alone sends the caller back for another round
   trip to find out which."
  (strcat "{\"ok\":false,\"error\":\"WRONG-DOC\""
          ",\"wanted_dwg\":\"" (mcp:esc (if dwg dwg "")) "\""
          ",\"wanted_tab\":\"" (mcp:esc (if tab tab "")) "\""
          ",\"actual_dwg\":\"" (mcp:esc (getvar "DWGNAME")) "\""
          ",\"actual_tab\":\"" (mcp:esc (getvar "CTAB")) "\"}")
)

(defun mcp:undo-begin (label)
  "Open a named UNDO group so the whole verb reverses as one step."
  (setq *mcp-undo-label* label)
  (command "_.UNDO" "_Begin")
  (princ)
)

(defun mcp:undo-end ( )
  (command "_.UNDO" "_End")
  (princ)
)

;; -----------------------------------------------------------------------
;; Verbs
;;
;; Each takes (handles dwg tab ...), guards, wraps itself in one UNDO group,
;; and reports what it changed. None of them re-reads the selection: the
;; caller passes the handle list it has already looked at with mcp:sel-dump
;; and echoed with mcp:sel-show.
;; -----------------------------------------------------------------------

(defun mcp:line-uniform (handles dwg tab len anchor / n skipped data p1 p2
                         keep other dir new out)
  "Set every LINE in handles to length len, holding one end fixed.

   anchor is \"left\", \"right\", \"top\" or \"bottom\" - the end that does
   NOT move. Non-LINE entities are counted as skipped rather than silently
   ignored; a set that was supposed to be all lines and is not is something
   the caller needs told."
  (if (not (mcp:guard dwg tab))
    (mcp:wrong-doc dwg tab)
    (progn
      (mcp:begin-output)
      (mcp:undo-begin "mcp:line-uniform")
      (setq n 0 skipped 0)
      (foreach h handles
        (setq data (if (handent h) (entget (handent h)) nil))
        (if (and data (= "LINE" (cdr (assoc 0 data))))
          (progn
            (setq p1 (cdr (assoc 10 data)) p2 (cdr (assoc 11 data)))
            ;; Which end is the anchor is a question about coordinates, not
            ;; about which group code came first.
            (setq keep
                  (cond
                    ((= anchor "left")   (if (<= (car p1) (car p2)) p1 p2))
                    ((= anchor "right")  (if (>= (car p1) (car p2)) p1 p2))
                    ((= anchor "bottom") (if (<= (cadr p1) (cadr p2)) p1 p2))
                    ((= anchor "top")    (if (>= (cadr p1) (cadr p2)) p1 p2))
                    (t nil)
                  ))
            (if keep
              (progn
                (setq other (if (equal keep p1 1e-12) p2 p1))
                (setq dir (angle keep other))
                (setq new (polar keep dir len))
                (setq data (subst (cons 10 keep) (assoc 10 data) data))
                (setq data (subst (cons 11 new) (assoc 11 data) data))
                (entmod data)
                (setq n (1+ n))
              )
              (setq skipped (1+ skipped))
            )
          )
          (setq skipped (1+ skipped))
        )
      )
      (mcp:undo-end)
      (setq out (strcat "{\"ok\":true,\"verb\":\"line-uniform\""
                        ",\"changed\":" (itoa n)
                        ",\"skipped\":" (itoa skipped)
                        ",\"length\":" (mcp:fmt len)
                        ",\"anchor\":\"" (mcp:esc anchor) "\"}"))
      (mcp:end-output)
      out
    )
  )
)

(defun mcp:anchor-point (h / data)
  "The point an entity is positioned BY - group 10 for everything this file
   moves. Kept separate so align and move agree on what an entity's position
   means."
  (setq data (entget (handent h)))
  (cdr (assoc 10 data))
)

(defun mcp:align-ref (handles axis ref / best bestval p want)
  "Resolve ref to a coordinate on axis.

   ref is a number, or one of \"topmost\" / \"bottommost\" / \"leftmost\" /
   \"rightmost\", meaning: take the value from whichever entity is extreme in
   THAT direction. \"Match everything to the x of the topmost object\" is then
   one call, which is how it gets asked for."
  (if (numberp ref)
    ref
    (progn
      (setq best nil bestval nil)
      (foreach h handles
        (if (handent h)
          (progn
            (setq p (mcp:anchor-point h))
            (setq want (cond ((or (= ref "topmost") (= ref "bottommost")) (cadr p))
                             (t (car p))))
            (if (or (null bestval)
                    (cond
                      ((= ref "topmost")    (> want bestval))
                      ((= ref "bottommost") (< want bestval))
                      ((= ref "rightmost")  (> want bestval))
                      ((= ref "leftmost")   (< want bestval))
                      (t nil)))
              (setq bestval want best p)
            )
          )
        )
      )
      (if best (if (= axis "X") (car best) (cadr best)) nil)
    )
  )
)

(defun mcp:align-axis (handles dwg tab axis ref / target n data p10 p11 out)
  "Align every entity in handles on one axis, leaving the other unchanged.

   axis is \"X\" or \"Y\". TEXT gets both group 10 and group 11 moved: for any
   justification other than left, 11 is the point that positions the glyphs,
   and moving only 10 changes the data without changing the drawing."
  (if (not (mcp:guard dwg tab))
    (mcp:wrong-doc dwg tab)
    (progn
      (mcp:begin-output)
      (setq target (mcp:align-ref handles axis ref))
      (if (null target)
        (progn
          (mcp:end-output)
          (strcat "{\"ok\":false,\"error\":\"UNRESOLVED-REF\""
                  ",\"ref\":\"" (mcp:esc (if (numberp ref) (mcp:fmt ref) ref)) "\"}")
        )
        (progn
          (mcp:undo-begin "mcp:align-axis")
          (setq n 0)
          (foreach h handles
            (setq data (if (handent h) (entget (handent h)) nil))
            (if data
              (progn
                (setq p10 (cdr (assoc 10 data)))
                (setq p10 (if (= axis "X")
                            (list target (cadr p10) (caddr p10))
                            (list (car p10) target (caddr p10))))
                (setq data (subst (cons 10 p10) (assoc 10 data) data))
                (if (and (= "TEXT" (cdr (assoc 0 data))) (assoc 11 data))
                  (progn
                    (setq p11 (cdr (assoc 11 data)))
                    (setq p11 (if (= axis "X")
                                (list target (cadr p11) (caddr p11))
                                (list (car p11) target (caddr p11))))
                    (setq data (subst (cons 11 p11) (assoc 11 data) data))
                  )
                )
                (entmod data)
                (setq n (1+ n))
              )
            )
          )
          (mcp:undo-end)
          (setq out (strcat "{\"ok\":true,\"verb\":\"align-axis\""
                            ",\"changed\":" (itoa n)
                            ",\"axis\":\"" (mcp:esc axis) "\""
                            ",\"target\":" (mcp:fmt target) "}"))
          (mcp:end-output)
          out
        )
      )
    )
  )
)

(defun mcp:sel-move (handles dwg tab dx dy / ss n out)
  "Move handles by a delta, as one undoable step.

   Uses _.MOVE on a set built from handles rather than entmod per entity:
   MOVE understands every entity type, including the ones with no group 10
   worth moving."
  (if (not (mcp:guard dwg tab))
    (mcp:wrong-doc dwg tab)
    (progn
      (mcp:begin-output)
      (setq ss (mcp:by-handles handles))
      (setq n (if ss (sslength ss) 0))
      (if ss
        (progn
          (mcp:undo-begin "mcp:sel-move")
          (command "_.MOVE" ss "" (list 0.0 0.0 0.0) (list dx dy 0.0))
          (mcp:undo-end)
        )
      )
      (setq out (strcat "{\"ok\":true,\"verb\":\"sel-move\""
                        ",\"moved\":" (itoa n)
                        ",\"requested\":" (itoa (length handles))
                        ",\"dx\":" (mcp:fmt dx) ",\"dy\":" (mcp:fmt dy) "}"))
      (mcp:end-output)
      out
    )
  )
)

(defun mcp:str-sub (s old new / out i lo ll)
  "Literal substring replace, all occurrences. wcmatch is a matcher, not a
   replacer, and every AutoLISP idiom for this is hand-rolled."
  (setq ll (strlen old) lo (strlen s))
  (if (= ll 0)
    s
    (progn
      (setq out "" i 1)
      (while (<= i lo)
        (if (and (<= (+ i ll -1) lo) (= (substr s i ll) old))
          (progn (setq out (strcat out new)) (setq i (+ i ll)))
          (progn (setq out (strcat out (substr s i 1))) (setq i (1+ i)))
        )
      )
      out
    )
  )
)

(defun mcp:text-sub (handles dwg tab old new / n skipped data kind s ns out)
  "Replace a literal substring in the contents of TEXT and single-chunk MTEXT.

   MTEXT long enough to be split across group 3 chunks is reported as skipped
   rather than edited: writing group 1 alone on a chunked MTEXT truncates it,
   and a verb that quietly destroys text is worse than one that declines."
  (if (not (mcp:guard dwg tab))
    (mcp:wrong-doc dwg tab)
    (progn
      (mcp:begin-output)
      (mcp:undo-begin "mcp:text-sub")
      (setq n 0 skipped 0)
      (foreach h handles
        (setq data (if (handent h) (entget (handent h)) nil))
        (setq kind (if data (cdr (assoc 0 data)) ""))
        (cond
          ((and (= kind "MTEXT") (assoc 3 data)) (setq skipped (1+ skipped)))
          ((or (= kind "TEXT") (= kind "MTEXT"))
           (setq s (cdr (assoc 1 data)))
           (setq ns (mcp:str-sub s old new))
           (if (= ns s)
             (setq skipped (1+ skipped))
             (progn
               (entmod (subst (cons 1 ns) (assoc 1 data) data))
               (setq n (1+ n))
             )
           )
          )
          (t (setq skipped (1+ skipped)))
        )
      )
      (mcp:undo-end)
      (setq out (strcat "{\"ok\":true,\"verb\":\"text-sub\""
                        ",\"changed\":" (itoa n)
                        ",\"skipped\":" (itoa skipped)
                        ",\"old\":\"" (mcp:esc old) "\""
                        ",\"new\":\"" (mcp:esc new) "\"}"))
      (mcp:end-output)
      out
    )
  )
)

;; -----------------------------------------------------------------------
;; Identity and write verification
;;
;; Neither of these does anything a caller could not write inline. That is
;; exactly why they are here. Measured on 2026-08-25 over the previous day's
;; transcripts, one session hand-wrote the identity getvar list FIVE times and
;; rebuilt the FILEDIA/mtime dance about six times, each variation slightly
;; different from the last, each one its own ~108 K round trip. A one-liner
;; retyped from memory is a one-liner that is eventually retyped wrong.
;; -----------------------------------------------------------------------

(defun mcp:pad2 (n)
  (if (< n 10) (strcat "0" (itoa n)) (itoa n))
)

(defun mcp:systime-str (st)
  "vl-file-systime's list as \"YYYY-MM-DD HH:MM:SS\", or \"\" for nil.

   The raw list wedges day-of-week in at index 2, so the day sits at 3 and
   every field after it is off by one from where it looks like it should be.
   Skipping it here means no caller has to remember to."
  (if st
    (strcat (itoa (nth 0 st)) "-"
            (mcp:pad2 (nth 1 st)) "-"
            (mcp:pad2 (nth 3 st)) " "
            (mcp:pad2 (nth 4 st)) ":"
            (mcp:pad2 (nth 5 st)) ":"
            (mcp:pad2 (nth 6 st)))
    ""
  )
)

(defun mcp:whoami ( / out)
  "Which document, which space, and is it dirty - in one call.

   DWGPREFIX is reported next to DWGNAME because the question this actually
   answers is not \"what is it called\" but \"which file am I about to write
   over\", and two drawings with the same name in different folders is the
   ordinary case, not the exotic one. autocad-mcp routes to whatever window
   holds UI focus, so the answer is not necessarily the document the caller
   had in mind.

   dbmod is reported raw and as a boolean: nonzero means unsaved changes,
   which is the precondition worth checking before an open or a close, and
   the thing a bare {\"ok\":true} from save will not tell you."
  (mcp:begin-output)
  (setq out (strcat "{\"ok\":true,\"verb\":\"whoami\""
                    ",\"dwg\":\"" (mcp:esc (getvar "DWGNAME")) "\""
                    ",\"prefix\":\"" (mcp:esc (getvar "DWGPREFIX")) "\""
                    ",\"ctab\":\"" (mcp:esc (getvar "CTAB")) "\""
                    ",\"tilemode\":" (itoa (getvar "TILEMODE"))
                    ",\"dbmod\":" (itoa (getvar "DBMOD"))
                    ",\"filedia\":" (itoa (getvar "FILEDIA"))
                    ",\"titled\":" (if (= 1 (getvar "DWGTITLED")) "true" "false")
                    ",\"dirty\":" (if (= 0 (getvar "DBMOD")) "false" "true")
                    "}"))
  (mcp:end-output)
  out
)

(defun mcp:verify-write (path cmdlist / fd existed before before-size
                                        r err after after-size changed out)
  "Run a file-writing command and report whether the file actually moved.

   cmdlist is the argument list for vl-cmdf, so the caller writes the command
   it means:

     (mcp:verify-write \"C:/temp/x.dxf\" (list \"_.DXFOUT\" \"C:/temp/x.dxf\" \"16\"))
     (mcp:verify-write \"C:/t/a.dwg\"    (list \"_.SAVEAS\" \"\" \"C:/t/a.dwg\" \"_Y\"))

   ok is derived from the FILE, never from the absence of a caught error, and
   that is the whole point. Measured 2026-08-22: DXFOUT aimed at a directory
   that does not exist returned \"no-error\" from vl-catch-all-apply and wrote
   nothing at all. An error that is never raised cannot be caught, so the only
   evidence that survives is that the bytes on disk changed. Same reasoning as
   the standing rule that a bare {\"ok\":true} from save is not evidence.

   FILEDIA is forced to 0 so the command takes its filename from the argument
   list instead of stopping on a dialog the caller can neither see nor
   dismiss, and restored to whatever it was - not to 1 - because the caller
   may have set it deliberately. The bare (vl-cmdf) flushes any input a
   half-finished command is still waiting on; without it the next call
   inherits an active command.

   Size is compared as well as mtime: a rewrite fast enough to land inside the
   same second is invisible to the timestamp alone. When both are identical
   this reports changed:false, which is the conservative answer rather than
   the confident wrong one."
  (mcp:begin-output)
  (setq existed (if (findfile path) T nil))
  (setq before (vl-file-systime path))
  (setq before-size (vl-file-size path))
  (setq fd (getvar "FILEDIA"))
  (setvar "FILEDIA" 0)
  (setq r (vl-catch-all-apply 'vl-cmdf cmdlist))
  (vl-cmdf)
  (setvar "FILEDIA" fd)
  (setq err (if (vl-catch-all-error-p r) (vl-catch-all-error-message r) nil))
  (setq after (vl-file-systime path))
  (setq after-size (vl-file-size path))
  (setq changed
    (cond
      ((null after) nil)
      ((not existed) T)
      ((not (equal before after)) T)
      ((and before-size after-size (/= before-size after-size)) T)
      (T nil)
    )
  )
  (setq out (strcat "{\"ok\":" (if changed "true" "false")
                    ",\"verb\":\"verify-write\""
                    ",\"path\":\"" (mcp:esc path) "\""
                    ",\"changed\":" (if changed "true" "false")
                    ",\"existed_before\":" (if existed "true" "false")
                    ",\"exists_after\":" (if after "true" "false")
                    ",\"mtime_before\":\"" (mcp:systime-str before) "\""
                    ",\"mtime_after\":\"" (mcp:systime-str after) "\""
                    ",\"size\":" (if after-size (itoa after-size) "-1")
                    ",\"cmdactive\":" (itoa (getvar "CMDACTIVE"))
                    ",\"dbmod\":" (itoa (getvar "DBMOD"))
                    ",\"error\":" (if err (strcat "\"" (mcp:esc err) "\"") "null")
                    "}"))
  (mcp:end-output)
  out
)

(princ "\nmcp_select.lsp loaded: c:HS mcp:sel mcp:sel-dump mcp:sel-show mcp:by-handles mcp:guard mcp:reactor-init mcp:whoami mcp:verify-write mcp:line-uniform mcp:align-axis mcp:sel-move mcp:text-sub")
(princ)
