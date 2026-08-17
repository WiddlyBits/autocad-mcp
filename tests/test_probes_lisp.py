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


class TestFileDialogsAreSuppressed:
    """A modal dialog is not an error. The dispatcher stops answering, Python
    waits out the full 10 s IPC window, and the call comes back as a timeout
    with no hint that a Save As box is sitting on Gianni's screen — so the guard
    has to be structural, not remembered when each branch is written.

    QSAVE is deliberately not in MODAL_COMMANDS: it only raises a dialog on a
    drawing that has never been saved, so the check would be conditional on
    document state rather than on the source. That case is handled in the
    branch instead, by the DWGTITLED refusal that TestSaveIsHonestAboutNotSaving
    ::test_refuses_to_qsave_a_drawing_that_has_never_been_saved pins.
    """

    MODAL_COMMANDS = ['"_.SAVEAS"', '"_.OPEN"']

    def _branches_needing_a_guard(self):
        text = source(DISPATCH_LSP)
        for name in re.findall(r'\(\(= cmd-name "([^"]+)"\)', text):
            body = _dispatch_branch(text, name)
            if any(cmd in body for cmd in self.MODAL_COMMANDS):
                yield name, body

    def test_there_are_branches_to_check(self):
        """Guards against the regex quietly matching nothing and the two tests
        below passing over an empty set."""
        found = {name for name, _ in self._branches_needing_a_guard()}
        assert {"drawing-save", "drawing-save-as-dxf", "drawing-open"} <= found

    def test_every_modal_command_is_preceded_by_filedia_0(self):
        for name, body in self._branches_needing_a_guard():
            guard = body.find('(setvar "FILEDIA" 0)')
            first_modal = min(
                body.find(cmd) for cmd in self.MODAL_COMMANDS if cmd in body
            )
            assert guard != -1, f'{name} issues a modal command with no FILEDIA guard'
            assert guard < first_modal, f"{name} sets FILEDIA 0 after the dialog would open"

    def test_every_guard_is_closed(self):
        for name, body in self._branches_needing_a_guard():
            assert body.count('(setvar "FILEDIA"') >= 2, (
                f"{name} sets FILEDIA 0 and never puts it back"
            )


class TestDxfExportIsHonestAboutTheRename:
    """SAVEAS renames the active document to the .dxf; LT has no COM and
    EXPORT has no DXF format, so there is no writer that does not. The rename
    is therefore reported rather than prevented — the failure it guards against
    is a later QSAVE writing DXF over a drawing whose name nobody noticed had
    changed."""

    def body(self) -> str:
        return _dispatch_branch(source(DISPATCH_LSP), "drawing-save-as-dxf")

    def test_reports_the_document_name_it_ended_on(self):
        body = self.body()
        assert '\\"document\\"' in body and '(getvar "DWGNAME")' in body

    def test_the_rename_flag_is_measured_not_asserted(self):
        """A hardcoded "renamed": true is a claim about AutoCAD's behaviour.
        Comparing DWGNAME either side of the SAVEAS is an observation of it."""
        body = self.body()
        assert body.count('(getvar "DWGNAME")') == 2, (
            "renamed must come from DWGNAME before vs after, not a literal"
        )
        assert '(if (= dwg-before dwg-after) "false" "true")' in body

    def test_restores_the_filedia_it_found(self):
        """Hardcoding 1 turns a caller who had dialogs off into a caller who
        has them on — the neighbouring branches still do this."""
        body = self.body()
        assert '(setq filedia (getvar "FILEDIA"))' in body
        assert '(setvar "FILEDIA" filedia)' in body

    def test_rejects_an_empty_path(self):
        """SAVEAS with an empty string prompts for one, which is the hang the
        FILEDIA guard exists to prevent, arriving by another route."""
        assert '(> (strlen path) 0)' in self.body()

    def test_escapes_the_path_into_the_payload(self):
        """A Windows path lands here with backslashes; unescaped they make the
        result file unparseable JSON and the call fails after the work is done."""
        assert '(mcp-escape-string path)' in self.body()


