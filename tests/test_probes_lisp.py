"""Structural checks on lisp-code/*.lsp — the only checks possible offline.

execute_lisp exists only on the file_ipc backend, so nothing in these files can
be run without AutoCAD in front of it. That is precisely why the probe
arithmetic lives in src/autocad_mcp/probes.py and is tested there; this file
covers the class of mistake that a Python reference cannot catch, namely a .lsp
that will not parse or that has drifted out of step with its reference.

An unbalanced paren does not produce an error message. It produces a dispatcher
that silently stops responding, and a session that spends ten seconds per call
discovering it.
"""

import re
from pathlib import Path

import pytest

from autocad_mcp.probes import (
    DEFAULT_MAX_ENTITIES,
    DEFAULT_MAX_PROBES,
    NOMINAL_CHAR_WIDTH,
    PROBE_TIME_BUDGET_MS,
    SNAPSHOT_VERSION,
)

LISP_DIR = Path(__file__).parent.parent / "lisp-code"
PROBES_LSP = LISP_DIR / "mcp_probes.lsp"
DISPATCH_LSP = LISP_DIR / "mcp_dispatch.lsp"

#: The probes the capture ladder's L2 and L3 rungs name, plus the rest of the
#: set, each mapped to the defun that actually does the work.
#:
#: They differ because AutoLISP has no optional arguments: a probe that gained
#: a space parameter keeps its original zero-argument name as a wrapper meaning
#: model space, so that every call already written — and the ladder in the
#: skill — still works. The assertions below have to look inside the
#: implementation, not the wrapper.
PROBE_IMPL = {
    "mcp:extents": "mcp:extents-in",
    "mcp:bbox-of": "mcp:bbox-of",  # handles are document-wide; no space needed
    "mcp:bbox-by-layer": "mcp:bbox-by-layer-in",
    "mcp:text-dump": "mcp:text-dump-in",
    "mcp:overlap": "mcp:overlap-in",
    "mcp:grid-map": "mcp:grid-map-in",
    "mcp:snapshot": "mcp:snapshot-in",
}

PUBLIC_PROBES = list(PROBE_IMPL)


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_lisp(text: str) -> str:
    """Blank out comments and string literals so paren counting means something.

    Length-preserving: characters are replaced by spaces rather than removed,
    so an index into the result still points at the same character of the
    original. _defun_body slices the original using offsets found here.
    """
    out = list(text)
    in_string = False
    in_comment = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_comment:
            if ch == "\n":
                in_comment = False
            else:
                out[i] = " "
            i += 1
            continue
        if in_string:
            out[i] = " "
            if ch == "\\":
                if i + 1 < len(text):
                    out[i + 1] = " "
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == ";":
            in_comment = True
            out[i] = " "
            i += 1
            continue
        if ch == '"':
            in_string = True
            out[i] = " "
            i += 1
            continue
        i += 1
    return "".join(out)


@pytest.mark.parametrize("name", ["mcp_probes.lsp", "mcp_dispatch.lsp", "attribute_tools.lsp"])
class TestParseable:
    def test_parens_balance(self, name):
        """The failure this catches is not a syntax error, it is silence: an
        unbalanced file leaves the dispatcher waiting for a form that never
        closes, and every call after it times out."""
        code = strip_lisp(source(LISP_DIR / name))
        depth = 0
        line = 1
        for ch in code:
            if ch == "\n":
                line += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                assert depth >= 0, f"{name}: unmatched ')' at line {line}"
        assert depth == 0, f"{name}: {depth} unclosed '(' at end of file"

    def test_masking_preserves_offsets(self, name):
        """_defun_body finds offsets in the masked text and slices the
        original; a length change there would silently misreport bodies."""
        text = source(LISP_DIR / name)
        assert len(strip_lisp(text)) == len(text)

    def test_no_tabs_in_source(self, name):
        """AutoCAD's editor and this repo disagree about tab width, and a
        misread indent in a nested cond is how a paren goes missing."""
        assert "\t" not in source(LISP_DIR / name)


