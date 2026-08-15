"""Reference implementations of the probe logic in lisp-code/mcp_probes.lsp.

`execute_lisp` exists only on the file_ipc backend, so no line of AutoLISP in
this repo can be exercised without a running AutoCAD. Writing the probes as
AutoLISP first would mean shipping untested arithmetic and discovering the bugs
at the drawing, one 10-second IPC round trip at a time.

So the arithmetic lives here, where pytest and the ezdxf backend hold it against
tests/golden/*.dxf, and mcp_probes.lsp is a transcription of logic that already
passes. The two must agree: the .snap fixtures are the contract between them,
and the same drawing must produce byte-identical snapshot text from either side.

Everything here is pure — no ezdxf, no I/O, no AutoCAD. Entities arrive as the
plain dicts entity(get) already returns. See probe_dxf.py for the ezdxf adapter.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from autocad_mcp.config import IPC_TIMEOUT

#: v2 added the `space` line and the grid's mode. Both exist because v1 could
#: not say which space it described, and a snapshot of the wrong space is
#: indistinguishable from a snapshot of a changed drawing.
SNAPSHOT_VERSION = 2

#: Nominal glyph advance as a fraction of text height.
#:
#: Deliberately a constant rather than a real font measurement. AutoLISP cannot
#: measure a glyph without ActiveX, so anything more accurate here would make
#: the Python and the LISP disagree — and the whole point of two layers is that
#: they cannot. Text bounding boxes are therefore approximate, and every probe
#: that uses one says so.
NOMINAL_CHAR_WIDTH = 0.6

#: Fraction of the IPC timeout a probe may spend before giving up.
#:
#: AUTOCAD_MCP_IPC_TIMEOUT defaults to 10 s. A probe that runs to 10 s does not
#: return a slow answer, it returns no answer at all: Python has already stopped
#: polling for the result file. The remainder covers writing the result and the
#: poll interval that notices it.
TIME_BUDGET_FRACTION = 0.7

PROBE_TIME_BUDGET_MS = int(IPC_TIMEOUT * 1000 * TIME_BUDGET_FRACTION)

#: Entity-count ceiling for a full scan. A 20-30k entity drawing is the case
#: that matters; past this the probe truncates and says it truncated rather
#: than silently blowing the IPC deadline.
DEFAULT_MAX_ENTITIES = 20000

#: Probe ceiling for grid-map. Each probe is an ssget "_C" in AutoLISP.
DEFAULT_MAX_PROBES = 400

OCCUPIED = "#"
EMPTY = "."
UNKNOWN = "?"


# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------


def fmt(value: float) -> str:
    """Six decimal places, and never "-0.000000".

    Negative zero is the one float that breaks a golden-file comparison for no
    reason at all: -1e-9 and +1e-9 both round to zero but print with different
    signs, so a snapshot flips between runs on a value that did not change.
    AutoLISP's rtos has the same hazard.
    """
    text = f"{value:.6f}"
    if text.startswith("-") and float(text) == 0.0:
        return text[1:]
    return text


# ---------------------------------------------------------------------------
# Bounding boxes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BBox:
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @classmethod
    def of_points(cls, points: Iterable[Sequence[float]]) -> "BBox | None":
        xs: list[float] = []
        ys: list[float] = []
        for p in points:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
        if not xs:
            return None
        return cls(min(xs), min(ys), max(xs), max(ys))

    def union(self, other: "BBox | None") -> "BBox":
        if other is None:
            return self
        return BBox(
            min(self.xmin, other.xmin),
            min(self.ymin, other.ymin),
            max(self.xmax, other.xmax),
            max(self.ymax, other.ymax),
        )

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    def as_list(self) -> list[float]:
        return [self.xmin, self.ymin, self.xmax, self.ymax]

    def fmt(self) -> str:
        return " ".join(fmt(v) for v in self.as_list())


def bbox_union(boxes: Iterable[BBox | None]) -> BBox | None:
    result: BBox | None = None
    for b in boxes:
        if b is None:
            continue
        result = b if result is None else result.union(b)
    return result


def intersects_closed(a: BBox, b: BBox) -> bool:
    """Touching counts as a hit.

    This is AutoCAD's crossing-window rule, so it is what grid-map's cell
    probes use: an entity that ends exactly on a cell boundary lights both
    cells, in the grid and in `ssget "_C"` alike.
    """
    return a.xmin <= b.xmax and b.xmin <= a.xmax and a.ymin <= b.ymax and b.ymin <= a.ymax


def intersects_open(a: BBox, b: BBox) -> bool:
    """Touching does not count as a hit.

    This is the rule for "the border must not overlap the diagram". A border
    line that lands exactly on the diagram's edge is flush, not overlapping,
    and reporting it as a collision would make the check useless.
    """
    return a.xmin < b.xmax and b.xmin < a.xmax and a.ymin < b.ymax and b.ymin < a.ymax


def intersection(a: BBox, b: BBox) -> BBox | None:
    if not intersects_open(a, b):
        return None
    return BBox(
        max(a.xmin, b.xmin),
        max(a.ymin, b.ymin),
        min(a.xmax, b.xmax),
        min(a.ymax, b.ymax),
    )


def gap(a: BBox, b: BBox) -> float:
    """Shortest distance between two boxes; 0.0 when they touch or overlap.

    Andy's note 4 wants clearance, not just absence of collision. This turns
    "does the border clear the diagram by at least 2 units" into arithmetic.
    """
    dx = max(0.0, max(a.xmin - b.xmax, b.xmin - a.xmax))
    dy = max(0.0, max(a.ymin - b.ymax, b.ymin - a.ymax))
    return math.hypot(dx, dy)


# ---------------------------------------------------------------------------
# Per-entity geometry
# ---------------------------------------------------------------------------


def _angle_in_sweep(angle: float, start: float, end: float) -> bool:
    """Is `angle` inside the CCW sweep from `start` to `end`? Degrees."""
    span = (end - start) % 360.0
    if span == 0.0:
        # A DXF arc whose start and end coincide is a full circle, not a
        # zero-length arc. Treating it as empty loses the whole entity.
        return True
    return ((angle - start) % 360.0) <= span


def arc_bbox(cx: float, cy: float, r: float, start_deg: float, end_deg: float) -> BBox:
    """Tight bbox of an arc.

    Not center +- r: a 0-90 degree arc occupies one quadrant, and using the
    full circle's box would inflate it fourfold and produce phantom overlaps.
    The extremes are the two endpoints plus whichever of the four cardinal
    directions the sweep actually passes through.
    """
    pts = [
        (cx + r * math.cos(math.radians(start_deg)), cy + r * math.sin(math.radians(start_deg))),
        (cx + r * math.cos(math.radians(end_deg)), cy + r * math.sin(math.radians(end_deg))),
    ]
    for cardinal, (dx, dy) in ((0, (1, 0)), (90, (0, 1)), (180, (-1, 0)), (270, (0, -1))):
        if _angle_in_sweep(cardinal, start_deg, end_deg):
            pts.append((cx + r * dx, cy + r * dy))
    box = BBox.of_points(pts)
    assert box is not None
    return box


def ellipse_bbox(cx: float, cy: float, major_x: float, major_y: float, ratio: float) -> BBox:
    """Axis-aligned bbox of a rotated full ellipse.

    x(t) = cx + a.cos(t).cos(th) - b.sin(t).sin(th), whose amplitude is
    hypot(a.cos(th), b.sin(th)); y likewise with the terms swapped.

    Conservative for elliptical *arcs* — the DXF start/end parameters are
    ignored and the full ellipse is measured. A superset bbox can report an
    overlap that is not there, which is the safe direction to be wrong in.
    """
    a = math.hypot(major_x, major_y)
    b = a * ratio
    theta = math.atan2(major_y, major_x)
    hx = math.hypot(a * math.cos(theta), b * math.sin(theta))
    hy = math.hypot(a * math.sin(theta), b * math.cos(theta))
    return BBox(cx - hx, cy - hy, cx + hx, cy + hy)


def text_bbox(
    insert: Sequence[float],
    text: str,
    height: float,
    rotation: float = 0.0,
    anchor_top: bool = False,
) -> BBox:
    """Nominal box for a text string. Approximate by construction.

    Width is len(text) * height * NOMINAL_CHAR_WIDTH — see that constant for
    why this is not a real font measurement. `anchor_top` is for MTEXT, whose
    insertion point is at the top of the first line rather than the baseline.
    """
    w = len(text) * height * NOMINAL_CHAR_WIDTH
    x0, y0 = float(insert[0]), float(insert[1])
    lo, hi = (-height, 0.0) if anchor_top else (0.0, height)
    corners = [(0.0, lo), (w, lo), (w, hi), (0.0, hi)]
    if rotation:
        rad = math.radians(rotation)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        corners = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r) for x, y in corners]
    box = BBox.of_points([(x0 + x, y0 + y) for x, y in corners])
    assert box is not None
    return box


def _transform_bbox(box: BBox, ox: float, oy: float, sx: float, sy: float, rot: float) -> BBox:
    """Scale, rotate and translate a box by transforming its four corners.

    Conservative under rotation: the box around the rotated corners is at least
    as large as the box around the rotated geometry. `bbox_is_exact` reports
    that, so a caller can tell a measured box from a padded one.
    """
    corners = [
        (box.xmin * sx, box.ymin * sy),
        (box.xmax * sx, box.ymin * sy),
        (box.xmax * sx, box.ymax * sy),
        (box.xmin * sx, box.ymax * sy),
    ]
    if rot:
        rad = math.radians(rot)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        corners = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r) for x, y in corners]
    result = BBox.of_points([(ox + x, oy + y) for x, y in corners])
    assert result is not None
    return result


#: Types whose bbox is measured rather than estimated.
_EXACT_TYPES = {"LINE", "CIRCLE", "ARC", "POINT", "LWPOLYLINE", "POLYLINE"}


def bbox_is_exact(info: dict) -> bool:
    """False when the box is nominal, conservative, or padded.

    Worth carrying separately from the box itself. An exact box supports "these
    do not overlap"; an approximate one only supports "these might".
    """
    kind = info.get("type")
    if kind in ("LWPOLYLINE", "POLYLINE"):
        # Bulges arc *outside* the vertex hull, so a bulged polyline is
        # under-reported. This is the one case where the error runs toward a
        # missed overlap rather than a phantom one.
        return not info.get("has_bulge", False)
    if kind == "ELLIPSE":
        return False  # full-ellipse superset, see ellipse_bbox
    if kind == "INSERT":
        return False  # block contents padded by the corner transform
    if kind in ("TEXT", "MTEXT", "ATTDEF"):
        return False  # nominal glyph advance
    return kind in _EXACT_TYPES


def entity_bbox(
    info: dict,
    blocks: dict[str, list[dict]] | None = None,
    _depth: int = 0,
) -> BBox | None:
    """Bbox from an entity(get)-shaped dict, or None if it has no geometry.

    `blocks` maps block name to its member entity dicts, so an INSERT can be
    resolved to what it actually draws rather than to its insertion point.
    """
    kind = info.get("type")

    if kind == "LINE":
        return BBox.of_points([info["start"], info["end"]])

    if kind == "CIRCLE":
        cx, cy = info["center"][:2]
        r = float(info["radius"])
        return BBox(cx - r, cy - r, cx + r, cy + r)

    if kind == "ARC":
        cx, cy = info["center"][:2]
        return arc_bbox(cx, cy, float(info["radius"]), float(info["start_angle"]), float(info["end_angle"]))

    if kind == "ELLIPSE":
        cx, cy = info["center"][:2]
        mx, my = info["major_axis"][:2]
        return ellipse_bbox(cx, cy, float(mx), float(my), float(info["ratio"]))

    if kind in ("LWPOLYLINE", "POLYLINE"):
        return BBox.of_points(info.get("vertices", []))

    if kind in ("TEXT", "ATTDEF"):
        return text_bbox(info["insert"], info.get("text", ""), float(info.get("height", 0.0)),
                         float(info.get("rotation", 0.0)))

    if kind == "MTEXT":
        return text_bbox(info["insert"], info.get("text", ""), float(info.get("height", 0.0)),
                         float(info.get("rotation", 0.0)), anchor_top=True)

    if kind == "POINT":
        x, y = info["location"][:2]
        return BBox(x, y, x, y)

    if kind == "INSERT":
        ox, oy = info["insert"][:2]
        members = (blocks or {}).get(info.get("name", ""))
        # Depth cap: a block that references itself is invalid but does exist
        # in the wild, and unbounded recursion inside a 10 s budget is fatal.
        if not members or _depth >= 4:
            return BBox(ox, oy, ox, oy)
        inner = bbox_union(entity_bbox(m, blocks, _depth + 1) for m in members)
        if inner is None:
            return BBox(ox, oy, ox, oy)
        return _transform_bbox(
            inner, ox, oy,
            float(info.get("xscale", 1.0)), float(info.get("yscale", 1.0)),
            float(info.get("rotation", 0.0)),
        )

    return None


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class Budget:
    """Entity, probe and wall-clock ceilings for an iterating probe.

    An unbounded scan of a 30k-entity drawing does not return late — it returns
    nothing, because Python stopped polling for the result file at
    AUTOCAD_MCP_IPC_TIMEOUT and reported a dead dispatcher. A truncated answer
    that says it is truncated beats a timeout that says nothing.

    `clock` returns milliseconds and is injectable so the deadline is testable
    without actually waiting.
    """

    def __init__(
        self,
        max_entities: int = DEFAULT_MAX_ENTITIES,
        max_probes: int = DEFAULT_MAX_PROBES,
        time_ms: int = PROBE_TIME_BUDGET_MS,
        clock: Callable[[], float] | None = None,
    ):
        self.max_entities = max_entities
        self.max_probes = max_probes
        self.time_ms = time_ms
        self._clock = clock or (lambda: time.monotonic() * 1000.0)
        self.entities = 0
        self.probes = 0
        self.reason: str | None = None
        self._t0 = self._clock()

    def restart(self) -> "Budget":
        self.entities = 0
        self.probes = 0
        self.reason = None
        self._t0 = self._clock()
        return self

    @property
    def elapsed_ms(self) -> float:
        return self._clock() - self._t0

    @property
    def exhausted(self) -> bool:
        return self.reason is not None

    def spend_entity(self) -> bool:
        """Charge one entity. False means stop scanning."""
        if self.reason:
            return False
        if self.entities >= self.max_entities:
            self.reason = "max_entities"
            return False
        if self.elapsed_ms > self.time_ms:
            self.reason = "time"
            return False
        self.entities += 1
        return True

    def spend_probe(self) -> bool:
        """Charge one crossing-window probe. False means stop probing."""
        if self.reason:
            return False
        if self.probes >= self.max_probes:
            self.reason = "max_probes"
            return False
        if self.elapsed_ms > self.time_ms:
            self.reason = "time"
            return False
        self.probes += 1
        return True


# ---------------------------------------------------------------------------
# The probes
# ---------------------------------------------------------------------------


def extents(entities: Sequence[dict], blocks=None, budget: Budget | None = None) -> dict:
    """mcp:extents — computed extents, which is not what $EXTMIN reports.

    EXTMIN/EXTMAX only update on a regen or a zoom-extents, so after an erase
    they routinely describe geometry that no longer exists. Reporting both, and
    naming which is which, is the difference between a cheap answer and a
    confidently wrong one.
    """
    budget = budget or Budget()
    box: BBox | None = None
    scanned = 0
    for e in entities:
        if not budget.spend_entity():
            break
        scanned += 1
        this = entity_bbox(e, blocks)
        if this is not None:
            box = this if box is None else box.union(this)
    return {
        "extents": box.as_list() if box else None,
        "entities": len(entities),
        "scanned": scanned,
        "truncated": budget.exhausted,
        "truncated_reason": budget.reason,
    }


def bbox_of(entities: Sequence[dict], blocks=None) -> dict:
    """mcp:bbox-of — one box over a handful of named entities."""
    boxes = [entity_bbox(e, blocks) for e in entities]
    box = bbox_union(boxes)
    return {
        "bbox": box.as_list() if box else None,
        "count": len(entities),
        "exact": all(bbox_is_exact(e) for e in entities) if entities else True,
    }


def bbox_by_layer(
    entities: Sequence[dict],
    blocks=None,
    budget: Budget | None = None,
    layers: Sequence[str] | None = None,
) -> dict:
    """mcp:bbox-by-layer — what occupies which region, one line per layer.

    The L2 rung. For a panel drawing this is the whole layout in a few hundred
    tokens: every layer's footprint, which is enough to answer "is the schedule
    still inside the border" without rendering anything.
    """
    budget = budget or Budget()
    wanted = set(layers) if layers else None
    boxes: dict[str, BBox | None] = {}
    counts: dict[str, int] = {}
    approx: dict[str, bool] = {}
    scanned = 0

    for e in entities:
        layer = e.get("layer", "0")
        if wanted is not None and layer not in wanted:
            continue
        if not budget.spend_entity():
            break
        scanned += 1
        counts[layer] = counts.get(layer, 0) + 1
        box = entity_bbox(e, blocks)
        # Marked before the None check on purpose: an entity this code cannot
        # measure at all leaves the layer's footprint incomplete, which is the
        # strongest reason of all not to call the result exact.
        if box is None or not bbox_is_exact(e):
            approx[layer] = True
        if box is None:
            continue
        boxes[layer] = box if boxes.get(layer) is None else boxes[layer].union(box)

    return {
        "layers": {
            name: {
                "count": counts[name],
                "bbox": boxes[name].as_list() if boxes.get(name) else None,
                "exact": not approx.get(name, False),
            }
            for name in sorted(counts)
        },
        "scanned": scanned,
        "truncated": budget.exhausted,
        "truncated_reason": budget.reason,
    }


def text_dump(
    entities: Sequence[dict],
    budget: Budget | None = None,
    layers: Sequence[str] | None = None,
) -> dict:
    """mcp:text-dump — every string in the drawing, with where it sits.

    Reading a label should never cost a screenshot. Ordered top-to-bottom then
    left-to-right so the dump reads the way the sheet does.
    """
    budget = budget or Budget()
    wanted = set(layers) if layers else None
    items = []
    scanned = 0

    for e in entities:
        if e.get("type") not in ("TEXT", "MTEXT", "ATTDEF"):
            continue
        layer = e.get("layer", "0")
        if wanted is not None and layer not in wanted:
            continue
        if not budget.spend_entity():
            break
        scanned += 1
        insert = e.get("insert", [0.0, 0.0])
        items.append({
            "text": e.get("text", ""),
            "layer": layer,
            "insert": [float(insert[0]), float(insert[1])],
            "height": float(e.get("height", 0.0)),
            "type": e["type"],
        })

    items.sort(key=lambda it: (-it["insert"][1], it["insert"][0], it["text"]))
    return {
        "text": items,
        "count": len(items),
        "scanned": scanned,
        "truncated": budget.exhausted,
        "truncated_reason": budget.reason,
    }


def overlap(
    a: BBox | None,
    b: BBox | None,
    clearance: float = 0.0,
    comparable: bool = True,
    reason: str | None = None,
) -> dict:
    """mcp:overlap — Andy's review note 4 as a boolean.

    "The border must not overlap the diagram" is two rectangles and four
    comparisons. `clearance` promotes it from "do they collide" to "do they
    clear each other by at least this much", which is the actual drafting
    requirement.

    Touching is not overlapping — see intersects_open.

    `overlaps` is None, not False, whenever the question was not actually
    answered. That distinction is not pedantry: on Draft 3 this probe returned
    "no overlap" because the border is in paper space and the diagram in model
    space, so one of the two boxes was empty — a false negative wearing the
    exact same shape as a pass. False means measured and clear; None means the
    comparison did not happen, and `reason` says why.

    `comparable=False` is for boxes that exist but cannot be compared —
    two layers in different spaces, whose coordinates are related by a viewport
    transform this code does not have. Unioning them would produce a number,
    and the number would mean nothing.
    """
    if not comparable or a is None or b is None:
        return {
            "overlaps": None,
            "intersection": None,
            "gap": None,
            "clears": None,
            "comparable": comparable,
            "reason": reason or ("empty layer" if comparable else "not comparable"),
        }
    hit = intersects_open(a, b)
    inter = intersection(a, b)
    separation = gap(a, b)
    return {
        "overlaps": hit,
        "intersection": inter.as_list() if inter else None,
        "gap": separation,
        "clears": (not hit) and separation >= clearance,
        "comparable": True,
        "reason": None,
    }


def grid_map(
    probe: Callable[[float, float, float, float], bool],
    region: BBox,
    cols: int = 16,
    rows: int = 8,
    budget: Budget | None = None,
) -> dict:
    """mcp:grid-map — coarse occupancy, by strip probing.

    The naive form is cols*rows crossing-window probes: a 24x12 grid is 288
    ssget calls, and on a 30k-entity drawing that does not fit in the IPC
    budget. Strip probing tests each row as one full-width band first; an empty
    band means every cell in it is empty, so the row costs 1 probe instead of
    `cols`. Drawings are mostly whitespace, so this is the common case.

    Worst case (every row occupied) is rows + rows*cols, which is why the probe
    budget still exists. Cells past the budget are UNKNOWN, never EMPTY —
    reporting unprobed space as empty is how a probe lies.

    grid_map_naive is the oracle: for any input the two must produce the same
    grid, which is the property that lets the fast path be trusted.
    """
    budget = budget or Budget()
    cw = region.width / cols
    ch = region.height / rows
    grid: list[str] = []

    for r in range(rows):
        # Row 0 is the top of the region, so the grid reads like the drawing
        # rather than upside down.
        y_hi = region.ymax - r * ch
        y_lo = y_hi - ch

        if not budget.spend_probe():
            grid.append(UNKNOWN * cols)
            continue

        if not probe(region.xmin, y_lo, region.xmax, y_hi):
            grid.append(EMPTY * cols)
            continue

        row_chars: list[str] = []
        for c in range(cols):
            x_lo = region.xmin + c * cw
            if not budget.spend_probe():
                row_chars.append(UNKNOWN)
                continue
            row_chars.append(OCCUPIED if probe(x_lo, y_lo, x_lo + cw, y_hi) else EMPTY)
        grid.append("".join(row_chars))

    return {
        "grid": grid,
        "cols": cols,
        "rows": rows,
        "region": region.as_list(),
        # Named because a bbox_raster grid cannot be read the same way: this
        # one resolves a hollow border as hollow, that one does not.
        "mode": "crossing",
        "probes": budget.probes,
        "truncated": budget.exhausted,
        "truncated_reason": budget.reason,
    }


def grid_map_naive(
    probe: Callable[[float, float, float, float], bool],
    region: BBox,
    cols: int = 16,
    rows: int = 8,
) -> dict:
    """Every cell probed, no strip skipping. The oracle for grid_map.

    Kept in the module rather than the test file because it is the definition
    of what grid_map means; the fast version is only correct relative to it.
    """
    cw = region.width / cols
    ch = region.height / rows
    grid = []
    probes = 0
    for r in range(rows):
        y_hi = region.ymax - r * ch
        y_lo = y_hi - ch
        row = []
        for c in range(cols):
            x_lo = region.xmin + c * cw
            probes += 1
            row.append(OCCUPIED if probe(x_lo, y_lo, x_lo + cw, y_hi) else EMPTY)
        grid.append("".join(row))
    return {"grid": grid, "cols": cols, "rows": rows, "region": region.as_list(), "probes": probes}


def bbox_probe(boxes: Sequence[BBox]) -> Callable[[float, float, float, float], bool]:
    """A coarse occupancy probe backed by bounding boxes only.

    NOT a model of `ssget "_C"`, and must not be used to build a grid that is
    meant to match one: a 200x100 border rectangle has a bbox covering the
    whole sheet, so every interior cell reads as occupied while AutoCAD's
    crossing window would select nothing there. Use geometry_probe for that.
    Kept for the cases where a conservative superset is what is wanted.
    """

    def probe(x1: float, y1: float, x2: float, y2: float) -> bool:
        window = BBox(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        return any(intersects_closed(window, b) for b in boxes)

    return probe


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else (hi if v > hi else v)


def bbox_raster(
    boxes: Sequence[BBox],
    region: BBox,
    cols: int = 24,
    rows: int = 12,
    budget: Budget | None = None,
) -> dict:
    """Occupancy by marking cells from each box, rather than probing each cell.

    Same grid as ``grid_map(bbox_probe(boxes), ...)`` — TestRasterMatchesProbing
    pins that — but the loop is inverted, and the inversion is the entire point.

    Probing costs probes x entities. A 24x16 grid against Draft 3's 29,717
    entities is up to 12M comparisons, and AutoLISP does not do that inside a
    7 s IPC budget; the probe would truncate and answer '?'. Marking costs
    entities x cells-touched, and almost every entity touches one or two.

    Why this exists at all: `ssget "_C"` is the cheap way to test a cell and it
    models the *geometry*, so a sheet border reads as a hollow frame. But it
    only ever sees the current space — measured, it returned the same 24
    paper-space entities for a paper-coordinate box and a model-coordinate box
    while CTAB was Layout1 — so a grid of model space taken from a layout tab
    cannot use it at any zoom. This is the fallback for that case: view-
    independent and space-explicit, at the cost of being conservative. A box is
    not the geometry, so a hollow border reads solid. Callers are told which
    mode produced the grid because the two cannot be read the same way.

    On truncation the unmarked cells are UNKNOWN, never EMPTY. A partial scan
    has found every '#' it reports honestly; it has established no '.' at all.
    """
    budget = budget or Budget()
    marked = [[False] * cols for _ in range(rows)]
    cw = region.width / cols
    ch = region.height / rows

    if cw > 0 and ch > 0:
        for box in boxes:
            if not budget.spend_entity():
                break
            # Widen by a cell on each side, then filter with the real
            # comparison. A closed-form index range has to decide what happens
            # when a box edge lands exactly on a cell boundary, and drafting
            # geometry lands on boundaries constantly; this way the answer
            # comes from intersects_closed, the same rule bbox_probe uses.
            c0 = _clamp(int(math.floor((box.xmin - region.xmin) / cw)) - 1, 0, cols - 1)
            c1 = _clamp(int(math.floor((box.xmax - region.xmin) / cw)) + 1, 0, cols - 1)
            r0 = _clamp(int(math.floor((region.ymax - box.ymax) / ch)) - 1, 0, rows - 1)
            r1 = _clamp(int(math.floor((region.ymax - box.ymin) / ch)) + 1, 0, rows - 1)
            for r in range(r0, r1 + 1):
                y_hi = region.ymax - r * ch
                y_lo = y_hi - ch
                for c in range(c0, c1 + 1):
                    x_lo = region.xmin + c * cw
                    if intersects_closed(BBox(x_lo, y_lo, x_lo + cw, y_hi), box):
                        marked[r][c] = True

    fill = UNKNOWN if budget.exhausted else EMPTY
    return {
        "grid": ["".join(OCCUPIED if m else fill for m in row) for row in marked],
        "cols": cols,
        "rows": rows,
        "region": region.as_list(),
        "mode": "bbox",
        "scanned": budget.entities,
        "truncated": budget.exhausted,
        "truncated_reason": budget.reason,
    }


#: Segments per full circle when flattening curves.
#:
#: The divergence risk this controls: AutoCAD tests the true curve, this tests
#: a chord approximation, and they disagree when a chord's sagitta is
#: comparable to a grid cell. At 5 degrees a radius-30 circle has 2.6-unit
#: chords, well under a typical cell.
CURVE_SEGMENTS = 72


def _point_in_rect(p: Sequence[float], r: BBox) -> bool:
    return r.xmin <= p[0] <= r.xmax and r.ymin <= p[1] <= r.ymax


def _segments_cross(p1, p2, p3, p4) -> bool:
    """Proper or improper intersection of two closed segments."""

    def orient(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a, b, c) -> bool:
        return (
            min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
        )

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    # Collinear-touch cases, which matter because drafting geometry is full of
    # segments that land exactly on a boundary.
    for d, a, b, c in ((d1, p3, p4, p1), (d2, p3, p4, p2), (d3, p1, p2, p3), (d4, p1, p2, p4)):
        if d == 0 and on_segment(a, b, c):
            return True
    return False


def segment_hits_rect(p1: Sequence[float], p2: Sequence[float], rect: BBox) -> bool:
    """AutoCAD's crossing-window rule for one segment.

    Selected if either end is inside the window, or if the segment crosses any
    of its four edges. That second clause is what makes a hollow rectangle read
    as hollow: a cell in the middle of a border touches none of its edges.
    """
    if _point_in_rect(p1, rect) or _point_in_rect(p2, rect):
        return True
    corners = [
        (rect.xmin, rect.ymin), (rect.xmax, rect.ymin),
        (rect.xmax, rect.ymax), (rect.xmin, rect.ymax),
    ]
    return any(_segments_cross(p1, p2, corners[i], corners[(i + 1) % 4]) for i in range(4))


def _rect_segments(box: BBox) -> list[tuple]:
    c = [
        (box.xmin, box.ymin), (box.xmax, box.ymin),
        (box.xmax, box.ymax), (box.xmin, box.ymax),
    ]
    return [(c[i], c[(i + 1) % 4]) for i in range(4)]


def _flatten_arc(cx, cy, r, start_deg, end_deg) -> list[tuple]:
    span = (end_deg - start_deg) % 360.0
    if span == 0.0:
        span = 360.0  # equal angles encode a full circle — see _angle_in_sweep
    steps = max(2, int(round(CURVE_SEGMENTS * span / 360.0)))
    pts = []
    for i in range(steps + 1):
        a = math.radians(start_deg + span * i / steps)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def entity_segments(info: dict, blocks=None, _depth: int = 0) -> list[tuple]:
    """An entity as line segments — what a crossing window actually tests.

    Curves are flattened to CURVE_SEGMENTS per turn. Entities with no drawable
    outline of their own (TEXT, an unresolvable INSERT) contribute their bbox
    outline, which is what AutoCAD selects them by anyway.
    """
    kind = info.get("type")

    if kind == "LINE":
        return [(tuple(info["start"][:2]), tuple(info["end"][:2]))]

    if kind == "CIRCLE":
        cx, cy = info["center"][:2]
        return _flatten_arc(cx, cy, float(info["radius"]), 0.0, 360.0)

    if kind == "ARC":
        cx, cy = info["center"][:2]
        return _flatten_arc(cx, cy, float(info["radius"]),
                            float(info["start_angle"]), float(info["end_angle"]))

    if kind == "ELLIPSE":
        cx, cy = info["center"][:2]
        mx, my = info["major_axis"][:2]
        a = math.hypot(mx, my)
        b = a * float(info["ratio"])
        th = math.atan2(my, mx)
        pts = []
        for i in range(CURVE_SEGMENTS + 1):
            t = 2 * math.pi * i / CURVE_SEGMENTS
            x, y = a * math.cos(t), b * math.sin(t)
            pts.append((cx + x * math.cos(th) - y * math.sin(th),
                        cy + x * math.sin(th) + y * math.cos(th)))
        return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]

    if kind in ("LWPOLYLINE", "POLYLINE"):
        # Bulges are flattened to their chords. Consistent with the bbox, which
        # also ignores them; both under-report a bulged segment.
        verts = [tuple(v[:2]) for v in info.get("vertices", [])]
        if len(verts) < 2:
            return [(verts[0], verts[0])] if verts else []
        segs = [(verts[i], verts[i + 1]) for i in range(len(verts) - 1)]
        if info.get("closed"):
            segs.append((verts[-1], verts[0]))
        return segs

    if kind == "POINT":
        p = tuple(info["location"][:2])
        return [(p, p)]

    if kind == "INSERT":
        ox, oy = info["insert"][:2]
        members = (blocks or {}).get(info.get("name", ""))
        if not members or _depth >= 4:
            return [((ox, oy), (ox, oy))]
        sx = float(info.get("xscale", 1.0))
        sy = float(info.get("yscale", 1.0))
        rad = math.radians(float(info.get("rotation", 0.0)))
        cos_r, sin_r = math.cos(rad), math.sin(rad)

        def place(p):
            x, y = p[0] * sx, p[1] * sy
            return (ox + x * cos_r - y * sin_r, oy + x * sin_r + y * cos_r)

        out = []
        for m in members:
            out.extend((place(a), place(b)) for a, b in entity_segments(m, blocks, _depth + 1))
        return out

    box = entity_bbox(info, blocks)
    return _rect_segments(box) if box else []


#: Types AutoCAD selects by their outline. Everything else is selected by its
#: extents as a solid region — a crossing window entirely inside a piece of
#: text picks up that text, it does not fall through the middle of it.
_OUTLINE_TYPES = {
    "LINE", "CIRCLE", "ARC", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "POINT", "INSERT",
}


def selects_as_solid(info: dict) -> bool:
    return info.get("type") not in _OUTLINE_TYPES


def geometry_probe(
    entities: Sequence[dict], blocks=None
) -> Callable[[float, float, float, float], bool]:
    """A crossing-window probe that models `ssget "_C"`.

    This is the one grid_map should use. The difference from bbox_probe is not
    cosmetic: a sheet border reads as a hollow frame rather than as a solid
    block of occupancy, which is the whole reason the grid is worth reading.

    Each entity is pre-filtered by its bbox before its segments are tested, so
    the common miss stays cheap.
    """
    prepared = []
    for e in entities:
        box = entity_bbox(e, blocks)
        if box is None:
            continue
        segs = None if selects_as_solid(e) else entity_segments(e, blocks)
        prepared.append((box, segs))

    def probe(x1: float, y1: float, x2: float, y2: float) -> bool:
        window = BBox(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        for box, segs in prepared:
            if not intersects_closed(window, box):
                continue
            if segs is None:  # solid: the bbox overlap is the answer
                return True
            if any(segment_hits_rect(a, b, window) for a, b in segs):
                return True
        return False

    return probe


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def snapshot(
    entities: Sequence[dict],
    blocks: dict[str, list[dict]] | None = None,
    header_extents: BBox | None = None,
    cols: int = 24,
    rows: int = 12,
    budget: Budget | None = None,
    hidden_layers: int = 0,
    space: str = "Model",
) -> str:
    """mcp:snapshot — the whole drawing as deterministic, diffable text.

    Modelled on Playwright's ARIA snapshots: commit the structure as text and
    diff it, so a regression is a failing test rather than something you notice
    in a screenshot three edits later. Pixels stay reserved for what only
    pixels can answer.

    Line-oriented and sorted throughout, because the output is read as a
    unified diff. Every number goes through fmt() — see there for why.

    What it does not catch, stated so it is not assumed: this records each
    layer's *envelope* plus a coarse grid, not per-entity coordinates. An
    entity that is not on its layer's envelope, moved by less than a grid
    cell, changes no line here. That question belongs to L1 — entity(get) on
    the handle — and a snapshot of a 30k-entity drawing that listed every
    coordinate would cost what it exists to save.
    """
    blocks = blocks or {}
    budget = budget or Budget(max_entities=DEFAULT_MAX_ENTITIES, clock=lambda: 0.0)

    measured = [(e, entity_bbox(e, blocks)) for e in entities[: budget.max_entities]]
    boxes = [b for _, b in measured if b is not None]
    computed = bbox_union(boxes)

    by_layer = bbox_by_layer(entities, blocks, budget=Budget(
        max_entities=budget.max_entities, clock=lambda: 0.0))
    texts = text_dump(entities, budget=Budget(
        max_entities=budget.max_entities, clock=lambda: 0.0))

    approx = sum(1 for e, b in measured if b is not None and not bbox_is_exact(e))
    unmeasured = sum(1 for _, b in measured if b is None)
    truncated = len(entities) > budget.max_entities

    lines = [
        f"# mcp-snapshot v{SNAPSHOT_VERSION}",
        # Which space this describes. A snapshot that does not name it is
        # indistinguishable from a snapshot of a changed drawing when the two
        # sides happen to be looking at different tabs — and on a
        # paper-space-composed sheet they routinely are.
        f"space {space}",
        # A truncated snapshot that does not say so is worse than no snapshot:
        # the golden comparison would pass on partial data and call it a match.
        f"entities {len(entities)} approx {approx} unmeasured {unmeasured} "
        f"truncated {'yes' if truncated else 'no'}",
        # ssget cannot select on a layer that is off or frozen, so the LISP
        # grid under-reports whenever any exist while this side, reading the
        # DXF directly, does not. Stating the count turns an unexplained
        # snapshot mismatch into an explained one.
        f"hidden-layers {hidden_layers}",
        f"extents-header {header_extents.fmt() if header_extents else 'none'}",
        f"extents-computed {computed.fmt() if computed else 'none'}",
    ]

    for name, info in by_layer["layers"].items():
        box = BBox(*info["bbox"]) if info["bbox"] else None
        lines.append(
            f"layer {name} count {info['count']} "
            f"bbox {box.fmt() if box else 'none'} "
            f"exact {'yes' if info['exact'] else 'no'}"
        )

    for item in texts["text"]:
        lines.append(
            f"text {item['layer']} {fmt(item['insert'][0])} {fmt(item['insert'][1])} "
            f"h {fmt(item['height'])} {_quote(item['text'])}"
        )

    if computed is not None and computed.width > 0 and computed.height > 0:
        mapped = grid_map(geometry_probe([e for e, _ in measured], blocks), computed,
                          cols=cols, rows=rows,
                          budget=Budget(max_probes=10**6, clock=lambda: 0.0))
        # The mode is part of the fixture because the two grids disagree by
        # design: crossing resolves a hollow border as hollow, bbox fills it.
        lines.append(f"grid {cols}x{rows} {mapped['mode']} {computed.fmt()}")
        for row in mapped["grid"]:
            lines.append(f"grid | {row} |")
    else:
        lines.append("grid none")

    return "\n".join(lines) + "\n"


def _quote(text: str) -> str:
    """One-line, quoted, escaped. MTEXT carries real newlines."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'