class TestDocumentSwitchesAreVerified:
    """(command ...) has no return value, so a branch that issues one can only
    report what it asked for. For the commands that change which document is
    active, what AutoCAD actually does is nothing: OPEN, NEW and CLOSE tear
    down the document whose LISP namespace is executing, and AutoCAD refuses
    them from inside (command ...). LT has no COM to fall back on.

    drawing-open returned {"ok": true, "payload": "opened: <path>"} for a call
    that opened nothing — confirmed against LT 2027, DWGNAME unchanged and no
    new tab. mcp-cmd-drawing-create had already found the same wall for _.NEW
    and worked around it; this branch had not.

    The guard is structural rather than remembered because the next branch that
    reaches for one of these commands will have the same nothing to check.
    """

    SWITCHING_COMMANDS = ['"_.OPEN"', '"_.NEW"', '"_.CLOSE"', '"_.QUIT"']

    def _branches_that_switch(self):
        text = source(DISPATCH_LSP)
        for name in re.findall(r'\(\(= cmd-name "([^"]+)"\)', text):
            body = _dispatch_branch(text, name)
            if any(cmd in body for cmd in self.SWITCHING_COMMANDS):
                yield name, body

    def _last_switch(self, body: str) -> int:
        return max(body.rfind(cmd) for cmd in self.SWITCHING_COMMANDS)

    def test_there_are_branches_to_check(self):
        """Guards against the regex quietly matching nothing and the tests
        below passing over an empty set."""
        assert {"drawing-open"} <= {name for name, _ in self._branches_that_switch()}

    def test_the_document_is_measured_after_the_command(self):
        for name, body in self._branches_that_switch():
            assert body.rfind("(mcp-active-document-path)") > self._last_switch(body), (
                f"{name} never looks at which document it ended up in"
            )

    def test_every_success_is_gated_on_the_comparison(self):
        """A (cons T ...) that no comparison stands in front of is the original
        defect verbatim, whatever the payload says."""
        for name, body in self._branches_that_switch():
            for hit in re.finditer(r"\(cons T", body):
                assert "(mcp-same-drawing" in body[: hit.start()], (
                    f"{name} reports success without comparing documents first"
                )

    def test_a_failed_switch_is_reported_as_a_failure(self):
        """Asserted against the comparison's own else-arm rather than against
        "a (cons nil somewhere after the command", which the argument-
        validation arm every branch already has would satisfy on its own."""
        for name, body in self._branches_that_switch():
            guard = _form_at(body, body.rindex("(if (mcp-same-drawing"))
            assert "(cons nil" in guard, (
                f"{name} compares documents and then reports success either "
                f"way, so it can only report the switch it asked for"
            )


class TestOpenIsHonestAboutNotSwitching:
    """What the verified branch says when it comes back."""

    def body(self) -> str:
        return _dispatch_branch(source(DISPATCH_LSP), "drawing-open")

    def mismatch_error(self) -> str:
        """The (cons nil ...) returned when the comparison fails — not the one
        that rejects a missing path, which sits last in the source."""
        body = self.body()
        return _form_at(body, body.index("(cons nil", body.index('"_.OPEN"')))

    def test_reports_the_document_it_is_actually_in(self):
        assert '\\"document\\"' in self.body()

    def test_both_outcomes_of_the_switch_flag_exist(self):
        """"switched": false is the already-open case and true the one that
        would need OPEN to have worked. A branch carrying only one of them is
        asserting an outcome rather than reporting it."""
        body = self.body()
        assert '\\"switched\\": false' in body and '\\"switched\\": true' in body

    def test_already_open_succeeds_without_issuing_the_command(self):
        """Opening the document you are already in is the one thing this branch
        can honestly succeed at, and it must not need OPEN to do it."""
        body = self.body()
        assert body.find("(cons T") < body.find('"_.OPEN"')

    def test_the_error_names_the_document_it_is_still_in(self):
        """"OPEN failed" sends you looking at the file. Naming the document you
        are still in says the drawing you were working on is untouched."""
        assert "doc-after" in self.mismatch_error()

    def test_the_error_is_not_escaped_twice(self):
        """mcp-write-result escapes error text on the way out; escaping here as
        well doubles every backslash in the path the caller has to read."""
        assert "mcp-escape-string" not in self.mismatch_error()

    def test_restores_the_filedia_it_found(self):
        body = self.body()
        assert '(setq filedia (getvar "FILEDIA"))' in body
        assert '(setvar "FILEDIA" filedia)' in body

    def test_rejects_an_empty_path(self):
        """An empty path made the old branch prompt for one behind FILEDIA 0,
        which is the 10 s hang by another route."""
        assert "(> (strlen path) 0)" in self.body()

    def test_the_refused_command_does_not_leave_input_at_the_prompt(self):
        """When OPEN is refused its path argument is still typed, and lands at
        the Command: prompt for the next dispatch to inherit."""
        body = self.body()
        assert "(command)" in body[body.find('"_.OPEN"') :]