class TestEveryCallResolves:
    """A misspelled function name is not a load error in AutoLISP.

    The file loads, the banner prints, and the mistake surfaces only when a
    caller reaches that branch — as "no function definition", one 10 s IPC
    round trip later, possibly on the drawing that mattered. Same class of
    failure as the unbalanced paren above, and equally checkable offline.
    """

    @pytest.mark.parametrize("name", ["mcp_probes.lsp", "mcp_dispatch.lsp"])
    def test_no_call_to_an_undefined_mcp_function(self, name):
        text = source(LISP_DIR / name)
        code = strip_lisp(text)
        defined = set(re.findall(r"\(defun ([\w:-]+) ", code))
        if name == "mcp_probes.lsp":
            # mcp_probes.lsp is loaded alongside the dispatcher, but is written
            # to work without it — see the local mcp:esc.
            defined |= set(re.findall(r"\(defun ([\w:-]+) ", strip_lisp(source(DISPATCH_LSP))))
        else:
            defined |= set(re.findall(r"\(defun ([\w:-]+) ", strip_lisp(source(PROBES_LSP))))
            defined |= set(re.findall(r"\(defun ([\w:-]+) ", strip_lisp(source(LISP_DIR / "attribute_tools.lsp"))))
        called = set(re.findall(r"\((mcp[:-][\w:-]+)", code))
        assert called - defined == set(), (
            f"{name} calls functions nothing defines: {sorted(called - defined)}"
        )

    @pytest.mark.parametrize("name", ["mcp_probes.lsp", "mcp_dispatch.lsp"])
    def test_no_function_is_defined_twice(self, name):
        """The second definition wins silently, and which one that is depends
        on load order."""
        names = re.findall(r"\(defun ([\w:-]+) ", strip_lisp(source(LISP_DIR / name)))
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert dupes == [], f"{name} defines these more than once: {dupes}"


class TestProbesArePresent:
    @pytest.mark.parametrize("probe", PUBLIC_PROBES)
    def test_defined(self, probe):
        assert f"(defun {probe} " in source(PROBES_LSP), f"{probe} is not defined"

    def test_load_banner_names_every_probe(self):
        """A load banner that lists a probe the file does not define is worse
        than no banner — it is what you check when something is missing."""
        banner = [l for l in source(PROBES_LSP).splitlines() if "mcp_probes.lsp loaded" in l]
        assert len(banner) == 1
        for name in PUBLIC_PROBES:
            assert name in banner[0]


class TestTunablesMatchTheReference:
    """The two layers are only a check on each other while their constants
    agree. A silent drift here makes the golden .snap comparison meaningless
    without making anything fail."""

    @pytest.mark.parametrize(
        "lisp_name,value",
        [
            ("*mcp-snapshot-version*", SNAPSHOT_VERSION),
            ("*mcp-time-budget-ms*", PROBE_TIME_BUDGET_MS),
            ("*mcp-max-entities*", DEFAULT_MAX_ENTITIES),
            ("*mcp-max-probes*", DEFAULT_MAX_PROBES),
        ],
    )
    def test_integer_tunable(self, lisp_name, value):
        assert f"(setq {lisp_name} {value})" in source(PROBES_LSP)

    def test_nominal_char_width(self):
        assert f"(setq *mcp-nominal-char-width* {NOMINAL_CHAR_WIDTH})" in source(PROBES_LSP)

    def test_time_budget_stays_under_the_ipc_timeout(self):
        from autocad_mcp.config import IPC_TIMEOUT

        assert PROBE_TIME_BUDGET_MS < IPC_TIMEOUT * 1000


