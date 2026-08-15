;;; mcp_probes.lsp - cheap structural probes for AutoCAD MCP
;;;
;;; Load via the APPLOAD Startup Suite alongside mcp_dispatch.lsp, then call
;;; through execute_lisp:
;;;
;;;   execute_lisp "(mcp:bbox-by-layer)"
;;;   execute_lisp "(mcp:overlap \"BORDER\" \"DIAGRAM\" 2.0)"
;;;   execute_lisp "(mcp:snapshot \"C:/temp/draft3.snap\")"
;;;
;;; Every probe that scans the drawing comes in two forms. The bare name means
;;; model space, which is what all of them silently meant before; the -in form
;;; takes an explicit space - "Model" or a layout tab name:
;;;
;;;   execute_lisp "(mcp:bbox-by-layer-in \"Layout1\")"
;;;   execute_lisp "(mcp:grid-map-in 24 16 \"Model\")"
;;;
;;; AutoLISP has no optional arguments, so this is two names rather than one
;;; with a default: calling a one-argument defun with none is "too few
;;; arguments", and finding that out costs a 10 s IPC round trip.
;;;
;;; SPACE IS NOT A DETAIL ON A COMPOSED SHEET. Draft 3 holds 29,717 entities in
;;; model space and 26 in Layout1, with the border and title block entirely in
;;; the latter and the diagram entirely in the former. A probe that assumes
;;; model space answers a different question than the one asked, and looks like
;;; it answered the right one.
;;;
;;; Each returns a JSON string. mcp-cmd-execute-lisp returns the value of the
;;; last form in the loaded file, so a bare probe call is the whole payload.
;;;
;;; ---------------------------------------------------------------------
;;; THIS FILE IS A TRANSCRIPTION. The arithmetic it implements lives in
;;; src/autocad_mcp/probes.py and is held to tests/golden/*.dxf by pytest.
;;; execute_lisp exists only on the file_ipc backend, so nothing here can be
;;; exercised without a running AutoCAD - which is exactly why none of it was
;;; invented here. Change the Python first, let the tests fail, then port.
;;;
;;; The contract between the two is tests/golden/*.snap: the same drawing must
;;; produce byte-identical snapshot text from either side.
;;; ---------------------------------------------------------------------
;;;
;;; NO ACTIVEX. Every measurement comes from entget group codes. AutoCAD LT
;;; has no COM interface, and vla- object creation hangs the dispatcher
;;; outright - so vla-getboundingbox is not used even where it would be
;;; convenient. The Python reference computes bounding boxes the same way for
;;; the same reason: two layers that measure differently cannot check
;;; each other.
;;;
;;; Compatible with AutoCAD LT 2024+.

;; -----------------------------------------------------------------------
;; Tunables - keep in step with src/autocad_mcp/probes.py
;; -----------------------------------------------------------------------

;; v2 added the `space` line and the grid's mode. Both exist because v1 could
;; not say which space it described, and a snapshot of the wrong space is
;; indistinguishable from a snapshot of a changed drawing.
(setq *mcp-snapshot-version* 2)

;; Nominal glyph advance as a fraction of text height. Deliberately a
;; constant: AutoLISP cannot measure a glyph without ActiveX, so anything
;; more accurate here would put the two layers out of step.
(setq *mcp-nominal-char-width* 0.6)

;; Wall-clock ceiling in milliseconds. AUTOCAD_MCP_IPC_TIMEOUT defaults to
;; 10 s, and a probe that runs to 10 s does not return a slow answer - Python
;; has already stopped polling and reported a dead dispatcher. The remainder
;; covers writing the result file and the poll that notices it.
(setq *mcp-time-budget-ms* 7000)

;; Entity ceiling for a full scan. A 20-30k entity drawing is the case that
;; matters; past this a probe truncates and says so.
(setq *mcp-max-entities* 20000)

;; Crossing-window probe ceiling for grid-map.
(setq *mcp-max-probes* 400)

;; -----------------------------------------------------------------------
;; Output formatting
;; -----------------------------------------------------------------------

(defun mcp:fmt (v / s)
  "Six decimal places, and never \"-0.000000\".
   Negative zero is the one value that breaks a golden-file comparison for no
   reason: -1e-9 and +1e-9 both round to zero but print with different signs,
   so a snapshot flips between runs on a value nothing touched."
  (if (null v) (setq v 0.0))
  (setq s (rtos (float v) 2 6))
  (if (and (= (substr s 1 1) "-") (equal (atof s) 0.0 1e-12))
    (setq s (substr s 2))
  )
  s
)

(defun mcp:esc (s / out i ch)
  "JSON/snapshot string escaping. Defined locally so this file works when
   loaded without mcp_dispatch.lsp."
  (if (null s) (setq s ""))
  (setq out "" i 1)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (cond
      ((= ch "\"") (setq out (strcat out "\\\"")))
      ((= ch "\\") (setq out (strcat out "\\\\")))
      ((= ch "\n") (setq out (strcat out "\\n")))
      ((= ch "\r") (setq out (strcat out "\\r")))
      ((= ch "\t") (setq out (strcat out "\\t")))
      (t (setq out (strcat out ch)))
    )
    (setq i (1+ i))
  )
  out
)

(defun mcp:begin-output ( )
  "rtos honours DIMZIN for leading/trailing zero suppression, which would make
   the output format depend on a drawing setting. Pinned once per probe rather
   than once per number - a setvar inside a 30k-entity loop is not free."
  (setq *mcp-old-dimzin* (getvar "DIMZIN"))
  (setvar "DIMZIN" 0)
  (princ)
)

(defun mcp:end-output ( )
  (if *mcp-old-dimzin* (setvar "DIMZIN" *mcp-old-dimzin*))
  (princ)
)

;; -----------------------------------------------------------------------
;; Bounding boxes - a bbox is the list (xmin ymin xmax ymax)
;; -----------------------------------------------------------------------

(defun mcp:bb (x1 y1 x2 y2)
  (list (min x1 x2) (min y1 y2) (max x1 x2) (max y1 y2))
)

(defun mcp:bb-of-points (pts / xs ys p)
  "Bbox over a list of points, or nil when the list is empty."
  (if (null pts) nil
    (progn
      (setq xs (mapcar 'car pts))
      (setq ys (mapcar 'cadr pts))
      (list (apply 'min xs) (apply 'min ys) (apply 'max xs) (apply 'max ys))
    )
  )
)

(defun mcp:bb-union (a b)
  "Union of two bboxes, either of which may be nil."
  (cond
    ((null a) b)
    ((null b) a)
    (t (list (min (nth 0 a) (nth 0 b)) (min (nth 1 a) (nth 1 b))
             (max (nth 2 a) (nth 2 b)) (max (nth 3 a) (nth 3 b))))
  )
)

(defun mcp:bb-hit-closed (a b)
  "Touching counts as a hit - AutoCAD's crossing-window rule."
  (and (<= (nth 0 a) (nth 2 b)) (<= (nth 0 b) (nth 2 a))
       (<= (nth 1 a) (nth 3 b)) (<= (nth 1 b) (nth 3 a)))
)

(defun mcp:bb-hit-open (a b)
  "Touching does NOT count. This is the rule for \"the border must not overlap
   the diagram\": a border drawn flush to the diagram's edge is flush, not a
   collision, and reporting it as one would make the check useless."
  (and (< (nth 0 a) (nth 2 b)) (< (nth 0 b) (nth 2 a))
       (< (nth 1 a) (nth 3 b)) (< (nth 1 b) (nth 3 a)))
)

(defun mcp:bb-inter (a b)
  (if (mcp:bb-hit-open a b)
    (list (max (nth 0 a) (nth 0 b)) (max (nth 1 a) (nth 1 b))
          (min (nth 2 a) (nth 2 b)) (min (nth 3 a) (nth 3 b)))
    nil
  )
)

(defun mcp:bb-gap (a b / dx dy)
  "Shortest distance between two boxes; 0.0 when they touch or overlap.
   Andy's review note wants clearance, not just absence of collision."
  (setq dx (max 0.0 (max (- (nth 0 a) (nth 2 b)) (- (nth 0 b) (nth 2 a)))))
  (setq dy (max 0.0 (max (- (nth 1 a) (nth 3 b)) (- (nth 1 b) (nth 3 a)))))
  (sqrt (+ (* dx dx) (* dy dy)))
)

(defun mcp:bb-json (b)
  (if b
    (strcat "[" (mcp:fmt (nth 0 b)) "," (mcp:fmt (nth 1 b)) ","
                (mcp:fmt (nth 2 b)) "," (mcp:fmt (nth 3 b)) "]")
    "null"
  )
)

(defun mcp:bb-text (b)
  "Space-separated form, for the .snap file."
  (if b
    (strcat (mcp:fmt (nth 0 b)) " " (mcp:fmt (nth 1 b)) " "
            (mcp:fmt (nth 2 b)) " " (mcp:fmt (nth 3 b)))
    "none"
  )
)

;; -----------------------------------------------------------------------
;; Per-type geometry
;; -----------------------------------------------------------------------

(defun mcp:d2r (d) (* pi (/ d 180.0)))

(defun mcp:angle-in-sweep (a s e / span)
  "Is angle a inside the CCW sweep from s to e? Degrees."
  (setq span (rem (+ (rem (- e s) 360.0) 360.0) 360.0))
  (if (equal span 0.0 1e-12)
    T  ; equal start and end angles encode a full circle, not a zero-length arc
    (<= (rem (+ (rem (- a s) 360.0) 360.0) 360.0) span)
  )
)

(defun mcp:arc-bbox (cx cy r sa ea / pts)
  "Tight bbox of an arc.
   NOT center +- r: a 0-90 degree arc occupies one quadrant, and the full
   circle's box would inflate it fourfold and produce phantom overlaps. The
   extremes are the two endpoints plus whichever cardinal directions the sweep
   actually passes through."
  (setq pts (list
    (list (+ cx (* r (cos (mcp:d2r sa)))) (+ cy (* r (sin (mcp:d2r sa)))))
    (list (+ cx (* r (cos (mcp:d2r ea)))) (+ cy (* r (sin (mcp:d2r ea)))))
  ))
  (if (mcp:angle-in-sweep 0.0   sa ea) (setq pts (cons (list (+ cx r) cy) pts)))
  (if (mcp:angle-in-sweep 90.0  sa ea) (setq pts (cons (list cx (+ cy r)) pts)))
  (if (mcp:angle-in-sweep 180.0 sa ea) (setq pts (cons (list (- cx r) cy) pts)))
  (if (mcp:angle-in-sweep 270.0 sa ea) (setq pts (cons (list cx (- cy r)) pts)))
  (mcp:bb-of-points pts)
)

(defun mcp:ellipse-bbox (cx cy mx my ratio / a b th hx hy)
  "Axis-aligned bbox of a rotated full ellipse.
   x(t) = cx + a.cos(t).cos(th) - b.sin(t).sin(th), amplitude
   hypot(a.cos(th), b.sin(th)); y likewise with the terms swapped.
   Conservative for elliptical arcs - the start/end parameters are ignored and
   the full ellipse is measured. A superset box can report an overlap that is
   not there, which is the safe direction to be wrong in."
  (setq a (sqrt (+ (* mx mx) (* my my))))
  (if (equal a 0.0 1e-12)
    (setq mx 1.0 my 0.0 a 1e-12)  ; degenerate major axis: (atan 0 0) errors
  )
  (setq b (* a ratio))
  (setq th (atan my mx))
  (setq hx (sqrt (+ (* (* a (cos th)) (* a (cos th))) (* (* b (sin th)) (* b (sin th))))))
  (setq hy (sqrt (+ (* (* a (sin th)) (* a (sin th))) (* (* b (cos th)) (* b (cos th))))))
  (list (- cx hx) (- cy hy) (+ cx hx) (+ cy hy))
)

(defun mcp:text-bbox (ix iy s h rot top-anchored / w lo hi corners rad c sn out p)
  "Nominal box for a text string. Approximate by construction - see
   *mcp-nominal-char-width*. top-anchored is for MTEXT, whose insertion point
   is the top of the first line rather than the baseline."
  (setq w (* (strlen s) h *mcp-nominal-char-width*))
  (if top-anchored (setq lo (- h) hi 0.0) (setq lo 0.0 hi h))
  (setq corners (list (list 0.0 lo) (list w lo) (list w hi) (list 0.0 hi)))
  (if (not (equal rot 0.0 1e-12))
    (progn
      (setq rad (mcp:d2r rot) c (cos rad) sn (sin rad))
      (setq corners
        (mapcar '(lambda (p)
                   (list (- (* (car p) c) (* (cadr p) sn))
                         (+ (* (car p) sn) (* (cadr p) c))))
                corners))
    )
  )
  (setq out '())
  (foreach p corners (setq out (cons (list (+ ix (car p)) (+ iy (cadr p))) out)))
  (mcp:bb-of-points out)
)

(defun mcp:transform-bbox (b ox oy sx sy rot / corners rad c sn out p)
  "Scale, rotate and translate a box by transforming its four corners.
   Conservative under rotation: the box around the rotated corners is at least
   as large as the box around the rotated geometry. mcp:ent-exact-p reports
   that, so a caller can tell a measured box from a padded one."
  (setq corners (list
    (list (* (nth 0 b) sx) (* (nth 1 b) sy))
    (list (* (nth 2 b) sx) (* (nth 1 b) sy))
    (list (* (nth 2 b) sx) (* (nth 3 b) sy))
    (list (* (nth 0 b) sx) (* (nth 3 b) sy))
  ))
  (if (not (equal rot 0.0 1e-12))
    (progn
      (setq rad (mcp:d2r rot) c (cos rad) sn (sin rad))
      (setq corners
        (mapcar '(lambda (p)
                   (list (- (* (car p) c) (* (cadr p) sn))
                         (+ (* (car p) sn) (* (cadr p) c))))
                corners))
    )
  )
  (setq out '())
  (foreach p corners (setq out (cons (list (+ ox (car p)) (+ oy (cadr p))) out)))
  (mcp:bb-of-points out)
)

(defun mcp:group (data code dflt / hit)
  (if (setq hit (assoc code data)) (cdr hit) dflt)
)

(defun mcp:block-bbox (bname depth / ent data b box)
  "Union of a block definition's members, in block coordinates.
   Depth-capped: a block that references itself is invalid DXF but does exist
   in the wild, and unbounded recursion inside a 10 s budget is not a stack
   overflow, it is a dead dispatcher."
  (setq box nil)
  (if (< depth 4)
    (progn
      (setq ent (tblobjname "BLOCK" bname))
      (if ent (setq ent (entnext ent)))
      (while (and ent (setq data (entget ent))
                  (/= (mcp:group data 0 "") "ENDBLK"))
        (setq b (mcp:ent-bbox-data data (1+ depth)))
        (if b (setq box (mcp:bb-union box b)))
        (setq ent (entnext ent))
      )
    )
  )
  box
)

(defun mcp:ent-bbox-data (data depth / kind p c r sa ea mx my ix iy s h rot inner sx sy)
  "Bbox from an entget association list, or nil when the entity has no
   geometry this code can measure. Mirrors probes.entity_bbox."
  (setq kind (mcp:group data 0 ""))
  (cond
    ((= kind "LINE")
     (mcp:bb-of-points (list (mcp:group data 10 '(0 0)) (mcp:group data 11 '(0 0)))))

    ((= kind "CIRCLE")
     (setq c (mcp:group data 10 '(0 0)) r (mcp:group data 40 0.0))
     (list (- (car c) r) (- (cadr c) r) (+ (car c) r) (+ (cadr c) r)))

    ((= kind "ARC")
     (setq c (mcp:group data 10 '(0 0)) r (mcp:group data 40 0.0))
     (setq sa (/ (* 180.0 (mcp:group data 50 0.0)) pi))
     (setq ea (/ (* 180.0 (mcp:group data 51 0.0)) pi))
     (mcp:arc-bbox (car c) (cadr c) r sa ea))

    ((= kind "ELLIPSE")
     (setq c (mcp:group data 10 '(0 0)))
     (setq p (mcp:group data 11 '(1 0)))
     (setq mx (car p) my (cadr p))
     (mcp:ellipse-bbox (car c) (cadr c) mx my (mcp:group data 40 1.0)))

    ((or (= kind "LWPOLYLINE") (= kind "POLYLINE"))
     ;; Group 10 repeats once per vertex, so assoc alone finds only the first.
     ;; Bulges are ignored, exactly as the Python does - both under-report a
     ;; bulged segment, which mcp:ent-exact-p declares.
     (setq p '())
     (foreach item data (if (= (car item) 10) (setq p (cons (cdr item) p))))
     (if (and (= kind "POLYLINE") (null p))
       (mcp:polyline-vertex-bbox data)
       (mcp:bb-of-points p)))

    ((or (= kind "TEXT") (= kind "ATTDEF"))
     (setq c (mcp:group data 10 '(0 0)))
     (mcp:text-bbox (car c) (cadr c) (mcp:group data 1 "") (mcp:group data 40 0.0)
                    (/ (* 180.0 (mcp:group data 50 0.0)) pi) nil))

    ((= kind "MTEXT")
     (setq c (mcp:group data 10 '(0 0)))
     (mcp:text-bbox (car c) (cadr c) (mcp:mtext-string data) (mcp:group data 40 0.0)
                    (/ (* 180.0 (mcp:group data 50 0.0)) pi) T))

    ((= kind "POINT")
     (setq c (mcp:group data 10 '(0 0)))
     (list (car c) (cadr c) (car c) (cadr c)))

    ((= kind "INSERT")
     (setq c (mcp:group data 10 '(0 0)) ix (car c) iy (cadr c))
     (setq inner (mcp:block-bbox (mcp:group data 2 "") depth))
     (if (null inner)
       (list ix iy ix iy)
       (progn
         (setq sx (mcp:group data 41 1.0) sy (mcp:group data 42 1.0))
         (setq rot (/ (* 180.0 (mcp:group data 50 0.0)) pi))
         (mcp:transform-bbox inner ix iy sx sy rot))))

    (t nil)
  )
)

(defun mcp:polyline-vertex-bbox (data / ent vdata pts)
  "Heavy POLYLINE keeps its vertices as separate VERTEX entities rather than
   as repeated group 10s on the header."
  (setq pts '())
  (setq ent (entnext (cdr (assoc -1 data))))
  (while (and ent (setq vdata (entget ent)) (= (mcp:group vdata 0 "") "VERTEX"))
    (setq pts (cons (mcp:group vdata 10 '(0 0)) pts))
    (setq ent (entnext ent))
  )
  (mcp:bb-of-points pts)
)

(defun mcp:mtext-string (data / out)
  "MTEXT splits long content across repeated group 3 chunks with the tail in
   group 1. Reading only group 1 truncates every long note in the drawing."
  (setq out "")
  (foreach item data (if (= (car item) 3) (setq out (strcat out (cdr item)))))
  (strcat out (mcp:group data 1 ""))
)

(defun mcp:ent-bbox (ename) (mcp:ent-bbox-data (entget ename) 0))

(defun mcp:ent-exact-p (data / kind item bulged)
  "False when the box is nominal, conservative or padded. Worth carrying
   separately: an exact box supports \"these do not overlap\", an approximate
   one only supports \"these might\"."
  (setq kind (mcp:group data 0 ""))
  (cond
    ((or (= kind "LINE") (= kind "CIRCLE") (= kind "ARC") (= kind "POINT")) T)
    ((or (= kind "LWPOLYLINE") (= kind "POLYLINE"))
     ;; A bulge arcs OUTSIDE the vertex hull, so the box is too small. This is
     ;; the one case where the error runs toward a missed overlap rather than
     ;; a phantom one.
     (setq bulged nil)
     (foreach item data
       (if (and (= (car item) 42) (> (abs (cdr item)) 1e-12)) (setq bulged T)))
     (not bulged))
    (t nil)  ; ELLIPSE superset, INSERT corner-padded, TEXT nominal, rest unknown
  )
)

;; -----------------------------------------------------------------------
;; Budget
;; -----------------------------------------------------------------------

(defun mcp:now ( / v)
  "Milliseconds, or nil where MILLISECS is unavailable. A missing clock
   disables the deadline rather than the probe - the count ceilings still hold."
  (setq v (vl-catch-all-apply 'getvar (list "MILLISECS")))
  (if (vl-catch-all-error-p v) nil v)
)

(defun mcp:budget-init (max-ent max-probe ms)
  (setq *mcp-bg-t0* (mcp:now))
  (setq *mcp-bg-ms* (if ms ms *mcp-time-budget-ms*))
  (setq *mcp-bg-max-ent* (if max-ent max-ent *mcp-max-entities*))
  (setq *mcp-bg-max-probe* (if max-probe max-probe *mcp-max-probes*))
  (setq *mcp-bg-ents* 0)
  (setq *mcp-bg-probes* 0)
  (setq *mcp-bg-reason* nil)
  (princ)
)

(defun mcp:budget-expired ( )
  (and *mcp-bg-t0* (> (- (mcp:now) *mcp-bg-t0*) *mcp-bg-ms*))
)

(defun mcp:budget-entity ( )
  "Charge one entity. nil means stop scanning."
  (cond
    (*mcp-bg-reason* nil)
    ((>= *mcp-bg-ents* *mcp-bg-max-ent*) (setq *mcp-bg-reason* "max_entities") nil)
    ((mcp:budget-expired) (setq *mcp-bg-reason* "time") nil)
    (t (setq *mcp-bg-ents* (1+ *mcp-bg-ents*)) T)
  )
)

(defun mcp:budget-probe ( )
  "Charge one crossing-window probe. nil means stop probing."
  (cond
    (*mcp-bg-reason* nil)
    ((>= *mcp-bg-probes* *mcp-bg-max-probe*) (setq *mcp-bg-reason* "max_probes") nil)
    ((mcp:budget-expired) (setq *mcp-bg-reason* "time") nil)
    (t (setq *mcp-bg-probes* (1+ *mcp-bg-probes*)) T)
  )
)

(defun mcp:budget-json ( )
  (strcat "\"truncated\":" (if *mcp-bg-reason* "true" "false")
          ",\"truncated_reason\":"
          (if *mcp-bg-reason* (strcat "\"" *mcp-bg-reason* "\"") "null")
          ",\"scanned\":" (itoa *mcp-bg-ents*))
)

;; -----------------------------------------------------------------------
;; Model-space iteration
;; -----------------------------------------------------------------------

;; A "space" here is the string DXF group 410 carries: "Model", or a layout's
;; tab name. It is the same vocabulary probe_dxf.spaces() uses, so a probe
;; called with "Layout1" means the same thing on both sides.

(defun mcp:space-name (space)
  "Normalise a space argument. nil defaults to model space, which is what
   every probe meant before any of them could say so."
  (cond
    ((null space) "Model")
    ((= space "") "Model")
    (t space)
  )
)

(defun mcp:current-space ( / cvport)
  "The space ssget \"_C\" can actually reach.

   This is the whole of the grid-map defect, in one function. ssget \"_C\" is
   SPACE-dependent, not view-dependent: measured on Draft 3 with CTAB=Layout1
   and CVPORT=1, a crossing window over paper coordinates and one over model
   coordinates both returned the same 24 paper-space entities. Model space was
   unreachable at any zoom. No save/restore ZOOM _E would have helped, because
   zoom was never what was wrong.

   TILEMODE 1 is the Model tab. In a layout, CVPORT 1 means paper space is
   current; anything else means the cursor is inside a floating viewport and
   model space is current."
  (if (= 1 (getvar "TILEMODE"))
    "Model"
    (progn
      (setq cvport (getvar "CVPORT"))
      (if (or (null cvport) (= cvport 1)) (getvar "CTAB") "Model")
    )
  )
)

(defun mcp:space-ss (space / nm)
  "Every entity in one space, or nil. Space-explicit and view-independent:
   ssget \"_X\" honours the 410 filter wherever the user happens to be."
  (setq nm (mcp:space-name space))
  (ssget "_X" (list (cons 410 nm)))
)

(defun mcp:space-ss-filtered (space extra / nm)
  "As mcp:space-ss, with additional ssget filter pairs appended."
  (setq nm (mcp:space-name space))
  (ssget "_X" (cons (cons 410 nm) extra))
)

;; There is deliberately no mcp:model-ss any more. A helper whose name means
;; "the entities" while its body means "the model-space entities" is how every
;; probe in this file came to answer a question nobody asked; call mcp:space-ss
;; with the space written down.

;; -----------------------------------------------------------------------
;; mcp:extents
;; -----------------------------------------------------------------------

(defun mcp:header-extents ( / emin emax)
  "EXTMIN/EXTMAX, or nil when they hold the empty-drawing sentinels."
  (setq emin (getvar "EXTMIN") emax (getvar "EXTMAX"))
  (if (or (null emin) (null emax) (> (abs (car emin)) 1e19) (> (abs (car emax)) 1e19))
    nil
    (list (car emin) (cadr emin) (car emax) (cadr emax))
  )
)

;; AutoLISP has no optional arguments: calling a one-argument defun with none
;; is "error: too few arguments", and discovering that costs a 10 s IPC round
;; trip. So every probe that gained a space keeps its original zero-argument
;; name meaning model space, and the space-taking form is a separate -in name.

(defun mcp:extents ( ) (mcp:extents-in "Model"))

(defun mcp:extents-in (space / ss i n box b out)
  "Computed extents, which is not what EXTMIN reports.
   EXTMIN/EXTMAX only update on a regen or a zoom-extents, so after an erase
   they routinely describe geometry that no longer exists. Reporting both, and
   naming which is which, is the difference between a cheap answer and a
   confidently wrong one.

   Also not what the header reports for a second reason: EXTMIN/EXTMAX describe
   the CURRENT space. On Draft 3 they read max [40.46, 22.50] - the paper sheet
   - while model space reaches [44.0, 59.46]. The space is named in the output
   so the two numbers can be told apart."
  (mcp:begin-output)
  (mcp:budget-init nil nil nil)
  (setq ss (mcp:space-ss space))
  (setq n (if ss (sslength ss) 0) i 0 box nil)
  (while (and (< i n) (mcp:budget-entity))
    (setq b (mcp:ent-bbox (ssname ss i)))
    (if b (setq box (mcp:bb-union box b)))
    (setq i (1+ i))
  )
  (setq out (strcat "{\"extents\":" (mcp:bb-json box)
                    ",\"extents_header\":" (mcp:bb-json (mcp:header-extents))
                    ",\"space\":\"" (mcp:esc (mcp:space-name space)) "\""
                    ",\"current_space\":\"" (mcp:esc (mcp:current-space)) "\""
                    ",\"entities\":" (itoa n)
                    "," (mcp:budget-json) "}"))
  (mcp:end-output)
  out
)

;; -----------------------------------------------------------------------
;; mcp:bbox-of
;; -----------------------------------------------------------------------

(defun mcp:bbox-of (handles / lst h ename box exact n data b out)
  "One box over a handful of named entities. Takes a handle string or a list
   of them: (mcp:bbox-of \"2A7\") or (mcp:bbox-of '(\"2A7\" \"2A8\"))."
  (mcp:begin-output)
  (setq lst (if (listp handles) handles (list handles)))
  (setq box nil exact T n 0)
  (foreach h lst
    (setq ename (handent h))
    (if ename
      (progn
        (setq data (entget ename))
        (setq b (mcp:ent-bbox-data data 0))
        (if b (setq box (mcp:bb-union box b)))
        (if (not (mcp:ent-exact-p data)) (setq exact nil))
        (setq n (1+ n))
      )
    )
  )
  (setq out (strcat "{\"bbox\":" (mcp:bb-json box)
                    ",\"count\":" (itoa n)
                    ",\"requested\":" (itoa (length lst))
                    ",\"exact\":" (if exact "true" "false") "}"))
  (mcp:end-output)
  out
)

;; -----------------------------------------------------------------------
;; mcp:bbox-by-layer
;; -----------------------------------------------------------------------

(defun mcp:bbox-by-layer ( ) (mcp:bbox-by-layer-in "Model"))

(defun mcp:bbox-by-layer-in (space / ss n i data lyr b acc rec out first)
  "What occupies which region, one line per layer - the L2 rung.
   For a panel drawing this is the whole layout in a few hundred tokens:
   every layer's footprint, which answers \"is the schedule still inside the
   border\" without rendering anything.

   One space at a time, and it says which. A layer is a document-wide NAME,
   not a place: on Draft 3, `border line 02` has entities only in Layout1 and
   `ECSI_Backpan` only in model space, so a single call can never show both -
   and one that silently showed model space made the border look like it did
   not exist."
  (mcp:begin-output)
  (mcp:budget-init nil nil nil)
  (setq ss (mcp:space-ss space))
  (setq n (if ss (sslength ss) 0) i 0 acc '())
  (while (and (< i n) (mcp:budget-entity))
    (setq data (entget (ssname ss i)))
    (setq lyr (mcp:group data 8 "0"))
    (setq b (mcp:ent-bbox-data data 0))
    (setq rec (assoc lyr acc))
    ;; Take the old record OUT of acc before pushing the updated one back.
    ;; Consing a placeholder here as well would leave two records per layer and
    ;; print the layer twice.
    (if (null rec)
      (setq rec (list lyr 0 nil T))
      (setq acc (vl-remove rec acc))
    )
    ;; count, box, exact - the exactness flag is set before the nil check on
    ;; purpose: an entity this code cannot measure at all leaves the layer's
    ;; footprint incomplete, which is the strongest reason not to call it exact.
    (setq rec (list lyr
                    (1+ (nth 1 rec))
                    (if b (mcp:bb-union (nth 2 rec) b) (nth 2 rec))
                    (and (nth 3 rec) (if b (mcp:ent-exact-p data) nil))))
    (setq acc (cons rec acc))
    (setq i (1+ i))
  )
  (setq acc (vl-sort acc '(lambda (a b) (< (car a) (car b)))))
  (setq out "" first T)
  (foreach rec acc
    (setq out (strcat out (if first "" ",")
                      "\"" (mcp:esc (nth 0 rec)) "\":{"
                      "\"count\":" (itoa (nth 1 rec))
                      ",\"bbox\":" (mcp:bb-json (nth 2 rec))
                      ",\"exact\":" (if (nth 3 rec) "true" "false") "}"))
    (setq first nil)
  )
  (setq out (strcat "{\"layers\":{" out "},\"entities\":" (itoa n)
                    ",\"space\":\"" (mcp:esc (mcp:space-name space)) "\""
                    "," (mcp:budget-json) "}"))
  (mcp:end-output)
  out
)

;; -----------------------------------------------------------------------
;; mcp:text-dump
;; -----------------------------------------------------------------------

(defun mcp:text-items (space / ss n i data kind c items)
  "Every string in one space with where it sits, sorted top-down then
   left-to-right so the dump reads the way the sheet does."
  (setq ss (mcp:space-ss-filtered space '((0 . "TEXT,MTEXT,ATTDEF"))))
  (setq n (if ss (sslength ss) 0) i 0 items '())
  (while (and (< i n) (mcp:budget-entity))
    (setq data (entget (ssname ss i)))
    (setq kind (mcp:group data 0 ""))
    (setq c (mcp:group data 10 '(0 0)))
    (setq items (cons (list (if (= kind "MTEXT") (mcp:mtext-string data)
                                                 (mcp:group data 1 ""))
                            (mcp:group data 8 "0")
                            (car c) (cadr c)
                            (mcp:group data 40 0.0)
                            kind
                            i)  ; index: see the tie-break below
                      items))
    (setq i (1+ i))
  )
  ;; vl-sort DISCARDS elements its predicate calls equal. Two identical labels
  ;; at the same point would silently become one, and the dump would disagree
  ;; with entity(count) for no visible reason. The index makes the order total,
  ;; so nothing ever compares equal.
  (vl-sort items
    '(lambda (a b)
       (cond
         ((not (equal (nth 3 a) (nth 3 b) 1e-9)) (> (nth 3 a) (nth 3 b)))
         ((not (equal (nth 2 a) (nth 2 b) 1e-9)) (< (nth 2 a) (nth 2 b)))
         ((/= (nth 0 a) (nth 0 b)) (< (nth 0 a) (nth 0 b)))
         (t (< (nth 6 a) (nth 6 b)))
       )))
)

(defun mcp:text-dump ( ) (mcp:text-dump-in "Model"))

(defun mcp:text-dump-in (space / items out first it)
  "Reading a label should never cost a screenshot.

   Not a cheap rung, though: measured on Draft 3's 84 labels this cost ~2.5-3K
   tokens, more than a full-window capture at 1280 (1,334). What it buys is
   exact strings and coordinates, which pixels cannot give. Pass a layer-heavy
   space or expect the bill."
  (mcp:begin-output)
  (mcp:budget-init nil nil nil)
  (setq items (mcp:text-items space))
  (setq out "" first T)
  (foreach it items
    (setq out (strcat out (if first "" ",")
                      "{\"text\":\"" (mcp:esc (nth 0 it)) "\""
                      ",\"layer\":\"" (mcp:esc (nth 1 it)) "\""
                      ",\"insert\":[" (mcp:fmt (nth 2 it)) "," (mcp:fmt (nth 3 it)) "]"
                      ",\"height\":" (mcp:fmt (nth 4 it))
                      ",\"type\":\"" (nth 5 it) "\"}"))
    (setq first nil)
  )
  (setq out (strcat "{\"text\":[" out "],\"count\":" (itoa (length items))
                    ",\"space\":\"" (mcp:esc (mcp:space-name space)) "\""
                    "," (mcp:budget-json) "}"))
  (mcp:end-output)
  out
)

;; -----------------------------------------------------------------------
;; mcp:overlap
;; -----------------------------------------------------------------------

(defun mcp:layer-bbox (lyr space / ss n i box b)
  "One layer's footprint in one space.

   The 410 filter was hardcoded to \"Model\" and that is what made mcp:overlap
   useless on Draft 3: `border line 02` lives entirely in Layout1, so this
   returned nil for it and the overlap check answered \"no overlap\" for a
   reason that had nothing to do with geometry."
  (setq ss (mcp:space-ss-filtered space (list (cons 8 lyr))))
  (setq n (if ss (sslength ss) 0) i 0 box nil)
  (while (and (< i n) (mcp:budget-entity))
    (setq b (mcp:ent-bbox (ssname ss i)))
    (if b (setq box (mcp:bb-union box b)))
    (setq i (1+ i))
  )
  box
)

(defun mcp:layer-spaces (lyr / out ss nm)
  "Which spaces a layer actually has entities in, as a list of names.
   Cheap - one ssget per space, no entget - and it is what stops mcp:overlap
   from comparing two boxes measured in different coordinate systems."
  (setq out '())
  (foreach nm (mcp:space-names)
    (setq ss (ssget "_X" (list (cons 410 nm) (cons 8 lyr))))
    (if (and ss (> (sslength ss) 0)) (setq out (cons nm out)))
  )
  (reverse out)
)

(defun mcp:space-names ( / out tab)
  "\"Model\" followed by every layout tab name - the same list
   probe_dxf.spaces() produces, in the same order."
  (setq out (list "Model"))
  (foreach tab (mcp:layout-tabs) (if (/= tab "Model") (setq out (cons tab out))))
  (reverse out)
)

(defun mcp:layout-tabs ( / dict out item)
  "Tab names from the ACAD_LAYOUT dictionary: group 3 is the name, 350 the
   layout object. dictsearch and namedobjdict are plain AutoLISP - no COM -
   which matters because the obvious route to this list is vla-get-Layouts,
   and creating a vla- object hangs the dispatcher on LT.

   Reading the dictionary rather than scanning entities for distinct 410
   values is not a style choice: the scan is 30k entget calls on Draft 3,
   which is most of the time budget spent on a list of three strings."
  (setq dict (dictsearch (namedobjdict) "ACAD_LAYOUT"))
  (setq out '())
  (foreach item dict (if (= (car item) 3) (setq out (cons (cdr item) out))))
  (reverse out)
)

(defun mcp:overlap (layer-a layer-b clearance)
  (mcp:overlap-in layer-a layer-b clearance nil))

(defun mcp:overlap-in (layer-a layer-b clearance space
                       / a b hit sep out sa sb shared nm)
  "Andy's review note 4 as a boolean: two rectangles, four comparisons.
   clearance promotes it from \"do they collide\" to \"do they clear each other
   by at least this much\", which is the actual drafting requirement.
   Touching is not overlapping - see mcp:bb-hit-open.

   overlaps is NULL, never false, whenever the comparison did not happen. That
   distinction is the entire lesson of the Draft 3 run: this probe reported
   \"no overlap\" between the border and the diagram, and it was right by
   accident and wrong in substance - the border is in paper space, the diagram
   in model space, and the model-only filter simply never saw the border. A
   false negative shaped exactly like a pass is worse than an error.

   Two layers in different spaces are reported as not comparable rather than
   unioned. Their coordinates are related by a viewport transform this code
   does not have; a number computed across them would mean nothing. Pass an
   explicit space (nil auto-detects a shared one) to compare within it."
  (mcp:begin-output)
  (mcp:budget-init nil nil nil)
  (if (null clearance) (setq clearance 0.0))
  (setq sa (mcp:layer-spaces layer-a))
  (setq sb (mcp:layer-spaces layer-b))
  (setq shared '())
  (foreach nm sa (if (member nm sb) (setq shared (cons nm shared))))
  (setq shared (reverse shared))
  (if space (setq nm (mcp:space-name space)) (setq nm (car shared)))
  (cond
    ;; No space holds both layers, so there is nothing to compare.
    ((null nm)
     (setq out (strcat "{\"overlaps\":null,\"comparable\":false"
                       ",\"reason\":\"layers occupy different spaces\""
                       ",\"intersection\":null,\"gap\":null,\"clears\":null"
                       ",\"spaces_a\":" (mcp:strings-json sa)
                       ",\"spaces_b\":" (mcp:strings-json sb)
                       ",\"space\":null"
                       "," (mcp:budget-json) "}")))
    (t
     (setq a (mcp:layer-bbox layer-a nm))
     (setq b (mcp:layer-bbox layer-b nm))
     (if (or (null a) (null b))
       (setq out (strcat "{\"overlaps\":null,\"comparable\":false"
                         ",\"reason\":\"empty layer in this space\""
                         ",\"intersection\":null,\"gap\":null,\"clears\":null"
                         ",\"bbox_a\":" (mcp:bb-json a)
                         ",\"bbox_b\":" (mcp:bb-json b)
                         ",\"spaces_a\":" (mcp:strings-json sa)
                         ",\"spaces_b\":" (mcp:strings-json sb)
                         ",\"space\":\"" (mcp:esc nm) "\""
                         "," (mcp:budget-json) "}"))
       (progn
         (setq hit (mcp:bb-hit-open a b))
         (setq sep (mcp:bb-gap a b))
         (setq out (strcat "{\"overlaps\":" (if hit "true" "false")
                           ",\"comparable\":true,\"reason\":null"
                           ",\"intersection\":" (mcp:bb-json (mcp:bb-inter a b))
                           ",\"gap\":" (mcp:fmt sep)
                           ",\"clears\":" (if (and (not hit) (>= sep clearance)) "true" "false")
                           ",\"bbox_a\":" (mcp:bb-json a)
                           ",\"bbox_b\":" (mcp:bb-json b)
                           ",\"spaces_a\":" (mcp:strings-json sa)
                           ",\"spaces_b\":" (mcp:strings-json sb)
                           ",\"space\":\"" (mcp:esc nm) "\""
                           "," (mcp:budget-json) "}"))
       )
     ))
  )
  (mcp:end-output)
  out
)

(defun mcp:strings-json (lst / out first s)
  (setq out "" first T)
  (foreach s lst
    (setq out (strcat out (if first "" ",") "\"" (mcp:esc s) "\""))
    (setq first nil)
  )
  (strcat "[" out "]")
)

;; -----------------------------------------------------------------------
;; mcp:grid-map
;; -----------------------------------------------------------------------

(defun mcp:cell-occupied (x1 y1 x2 y2 space / ss)
  "One crossing-window probe, in an explicitly named space.

   This is where AutoCAD earns its keep: ssget \"_C\" tests the true geometry,
   so a sheet border reads as a hollow frame instead of a solid block of
   occupancy. The Python reference has to model that with segment-versus-
   rectangle arithmetic; here it is one call.

   CALLABLE ONLY FOR THE CURRENT SPACE. ssget \"_C\" is space-dependent, not
   view-dependent: from Layout1 with CVPORT 1 it returns paper-space entities
   for a model-coordinate window and reports nothing missing. Callers must
   check mcp:current-space first - mcp:grid-map does, and switches to
   mcp:grid-raster when they differ. Left as a bare ssget rather than made
   self-checking on purpose: this is the fast path, called once per cell, and
   a getvar per call is not free."
  (setq ss (ssget "_C" (list x1 y1) (list x2 y2)
                  (list (cons 410 (mcp:space-name space)))))
  (if ss (> (sslength ss) 0) nil)
)

(defun mcp:grid-rows (region cols rows space / cw ch r c y-hi y-lo x-lo out row)
  "Strip probing.
   The naive form is cols*rows probes: a 24x12 grid is 288 ssget calls, and on
   a 30k-entity drawing that does not fit in the IPC budget. Each row is tested
   as one full-width band first; an empty band means every cell in it is empty,
   so the row costs 1 probe instead of cols. Drawings are mostly whitespace, so
   that is the common case. Worst case is rows + rows*cols, which is why the
   probe ceiling still exists.

   Cells past the budget are '?', never '.'. Reporting unprobed space as empty
   is how a probe lies: a caller reading '.' concludes there is nothing there."
  (setq cw (/ (- (nth 2 region) (nth 0 region)) (float cols)))
  (setq ch (/ (- (nth 3 region) (nth 1 region)) (float rows)))
  (setq out '() r 0)
  (while (< r rows)
    ;; Row 0 is the top of the region, so the grid reads like the drawing
    ;; rather than upside down.
    (setq y-hi (- (nth 3 region) (* r ch)))
    (setq y-lo (- y-hi ch))
    (cond
      ((not (mcp:budget-probe))
       (setq out (cons (mcp:repeat-char "?" cols) out)))
      ((not (mcp:cell-occupied (nth 0 region) y-lo (nth 2 region) y-hi space))
       (setq out (cons (mcp:repeat-char "." cols) out)))
      (t
       (setq row "" c 0)
       (while (< c cols)
         (setq x-lo (+ (nth 0 region) (* c cw)))
         (setq row (strcat row
           (cond
             ((not (mcp:budget-probe)) "?")
             ((mcp:cell-occupied x-lo y-lo (+ x-lo cw) y-hi space) "#")
             (t ".")
           )))
         (setq c (1+ c))
       )
       (setq out (cons row out)))
    )
    (setq r (1+ r))
  )
  (reverse out)
)

(defun mcp:repeat-char (ch n / s i)
  (setq s "" i 0)
  (while (< i n) (setq s (strcat s ch) i (1+ i)))
  s
)

;; -----------------------------------------------------------------------
;; Raster occupancy - the fallback for a space that is not current
;; -----------------------------------------------------------------------

(defun mcp:clamp (v lo hi) (cond ((< v lo) lo) ((> v hi) hi) (t v)))

(defun mcp:set-nth (lst idx val / out i)
  "Functional replace-by-index. AutoLISP has no arrays, and this list is
   `rows` long - sixteen, not thirty thousand - so rebuilding it per marked
   row is cheaper than any structure that would avoid it."
  (setq out '() i 0)
  (foreach v lst
    (setq out (cons (if (= i idx) val v) out))
    (setq i (1+ i))
  )
  (reverse out)
)

(defun mcp:space-boxes (space / ss n i b out)
  "Every measurable bounding box in one space, as a list.

   Collected once and held, rather than measured twice. The raster needs both
   the region and the boxes, and taking two passes to get them is two entget
   scans over 29,717 entities inside a 7 s budget - the second one is the one
   that runs out, and a grid that truncates is a grid of '?'. This is the only
   place the drawing is read."
  (setq ss (mcp:space-ss space))
  (setq n (if ss (sslength ss) 0) i 0 out '())
  (while (and (< i n) (mcp:budget-entity))
    (setq b (mcp:ent-bbox (ssname ss i)))
    (if b (setq out (cons b out)))
    (setq i (1+ i))
  )
  out
)

(defun mcp:bb-union-all (boxes / box b)
  (setq box nil)
  (foreach b boxes (setq box (mcp:bb-union box b)))
  box
)

(defun mcp:raster-expired ( )
  "Time-only stop for the marking loop. The entities were already charged
   against the ceiling when their boxes were collected, so charging them again
   would halve the effective budget; what still has to be bounded is the
   pathological drawing where every entity spans the whole grid."
  (cond
    (*mcp-bg-reason* T)
    ((mcp:budget-expired) (setq *mcp-bg-reason* "time") T)
    (t nil)
  )
)

(defun mcp:grid-raster (boxes region cols rows / b cw ch masks
                                                 c0 c1 r0 r1 r c y-hi y-lo x-lo
                                                 m out fill row)
  "Occupancy by marking cells from each entity's bbox, instead of probing each
   cell. Transcribed from probes.bbox_raster, which takes the same box list;
   TestRasterMatchesProbing holds it against grid_map(bbox_probe(...)).

   Why the loop is inverted: probing costs probes x entities. A 24x16 grid over
   Draft 3's 29,717 model-space entities is up to 12M comparisons, which does
   not happen inside the 7 s budget - the probe would truncate and answer '?'
   for the whole sheet. Marking costs entities x cells-touched, and nearly
   every entity touches one or two.

   Why it exists: mcp:cell-occupied cannot reach a space that is not current.
   This can, at the price of being conservative - a bounding box is not the
   geometry, so a hollow border reads as a solid block. The caller is told
   which mode produced the grid, because the two cannot be read the same way.

   Cells left unmarked when the scan truncates are '?', not '.'. Every '#' a
   partial scan reports was found honestly; it has established no '.' at all."
  (setq cw (/ (- (nth 2 region) (nth 0 region)) (float cols)))
  (setq ch (/ (- (nth 3 region) (nth 1 region)) (float rows)))
  (setq masks '() r 0)
  (while (< r rows) (setq masks (cons 0 masks) r (1+ r)))
  (foreach b boxes
    (if (not (mcp:raster-expired))
      (progn
        ;; Widen by a cell each way, then filter with the real comparison. A
        ;; closed-form index range has to decide what happens when an edge
        ;; lands exactly on a cell boundary, and drafting geometry lands on
        ;; boundaries constantly; this way the answer comes from
        ;; mcp:bb-hit-closed, the same rule the crossing grid uses.
        (setq c0 (mcp:clamp (1- (fix (/ (- (nth 0 b) (nth 0 region)) cw))) 0 (1- cols)))
        (setq c1 (mcp:clamp (1+ (fix (/ (- (nth 2 b) (nth 0 region)) cw))) 0 (1- cols)))
        (setq r0 (mcp:clamp (1- (fix (/ (- (nth 3 region) (nth 3 b)) ch))) 0 (1- rows)))
        (setq r1 (mcp:clamp (1+ (fix (/ (- (nth 3 region) (nth 1 b)) ch))) 0 (1- rows)))
        (setq r r0)
        (while (<= r r1)
          (setq y-hi (- (nth 3 region) (* r ch)))
          (setq y-lo (- y-hi ch))
          (setq m (nth r masks))
          (setq c c0)
          (while (<= c c1)
            (setq x-lo (+ (nth 0 region) (* c cw)))
            (if (mcp:bb-hit-closed (list x-lo y-lo (+ x-lo cw) y-hi) b)
              (setq m (logior m (lsh 1 c))))
            (setq c (1+ c))
          )
          (setq masks (mcp:set-nth masks r m))
          (setq r (1+ r))
        )
      )
    )
  )
  (setq fill (if *mcp-bg-reason* "?" "."))
  (setq out '())
  (foreach m masks
    (setq row "" c 0)
    (while (< c cols)
      (setq row (strcat row (if (/= 0 (logand m (lsh 1 c))) "#" fill)))
      (setq c (1+ c))
    )
    (setq out (cons row out))
  )
  (reverse out)
)

(defun mcp:grid-map (cols rows) (mcp:grid-map-in cols rows "Model"))

(defun mcp:grid-map-in (cols rows space / region ss total lines out first ln
                                          nm mode region-trunc boxes)
  "Does the layout read correctly, coarse - the L3 rung.

   Two modes, and the payload names which one ran, because they answer
   differently. \"crossing\" is ssget \"_C\" against the real geometry, so a
   sheet border reads as a hollow frame. \"bbox\" is mcp:grid-raster, which is
   conservative: it marks every cell an entity's bounding box touches, so that
   same border reads solid. Crossing is only available for the CURRENT space -
   see mcp:current-space for the measurement that establishes it - so a grid of
   model space taken from a layout tab is necessarily the conservative one.

   The drawing is read ONCE, and which pass that is depends on the mode.
   Crossing mode needs only the region, so it scans for extents and then lets
   ssget do the occupancy. Raster mode needs the region AND every box, so it
   collects the boxes and unions them - measuring twice would be two entget
   scans over 29,717 entities inside a 7 s budget, and the second is the one
   that runs out.

   The crossing path restarts the budget before probing. Sharing it with the
   region scan is what produced the first half of the Draft 3 failure:
   computing the region consumed all 20,000 entities of the ceiling and the
   grid came back entirely '?' at probes: 0. It reported unprobed rather than
   empty - the safeguard working - but it answered nothing.

   ssget also honours layer visibility, so a frozen or off layer contributes
   nothing in crossing mode. entities is reported alongside, so a grid that
   comes back empty against a non-empty drawing reads as a contradiction
   rather than as an empty sheet."
  (mcp:begin-output)
  (mcp:budget-init nil nil nil)
  (if (null cols) (setq cols 24))
  (if (null rows) (setq rows 12))
  ;; mcp:grid-raster packs a row into one integer bitmask, and AutoLISP's lsh
  ;; is 32-bit signed. Past 30 columns the mask would go negative and cells
  ;; would read empty.
  (if (> cols 30) (setq cols 30))
  (setq nm (mcp:space-name space))
  (setq mode (if (= nm (mcp:current-space)) "crossing" "bbox"))
  (if (= mode "crossing")
    (setq region (mcp:computed-extents-in nm))
    (progn
      (setq boxes (mcp:space-boxes nm))
      (setq region (mcp:bb-union-all boxes))
    )
  )
  (setq region-trunc *mcp-bg-reason*)
  (setq ss (mcp:space-ss nm))
  (setq total (if ss (sslength ss) 0))
  (if (or (null region)
          (<= (- (nth 2 region) (nth 0 region)) 0.0)
          (<= (- (nth 3 region) (nth 1 region)) 0.0))
    (setq out (strcat "{\"grid\":[],\"region\":null,\"cols\":" (itoa cols)
                      ",\"rows\":" (itoa rows) ",\"entities\":" (itoa total)
                      ",\"space\":\"" (mcp:esc nm) "\""
                      ",\"current_space\":\"" (mcp:esc (mcp:current-space)) "\""
                      ",\"mode\":\"" mode "\""
                      ",\"probes\":0," (mcp:budget-json) "}"))
    (progn
      ;; Crossing mode gets a fresh ceiling because its probes are a separate
      ;; cost from the region scan. Raster mode must NOT reset: its boxes were
      ;; already paid for above, and clearing the truncation reason here would
      ;; turn a partial scan's unmarked cells from '?' into '.' - the precise
      ;; lie this whole change exists to remove.
      (if (= mode "crossing") (mcp:budget-init nil nil nil))
      (setq lines (if (= mode "crossing")
                    (mcp:grid-rows region cols rows nm)
                    (mcp:grid-raster boxes region cols rows)))
      (setq out "" first T)
      (foreach ln lines
        (setq out (strcat out (if first "" ",") "\"" ln "\""))
        (setq first nil)
      )
      (setq out (strcat "{\"grid\":[" out "]"
                        ",\"region\":" (mcp:bb-json region)
                        ",\"cols\":" (itoa cols) ",\"rows\":" (itoa rows)
                        ",\"entities\":" (itoa total)
                        ",\"space\":\"" (mcp:esc nm) "\""
                        ",\"current_space\":\"" (mcp:esc (mcp:current-space)) "\""
                        ",\"mode\":\"" mode "\""
                        ",\"region_truncated\":"
                        (if region-trunc (strcat "\"" region-trunc "\"") "false")
                        ",\"probes\":" (itoa *mcp-bg-probes*)
                        "," (mcp:budget-json) "}"))
    )
  )
  (mcp:end-output)
  out
)

(defun mcp:computed-extents ( ) (mcp:computed-extents-in "Model"))

(defun mcp:computed-extents-in (space / ss n i box b)
  "Union of every entity bbox in one space. Separate from mcp:extents because
   the snapshot and the grid both need the value rather than the JSON."
  (setq ss (mcp:space-ss space))
  (setq n (if ss (sslength ss) 0) i 0 box nil)
  (while (and (< i n) (mcp:budget-entity))
    (setq b (mcp:ent-bbox (ssname ss i)))
    (if b (setq box (mcp:bb-union box b)))
    (setq i (1+ i))
  )
  box
)

;; -----------------------------------------------------------------------
;; mcp:snapshot
;; -----------------------------------------------------------------------

(defun mcp:hidden-layer-count ( / tbl n flags)
  "Layers that are off or frozen. ssget cannot select on them, so grid-map
   silently under-reports when any exist. Stating the count turns a confusing
   snapshot mismatch into an explained one."
  (setq n 0)
  (setq tbl (tblnext "LAYER" T))
  (while tbl
    (setq flags (mcp:group tbl 70 0))
    (if (or (< (mcp:group tbl 62 7) 0)   ; negative colour means off
            (= 1 (logand flags 1)))      ; bit 1 means frozen
      (setq n (1+ n)))
    (setq tbl (tblnext "LAYER"))
  )
  n
)

(defun mcp:snapshot (filepath) (mcp:snapshot-in filepath "Model"))

(defun mcp:snapshot-in (filepath space / cols rows fp ss n i data lyr b box acc
                                rec approx unmeasured truncated items it lines
                                ln exact tmp nm mode boxes)
  "The whole drawing as deterministic, diffable text, written to filepath.

   Modelled on Playwright's ARIA snapshots: commit the structure as text and
   diff it, so a regression is a failing test rather than something noticed in
   a screenshot three edits later. Pixels stay reserved for what only pixels
   can answer.

   Written to a file rather than returned through IPC on purpose - the payload
   would otherwise cost exactly what it is meant to save. Diff it against
   tests/golden/*.snap, which src/autocad_mcp/probes.py produces for the same
   drawing; that comparison is what validates this file."
  (mcp:begin-output)
  (mcp:budget-init nil nil nil)
  (setq cols 24 rows 12)
  (setq nm (mcp:space-name space))
  (setq mode (if (= nm (mcp:current-space)) "crossing" "bbox"))

  ;; --- scan ---
  (setq ss (mcp:space-ss nm))
  (setq n (if ss (sslength ss) 0) i 0 acc '() box nil approx 0 unmeasured 0
        boxes '())
  (while (and (< i n) (mcp:budget-entity))
    (setq data (entget (ssname ss i)))
    (setq lyr (mcp:group data 8 "0"))
    (setq b (mcp:ent-bbox-data data 0))
    (setq exact (mcp:ent-exact-p data))
    (if b
      (progn (setq box (mcp:bb-union box b))
             ;; Kept for the raster below. This loop is the only entget scan
             ;; the snapshot makes, and re-reading 29,717 entities to build
             ;; the grid would double the one cost that matters.
             (setq boxes (cons b boxes))
             (if (not exact) (setq approx (1+ approx))))
      (setq unmeasured (1+ unmeasured))
    )
    (setq rec (assoc lyr acc))
    (if (null rec)
      (setq rec (list lyr 0 nil T))
      (setq acc (vl-remove rec acc))
    )
    (setq acc (cons (list lyr (1+ (nth 1 rec))
                          (if b (mcp:bb-union (nth 2 rec) b) (nth 2 rec))
                          (and (nth 3 rec) (if b exact nil)))
                    acc))
    (setq i (1+ i))
  )
  (setq truncated (if (> n *mcp-bg-max-ent*) "yes" "no"))
  (setq acc (vl-sort acc '(lambda (a b) (< (car a) (car b)))))

  ;; --- write ---
  (setq tmp (strcat filepath ".tmp"))
  (setq fp (open tmp "w"))
  (if (null fp)
    (progn
      (mcp:end-output)
      (strcat "{\"ok\":false,\"error\":\"cannot open " (mcp:esc tmp) "\"}"))
    (progn
      (write-line (strcat "# mcp-snapshot v" (itoa *mcp-snapshot-version*)) fp)
      ;; Which space this describes. Without it, a snapshot of the wrong tab
      ;; is indistinguishable from a snapshot of a changed drawing - and on a
      ;; paper-space-composed sheet the two sides are routinely on different
      ;; tabs.
      (write-line (strcat "space " nm) fp)
      ;; A truncated snapshot that does not say so is worse than no snapshot:
      ;; the golden comparison would pass on partial data and call it a match.
      (write-line (strcat "entities " (itoa n)
                          " approx " (itoa approx)
                          " unmeasured " (itoa unmeasured)
                          " truncated " truncated) fp)
      (write-line (strcat "hidden-layers " (itoa (mcp:hidden-layer-count))) fp)
      (write-line (strcat "extents-header " (mcp:bb-text (mcp:header-extents))) fp)
      (write-line (strcat "extents-computed " (mcp:bb-text box)) fp)

      (foreach rec acc
        (write-line (strcat "layer " (nth 0 rec)
                            " count " (itoa (nth 1 rec))
                            " bbox " (mcp:bb-text (nth 2 rec))
                            " exact " (if (nth 3 rec) "yes" "no")) fp)
      )

      ;; Fresh ceiling per sub-probe, as the Python reference does. Carrying
      ;; the entity count over from the scan above would make the text dump
      ;; truncate immediately on any drawing large enough to matter.
      (mcp:budget-init *mcp-max-entities* *mcp-max-probes* nil)
      (setq items (mcp:text-items nm))
      (foreach it items
        (write-line (strcat "text " (nth 1 it) " "
                            (mcp:fmt (nth 2 it)) " " (mcp:fmt (nth 3 it))
                            " h " (mcp:fmt (nth 4 it))
                            " \"" (mcp:esc (nth 0 it)) "\"") fp)
      )

      (if (and box (> (- (nth 2 box) (nth 0 box)) 0.0)
                   (> (- (nth 3 box) (nth 1 box)) 0.0))
        (progn
          ;; The grid is the one part with no probe ceiling: a snapshot is a
          ;; deliberate, occasional act, and a partial grid would silently
          ;; differ from the fixture on every run.
          ;; Crossing mode only: its probes are a separate cost from the scan.
          ;; Raster mode must not reset, because budget-init clears the
          ;; truncation reason and mcp:grid-raster reads that to decide whether
          ;; an unmarked cell is '.' or '?'. Resetting it here would make a
          ;; partial scan assert an empty sheet.
          (if (= mode "crossing") (mcp:budget-init *mcp-max-entities* 1000000 nil))
          ;; The mode is part of the fixture because the two grids disagree by
          ;; design: crossing resolves a hollow border as hollow, bbox fills
          ;; it. A .snap taken in one mode and diffed against the other would
          ;; report a drawing change that did not happen.
          (write-line (strcat "grid " (itoa cols) "x" (itoa rows) " " mode " "
                              (mcp:bb-text box)) fp)
          (setq lines (if (= mode "crossing")
                        (mcp:grid-rows box cols rows nm)
                        (mcp:grid-raster boxes box cols rows)))
          (foreach ln lines (write-line (strcat "grid | " ln " |") fp))
        )
        (write-line "grid none" fp)
      )

      (close fp)
      (if (findfile filepath) (vl-file-delete filepath))
      (vl-file-rename tmp filepath)
      (mcp:end-output)
      (strcat "{\"ok\":true,\"path\":\"" (mcp:esc filepath) "\""
              ",\"entities\":" (itoa n)
              ",\"layers\":" (itoa (length acc))
              ",\"space\":\"" (mcp:esc nm) "\""
              ",\"mode\":\"" mode "\""
              ",\"text\":" (itoa (length items)) "}")
    )
  )
)

(princ "\nmcp_probes.lsp loaded: mcp:extents mcp:bbox-of mcp:bbox-by-layer mcp:text-dump mcp:overlap mcp:grid-map mcp:snapshot")
(princ "\n  space-explicit forms: mcp:extents-in mcp:bbox-by-layer-in mcp:text-dump-in mcp:overlap-in mcp:grid-map-in mcp:snapshot-in")
(princ)