class TestPathComparisonCannotMatchByAccident:
    """The comparison is the whole fix; a sloppy one restores the false
    success it replaced."""

    def test_normalizes_separator_and_case(self):
        """C:\\Temp\\x.dwg and c:/temp/x.dwg are one file to Windows and two
        strings to (= ...). DWGPREFIX always answers in backslashes."""
        body = _defun_body(source(DISPATCH_LSP), "mcp-normalize-path")
        assert "(strcase" in body and '"\\\\"' in body

    def test_the_active_path_is_folder_plus_name(self):
        """DWGNAME alone compares equal to a same-named drawing in any other
        folder — exactly the coincidence this check exists to rule out."""
        body = _defun_body(source(DISPATCH_LSP), "mcp-active-document-path")
        assert '(getvar "DWGNAME")' in body and '(getvar "DWGPREFIX")' in body

    def test_a_bare_name_is_not_compared_against_a_full_path(self):
        """Otherwise a caller who passes "panel.dwg" never matches, and the
        branch reports a failure on a document that is in fact the right one."""
        body = _defun_body(source(DISPATCH_LSP), "mcp-same-drawing")
        assert "mcp-path-basename" in body


class TestSavesAreVerified:
    """The same defect as drawing-open, on the commands that write files.
    (command ...) returns nothing, so a save branch that does not look at
    anything afterwards reports the path it was handed whether SAVEAS wrote it,
    hit a read-only path, or was refused.

    drawing-save returned {"ok": true, "payload": "saved to: <path>"} for a
    SAVEAS that had failed with the original document still active — confirmed
    live, and the reason CLAUDE.md now says a bare ok is not evidence of a save.

    Unlike OPEN, these commands do work from AutoLISP, so there is something
    real to measure. SAVEAS renames the active document to the file it wrote,
    which makes the document the evidence; QSAVE writes back over the file the
    document already came from, leaving no rename, so DBMOD is the evidence
    there instead. Structural rather than remembered because the next branch
    that reaches for a save has the same nothing to check.
    """

    SAVE_COMMANDS = ['"_.SAVEAS"', '"_.QSAVE"']

    # A gate is an `if` whose condition is one of the two measurements above.
    GATE_RE = r"\(if \((?:mcp-same-drawing|= dbmod )"

    def _branches_that_save(self):
        text = source(DISPATCH_LSP)
        for name in re.findall(r'\(\(= cmd-name "([^"]+)"\)', text):
            body = _dispatch_branch(text, name)
            if any(cmd in body for cmd in self.SAVE_COMMANDS):
                yield name, body

    def _last_save(self, body: str) -> int:
        return max(body.rfind(cmd) for cmd in self.SAVE_COMMANDS)

    def _gates(self, body: str) -> list[int]:
        return [m.start() for m in re.finditer(self.GATE_RE, body)]

    def test_there_are_branches_to_check(self):
        """Guards against the regex quietly matching nothing and the tests
        below passing over an empty set."""
        found = {name for name, _ in self._branches_that_save()}
        assert {"drawing-save", "drawing-save-as-dxf"} <= found

    def test_something_is_measured_after_the_command(self):
        """A branch that stops thinking when the command returns has only the
        arguments it passed in to report."""
        for name, body in self._branches_that_save():
            measured = max(
                body.rfind("(mcp-active-document-path)"), body.rfind('(getvar "DBMOD")')
            )
            assert measured > self._last_save(body), (
                f"{name} never looks at whether the save landed"
            )

    def test_every_success_is_gated_on_a_measurement(self):
        """A (cons T ...) that no measurement stands in front of is the
        original defect verbatim, whatever the payload says."""
        for name, body in self._branches_that_save():
            for hit in re.finditer(r"\(cons T", body):
                assert re.search(self.GATE_RE, body[: hit.start()]), (
                    f"{name} reports a save it never checked"
                )

    def test_there_is_a_gate_for_every_success(self):
        """The test above only asks that *a* gate precedes each success, which
        a second arm added later would satisfy off the first arm's gate — the
        QSAVE arm sits after the SAVEAS arm and would have inherited its
        comparison for free. One gate per success closes that."""
        for name, body in self._branches_that_save():
            assert len(re.findall(r"\(cons T", body)) == len(self._gates(body)), (
                f"{name} has a success arm with no measurement of its own"
            )

    def test_a_failed_save_is_reported_as_a_failure(self):
        """Asserted against each gate's own else-arm rather than against "a
        (cons nil somewhere after the command", which the argument-validation
        arm every branch already has would satisfy on its own."""
        for name, body in self._branches_that_save():
            for start in self._gates(body):
                assert "(cons nil" in _form_at(body, start), (
                    f"{name} measures the save and then reports success either "
                    f"way, so it can only report the save it asked for"
                )

    def test_an_error_cannot_leave_the_file_dialogs_suppressed(self):
        """FILEDIA is restored on the line after SAVEAS, so an error thrown out
        of SAVEAS skips it and leaves dialogs off for the rest of the AutoCAD
        session — every file dialog Gianni opens by hand afterwards silently
        does nothing, long after this call is forgotten."""
        for name, body in self._branches_that_save():
            if '"_.SAVEAS"' not in body:
                continue
            assert "(vl-catch-all-apply 'vl-cmdf (list \"_.SAVEAS\"" in body, (
                f"{name} lets a SAVEAS error unwind past the FILEDIA restore"
            )

    def test_restores_the_filedia_it_found(self):
        """Hardcoding 1 turns a caller who had dialogs off into a caller who
        has them on — which is what drawing-save did."""
        for name, body in self._branches_that_save():
            if '"_.SAVEAS"' not in body:
                continue
            assert '(setq filedia (getvar "FILEDIA"))' in body, name
            assert '(setvar "FILEDIA" filedia)' in body, name
            assert '(setvar "FILEDIA" 1)' not in body, (
                f"{name} restores a hardcoded FILEDIA rather than the one it found"
            )