class TestNoActiveX:
    """AutoCAD LT has no COM interface, and vla- object creation hangs the
    dispatcher outright. The probes measure from entget group codes for that
    reason — and because two layers that measure differently cannot check each
    other."""

    def test_no_vla_calls(self):
        code = strip_lisp(source(PROBES_LSP))
        offenders = [tok for tok in ("vla-", "vlax-", "vl-load-com") if tok in code]
        assert offenders == [], f"ActiveX in mcp_probes.lsp: {offenders}"

    def test_no_command_calls(self):
        """A probe must not change the drawing, the view, or the undo stack.
        `(command "_.ZOOM" ...)` in a read-only probe would be a side effect
        that outlives the answer."""
        assert "(command " not in strip_lisp(source(PROBES_LSP))


class TestBudgetIsWiredIn:
    """The 10 s IPC timeout is the constraint that shapes these probes. Every
    loop that can run the length of the drawing has to be bounded."""

    @pytest.mark.parametrize(
        "func",
        [
            "mcp:extents-in",
            "mcp:bbox-by-layer-in",
            "mcp:snapshot-in",
            "mcp:computed-extents-in",
            "mcp:space-boxes",
        ],
    )
    def test_iterating_probes_charge_the_budget(self, func):
        body = _defun_body(source(PROBES_LSP), func)
        assert "mcp:budget-entity" in body, f"{func} scans without a ceiling"

    def test_the_raster_is_bounded_by_the_clock_not_the_entity_count(self):
        """Its boxes were charged when mcp:space-boxes collected them.
        Charging them again would halve the effective ceiling — but the loop
        still has to stop, because a drawing where every entity spans the whole
        grid is cells-touched x entities and unbounded otherwise."""
        body = _defun_body(source(PROBES_LSP), "mcp:grid-raster")
        assert "mcp:raster-expired" in body
        assert "mcp:budget-entity" not in body

    def test_grid_map_charges_probes_not_entities(self):
        body = _defun_body(source(PROBES_LSP), "mcp:grid-rows")
        assert "mcp:budget-probe" in body

    def test_unprobed_cells_are_unknown_not_empty(self):
        """Reporting unprobed space as empty is how a probe lies."""
        body = _defun_body(source(PROBES_LSP), "mcp:grid-rows")
        assert '"?"' in body

    def test_strip_probing_is_present(self):
        """Two probe sites: one full-width band per row, then per-cell only
        when the band hits. Without the band this is 288 ssget calls for a
        24x12 grid, which does not fit in the budget on a 30k-entity drawing."""
        body = _defun_body(source(PROBES_LSP), "mcp:grid-rows")
        assert body.count("(mcp:cell-occupied") == 2

    def test_block_recursion_is_depth_capped(self):
        assert "(< depth 4)" in source(PROBES_LSP)

    def test_snapshot_gives_each_sub_probe_a_fresh_ceiling(self):
        """As the Python reference does. Carrying the entity count over from
        the layer scan would make the text dump truncate immediately on any
        drawing large enough for the ceiling to matter."""
        body = _defun_body(source(PROBES_LSP), "mcp:snapshot-in")
        assert body.count("(mcp:budget-init") >= 3

    def test_grid_map_restarts_the_budget_after_computing_the_region(self):
        """The first half of the Draft 3 failure. Computing the region over
        29,717 entities spent the whole 20,000-entity ceiling, so the grid ran
        with nothing left and came back entirely '?' at probes: 0. Honest, and
        an answer to no question."""
        body = _defun_body(source(PROBES_LSP), "mcp:grid-map-in")
        assert body.count("(mcp:budget-init") >= 2


