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

#: The probes the capture ladder's L2 and L3 rungs name, plus the rest of the set.
PUBLIC_PROBES = [
    "mcp:extents",
    "mcp:bbox-of",
    "mcp:bbox-by-layer",
    "mcp:text-dump",
    "mcp:overlap",
    "mcp:grid-map",
    "mcp:snapshot",
]


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
        "func", ["mcp:extents", "mcp:bbox-by-layer", "mcp:snapshot", "mcp:computed-extents"]
    )
    def test_iterating_probes_charge_the_budget(self, func):
        body = _defun_body(source(PROBES_LSP), func)
        assert "mcp:budget-entity" in body, f"{func} scans without a ceiling"

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
        body = _defun_body(source(PROBES_LSP), "mcp:snapshot")
        assert body.count("(mcp:budget-init") >= 3


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
        ['"# mcp-snapshot v"', '"entities "', '" approx "', '" unmeasured "',
         '" truncated "', '"hidden-layers "', '"extents-header "',
         '"extents-computed "', '"layer "', '" count "', '" bbox "', '" exact "',
         '"text "', '" h "', '"grid "', '"grid | "', '"grid none"'],
    )
    def test_emits_the_field(self, literal):
        body = _defun_body(source(PROBES_LSP), "mcp:snapshot")
        assert literal in body, f"mcp:snapshot never writes {literal}"

    def test_writes_atomically(self):
        """mcp_dispatch.lsp writes results through a .tmp and a rename; a
        half-written .snap read by a diff is a false regression."""
        body = _defun_body(source(PROBES_LSP), "mcp:snapshot")
        assert "vl-file-rename" in body

    def test_pins_dimzin_around_output(self):
        """rtos honours DIMZIN for zero suppression, so without this the
        snapshot format depends on a drawing setting rather than on the
        drawing."""
        assert '(setvar "DIMZIN" 0)' in source(PROBES_LSP)
        for probe in PUBLIC_PROBES:
            body = _defun_body(source(PROBES_LSP), probe)
            assert "mcp:begin-output" in body, f"{probe} does not pin DIMZIN"

    def test_snapshot_writes_to_a_file_rather_than_the_ipc_payload(self):
        """Returning the snapshot through execute_lisp would cost exactly what
        it exists to save."""
        body = _defun_body(source(PROBES_LSP), "mcp:snapshot")
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