class TestSaveIsHonestAboutNotSaving:
    """What the verified branch says when it comes back."""

    def body(self) -> str:
        return _dispatch_branch(source(DISPATCH_LSP), "drawing-save")

    def qsave_arm(self) -> str:
        """The no-path half, which QSAVEs the document over its own file."""
        body = self.body()
        return _form_at(body, body.index('(if (= (getvar "DWGTITLED")'))

    def saveas_arm(self) -> str:
        """Everything before it: the half handed an explicit path."""
        body = self.body()
        return body[: body.index('(if (= (getvar "DWGTITLED")')]

    def mismatch_error(self) -> str:
        """The (cons nil ...) returned when the document comparison fails —
        not the one that rejects a missing path."""
        arm = self.saveas_arm()
        return _form_at(arm, arm.index("(cons nil", arm.index('"_.SAVEAS"')))

    def test_the_path_form_checks_the_document_it_ended_in(self):
        """SAVEAS renames the active document to the file it wrote, so the
        document is the one piece of evidence that the write happened."""
        arm = self.saveas_arm()
        assert "(mcp-active-document-path)" in arm
        assert "(if (mcp-same-drawing path doc-after)" in arm

    def test_the_no_path_form_checks_dbmod(self):
        """QSAVE leaves no rename to compare, so success is read off the flag
        AutoCAD clears when a save lands."""
        arm = self.qsave_arm()
        assert '(getvar "DBMOD")' in arm and "(if (= dbmod 0)" in arm

    def test_dbmod_is_read_before_anything_else_can_move_it(self):
        """Any setvar between the save and the measurement puts a second
        writer in front of the only evidence this arm has."""
        arm = self.qsave_arm()
        between = arm[arm.index('(command "_.QSAVE")') : arm.index('(getvar "DBMOD")')]
        assert "(setvar" not in between

    def test_refuses_to_qsave_a_drawing_that_has_never_been_saved(self):
        """QSAVE on an untitled drawing turns into SAVEAS and asks for a name:
        a modal dialog under FILEDIA 1, a command-line prompt under 0. Both sit
        there for the whole 10 s IPC window and come back as a bare timeout
        with no hint that AutoCAD is waiting on an answer."""
        arm = self.qsave_arm()
        gate = arm.index('(command "_.QSAVE")')
        assert arm.index('(getvar "DWGTITLED")') < gate
        assert "(cons nil" in arm[:gate]

    def test_both_arms_name_the_document_rather_than_only_failing(self):
        """"Save failed" sends you looking at the file. Naming the document
        says which drawing is and is not on disk."""
        assert "doc-after" in self.mismatch_error()
        assert "doc-after" in _form_at(
            self.qsave_arm(), self.qsave_arm().rindex("(cons nil")
        )

    def test_the_errors_are_not_escaped_twice(self):
        """mcp-write-result escapes error text on the way out; escaping here as
        well doubles every backslash in the path the caller has to read."""
        assert "mcp-escape-string" not in self.mismatch_error()
        assert "mcp-escape-string" not in _form_at(
            self.qsave_arm(), self.qsave_arm().rindex("(cons nil")
        )

    def test_rejects_an_empty_path(self):
        """An empty string is not a missing path: it reaches SAVEAS, which
        prompts for one behind FILEDIA 0 — the same 10 s hang by another
        route."""
        assert "(> (strlen path) 0)" in self.body()

    def test_escapes_the_path_into_the_payload(self):
        """A Windows path lands here with backslashes; unescaped they make the
        result file unparseable JSON and the call fails after the save."""
        assert "(mcp-escape-string path)" in self.saveas_arm()
        assert "(mcp-escape-string doc-after)" in self.qsave_arm()

    def test_the_refused_command_does_not_leave_input_at_the_prompt(self):
        """When SAVEAS is refused its path argument is still typed, and lands
        at the Command: prompt for the next dispatch to inherit."""
        body = self.body()
        assert "(command)" in body[body.find('"_.SAVEAS"') :]


def _form_at(text: str, start: int) -> str:
    """Source of the parenthesised form that begins at start, by paren
    matching. Lets an assertion name one arm of one `if` rather than a slice
    of a branch that happens to contain the right words."""
    stripped = strip_lisp(text)
    depth = 0
    for i in range(start, len(text)):
        ch = stripped[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"form at offset {start} is not closed")


def _dispatch_branch(text: str, cmd_name: str) -> str:
    """Source of one branch of mcp-dispatch-command's cond, by paren matching."""
    start = text.index(f'((= cmd-name "{cmd_name}")')
    stripped = strip_lisp(text)
    depth = 0
    for i in range(start, len(text)):
        ch = stripped[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"{cmd_name} branch is not closed")


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