class TestSpaceIsExplicit:
    """The Draft 3 defect. Every probe hardcoded `(410 . "Model")` and
    mcp:cell-occupied used `ssget "_C"`, which is SPACE-dependent rather than
    view-dependent: measured from Layout1 with CVPORT 1, a crossing window over
    paper coordinates and one over model coordinates both returned the same 24
    paper-space entities, and model space was unreachable at any zoom. The grid
    came back solid '.' — asserting confirmed-empty — against 29,717 entities.
    """

    @pytest.mark.parametrize(
        "func",
        [
            "mcp:extents-in",
            "mcp:bbox-by-layer-in",
            "mcp:text-items",
            "mcp:layer-bbox",
            "mcp:computed-extents-in",
            "mcp:space-boxes",
            "mcp:cell-occupied",
        ],
    )
    def test_no_probe_hardcodes_model_space(self, func):
        body = _defun_body(source(PROBES_LSP), func)
        assert '410 . "Model"' not in body, (
            f"{func} still hardcodes model space, so it answers a question "
            f"other than the one it was asked on a composed sheet"
        )

    @pytest.mark.parametrize("public,impl", sorted(PROBE_IMPL.items()))
    def test_the_zero_argument_form_still_exists(self, public, impl):
        """AutoLISP has no optional arguments: calling a one-argument defun
        with none is 'too few arguments', discovered one 10 s IPC round trip
        at a time. Every documented call has to keep working."""
        assert f"(defun {public} " in source(PROBES_LSP)
        assert f"(defun {impl} " in source(PROBES_LSP)

    def test_grid_map_picks_its_mode_from_the_current_space(self):
        """ssget "_C" cannot reach a space that is not current, so a grid of
        model space taken from a layout tab has to fall back to the raster."""
        body = _defun_body(source(PROBES_LSP), "mcp:grid-map-in")
        assert "mcp:current-space" in body
        assert "mcp:grid-rows" in body and "mcp:grid-raster" in body

    def test_the_grid_says_which_mode_produced_it(self):
        """A bbox grid fills a hollow border and a crossing grid does not.
        Reading one as the other is a wrong conclusion, not a rounding error."""
        assert '\\"mode\\":\\"' in _defun_body(source(PROBES_LSP), "mcp:grid-map-in")
        # The snapshot carries it on the grid line, since .snap is not JSON.
        assert '"grid "' in _defun_body(source(PROBES_LSP), "mcp:snapshot-in")
        assert 'mode " "' in _defun_body(source(PROBES_LSP), "mcp:snapshot-in")

    def test_current_space_accounts_for_a_floating_viewport(self):
        """In a layout, CVPORT 1 means paper space is current; anything else
        means the cursor is inside a viewport and model space is."""
        body = _defun_body(source(PROBES_LSP), "mcp:current-space")
        assert '"TILEMODE"' in body and '"CVPORT"' in body and '"CTAB"' in body

    def test_raster_leaves_unscanned_cells_unknown(self):
        """Same rule as the crossing grid: a truncated scan has established no
        '.' at all, only the '#' it actually found."""
        body = _defun_body(source(PROBES_LSP), "mcp:grid-raster")
        assert '"?"' in body

    def test_the_drawing_is_read_once_per_grid(self):
        """Raster mode needs the region and every box. Measuring them in two
        passes is two entget scans over 29,717 entities inside a 7 s budget,
        and the second is the one that runs out."""
        body = _defun_body(source(PROBES_LSP), "mcp:grid-map-in")
        assert "mcp:space-boxes" in body and "mcp:bb-union-all" in body
        # Crossing mode gets extents the cheap way; raster mode must not also
        # call it, or the saving is given straight back.
        assert body.count("mcp:computed-extents-in") == 1

    def test_a_truncated_raster_still_reports_unknown_cells(self):
        """budget-init clears the truncation reason, and mcp:grid-raster reads
        exactly that to decide between '.' and '?'. Resetting it before the
        raster would make a partial scan assert an empty sheet — the original
        defect, reintroduced one layer down."""
        for func in ("mcp:grid-map-in", "mcp:snapshot-in"):
            body = _defun_body(source(PROBES_LSP), func)
            assert '(if (= mode "crossing") (mcp:budget-init' in body, (
                f"{func} restarts the budget unconditionally before the grid"
            )

    def test_raster_bitmask_cannot_overflow(self):
        """A row is packed into one integer and AutoLISP's lsh is 32-bit
        signed. Past 30 columns the mask goes negative and cells read empty —
        the exact failure this whole class exists to prevent."""
        body = _defun_body(source(PROBES_LSP), "mcp:grid-map-in")
        assert "(> cols 30)" in body


class TestOverlapRefusesRatherThanGuesses:
    """mcp:overlap answered 'no overlap' on Draft 3 because `border line 02` is
    entirely in Layout1 and mcp:layer-bbox was model-only. It was a false
    negative shaped exactly like a pass."""

    def test_overlaps_is_null_when_the_comparison_did_not_happen(self):
        body = _defun_body(source(PROBES_LSP), "mcp:overlap-in")
        assert '\\"overlaps\\":null' in body
        assert '\\"comparable\\":false' in body

    def test_it_checks_which_spaces_the_layers_occupy(self):
        """Two layers in different spaces have coordinates related by a
        viewport transform this code does not have. Unioning them produces a
        number that means nothing."""
        body = _defun_body(source(PROBES_LSP), "mcp:overlap-in")
        assert "mcp:layer-spaces" in body

    def test_layout_tabs_come_from_the_dictionary_not_an_entity_scan(self):
        """Scanning 30k entities for distinct 410 values is most of the time
        budget spent on a list of three strings."""
        body = _defun_body(source(PROBES_LSP), "mcp:layout-tabs")
        assert "namedobjdict" in body and "ACAD_LAYOUT" in body


class TestLispSortDoesNotDropRows:
    """vl-sort discards elements its predicate calls equal — an AutoLISP
    behaviour with no counterpart in Python's sorted(), and the kind of thing a
    reference implementation cannot catch for you."""

    def test_text_sort_has_a_total_order_tiebreak(self):
        body = _defun_body(source(PROBES_LSP), "mcp:text-items")
        assert "(nth 6 a)" in body, (
            "mcp:text-items sorts without a unique final key, so two identical "
            "labels at the same point would silently become one"
        )


class TestSnapshotFormatMatches:
    """Field-by-field agreement with probes.snapshot. The .snap fixture is only
    a contract if both sides emit the same lines in the same order."""

    @pytest.mark.parametrize(
        "literal",
        ['"# mcp-snapshot v"', '"space "', '"entities "', '" approx "',
         '" unmeasured "', '" truncated "', '"hidden-layers "',
         '"extents-header "', '"extents-computed "', '"layer "', '" count "',
         '" bbox "', '" exact "', '"text "', '" h "', '"grid "', '"grid | "',
         '"grid none"'],
    )
    def test_emits_the_field(self, literal):
        body = _defun_body(source(PROBES_LSP), "mcp:snapshot-in")
        assert literal in body, f"mcp:snapshot never writes {literal}"

    def test_writes_atomically(self):
        """mcp_dispatch.lsp writes results through a .tmp and a rename; a
        half-written .snap read by a diff is a false regression."""
        body = _defun_body(source(PROBES_LSP), "mcp:snapshot-in")
        assert "vl-file-rename" in body

    def test_pins_dimzin_around_output(self):
        """rtos honours DIMZIN for zero suppression, so without this the
        snapshot format depends on a drawing setting rather than on the
        drawing."""
        assert '(setvar "DIMZIN" 0)' in source(PROBES_LSP)
        for impl in PROBE_IMPL.values():
            body = _defun_body(source(PROBES_LSP), impl)
            assert "mcp:begin-output" in body, f"{impl} does not pin DIMZIN"

    def test_snapshot_writes_to_a_file_rather_than_the_ipc_payload(self):
        """Returning the snapshot through execute_lisp would cost exactly what
        it exists to save."""
        body = _defun_body(source(PROBES_LSP), "mcp:snapshot-in")
        assert "(open tmp \"w\")" in body


def _defun_body(text: str, name: str) -> str:
    """Source of one defun, by paren matching."""
    start = text.index(f"(defun {name} ")
    depth = 0
    stripped = strip_lisp(text)
    # Paren-match on the stripped text but slice the original, so comments
    # inside the body stay readable to the assertions above.
    offset = start
    for i in range(start, len(text)):
        ch = stripped[i] if i < len(stripped) else ""
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[offset : i + 1]
    raise AssertionError(f"{name} is not closed")
