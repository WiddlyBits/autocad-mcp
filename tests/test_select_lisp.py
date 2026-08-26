"""Structural checks on lisp-code/mcp_select.lsp.

Same constraint as tests/test_probes_lisp.py: execute_lisp exists only on the
file_ipc backend, so nothing here can be run without AutoCAD in front of it.
What *is* checkable offline is the class of mistake that costs a 10 s IPC round
trip to discover — an unbalanced paren, a misspelled call, a verb that mutates
without guarding first.

The last of those is the one this file exists for. On 2026-08-22 a bare
(ssget "_X") swept paper space into a model-space move, and a PASTECLIP
followed CTAB into the wrong tab. Both were caught afterwards, by eye. A verb
that cannot run without naming its document and space cannot make either
mistake in the first place, and that property is worth asserting.
"""

import re
from pathlib import Path

import pytest

from tests.test_probes_lisp import _defun_body as defun_body
from tests.test_probes_lisp import strip_lisp

LISP_DIR = Path(__file__).parent.parent / "lisp-code"
SELECT_LSP = LISP_DIR / "mcp_select.lsp"
PROBES_LSP = LISP_DIR / "mcp_probes.lsp"
DISPATCH_LSP = LISP_DIR / "mcp_dispatch.lsp"

#: Everything the skill and the server are allowed to name.
PUBLIC = [
    "c:HS",
    "mcp:sel",
    "mcp:sel-dump",
    "mcp:sel-show",
    "mcp:by-handles",
    "mcp:guard",
    "mcp:reactor-init",
    "mcp:whoami",
    "mcp:verify-write",
    "mcp:line-uniform",
    "mcp:align-axis",
    "mcp:sel-move",
    "mcp:text-sub",
]

#: The verbs that change the drawing. Every one of them has to guard, undo-wrap
#: and report — the three properties that turn a mutation into a reversible,
#: attributable one.
VERBS = ["mcp:line-uniform", "mcp:align-axis", "mcp:sel-move", "mcp:text-sub"]


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestParseable:
    def test_parens_balance(self):
        """An unbalanced file does not produce an error message. It produces a
        dispatcher that silently stops responding."""
        code = strip_lisp(source(SELECT_LSP))
        depth = 0
        line = 1
        for ch in code:
            if ch == "\n":
                line += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                assert depth >= 0, f"unmatched ')' at line {line}"
        assert depth == 0, f"{depth} unclosed '(' at end of file"

    def test_masking_preserves_offsets(self):
        text = source(SELECT_LSP)
        assert len(strip_lisp(text)) == len(text)

    def test_no_tabs_in_source(self):
        assert "\t" not in source(SELECT_LSP)

    def test_no_call_to_an_undefined_function(self):
        """mcp_select.lsp calls into mcp_probes.lsp on purpose — mcp:fmt,
        mcp:bb-union, mcp:ent-bbox-data — which is why the server loads probes
        first. A name that neither file defines is a 10 s round trip away from
        being discovered as "no function definition"."""
        code = strip_lisp(source(SELECT_LSP))
        defined = set(re.findall(r"\(defun ([\w:-]+) ", code))
        for other in (PROBES_LSP, DISPATCH_LSP):
            defined |= set(re.findall(r"\(defun ([\w:-]+) ", strip_lisp(source(other))))
        called = set(re.findall(r"\((mcp[:-][\w:-]+)", code))
        assert called - defined == set(), (
            f"calls functions nothing defines: {sorted(called - defined)}"
        )

    def test_no_function_is_defined_twice(self):
        names = re.findall(r"\(defun ([\w:-]+) ", strip_lisp(source(SELECT_LSP)))
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert dupes == [], f"defined more than once: {dupes}"


class TestPublicSurface:
    @pytest.mark.parametrize("name", PUBLIC)
    def test_defined(self, name):
        assert f"(defun {name} " in source(SELECT_LSP)

    def test_load_banner_names_every_public_function(self):
        """A banner that lists something the file does not define is worse than
        no banner — it is what you read when something is missing."""
        banner = [l for l in source(SELECT_LSP).splitlines() if "mcp_select.lsp loaded" in l]
        assert len(banner) == 1
        for name in PUBLIC:
            assert name in banner[0], f"{name} missing from the load banner"


class TestNoActiveX:
    """LT has no COM, vla- object creation hangs the dispatcher, and
    vla-getboundingbox's conventional 'mn / 'mx output symbols collide with any
    local of the same name — a measured failure on 2026-08-22, reported as
    "bad argument type for compare: 1 #<safearray...>". Group codes only."""

    def test_no_vla_calls(self):
        code = strip_lisp(source(SELECT_LSP))
        offenders = [tok for tok in ("vla-", "vlax-", "vl-load-com") if tok in code]
        assert offenders == [], f"ActiveX in mcp_select.lsp: {offenders}"

    def test_reactor_registration_is_guarded(self):
        """vlr- is not vla-, but LT's VLISP support is partial and calling a
        reactor function that does not exist is an error, not a nil."""
        body = defun_body(source(SELECT_LSP), "mcp:reactor-init")
        assert "atoms-family" in body
        assert body.index("atoms-family") < body.index("(vlr-command-reactor")


class TestEveryVerbGuards:
    """The properties that make a mutation safe to run unattended."""

    @pytest.mark.parametrize("verb", VERBS)
    def test_names_its_document_and_space(self, verb):
        """AutoLISP has no optional arguments, so a guard that is written into
        the signature cannot be forgotten at a call site."""
        body = defun_body(source(SELECT_LSP), verb)
        params = body[body.index("(") : body.index(")") + 1]
        assert " dwg " in params and " tab " in params, (
            f"{verb} does not take dwg and tab: {params}"
        )

    @pytest.mark.parametrize("verb", VERBS)
    def test_guards_before_anything_else(self, verb):
        """The guard has to come before the first mutation, not alongside it."""
        body = defun_body(source(SELECT_LSP), verb)
        assert "(mcp:guard dwg tab)" in body, f"{verb} never calls mcp:guard"
        assert "(mcp:wrong-doc dwg tab)" in body, f"{verb} has no refusal branch"
        # Raw body, not strip_lisp: the mask blanks string literals, so
        # '(command "_.MOVE"' would never be found and the check would
        # pass by never running.
        guard_at = body.index("(mcp:guard dwg tab)")
        mutators = [m for m in ("(entmod ", '(command "_.MOVE"') if m in body]
        assert mutators, f"{verb} mutates nothing"
        for mutator in mutators:
            assert guard_at < body.index(mutator), f"{verb} mutates before guarding"

    @pytest.mark.parametrize("verb", VERBS)
    def test_wraps_itself_in_one_undo_group(self, verb):
        """So that backing it out is one UNDO. On 2026-08-22 a wrong-space move
        was reversed by moving back with a slightly different offset, which
        leaves a drawing that is not the one you started with."""
        body = strip_lisp(defun_body(source(SELECT_LSP), verb))
        assert body.count("(mcp:undo-begin") == 1, f"{verb} does not open one undo group"
        assert body.count("(mcp:undo-end)") == 1, f"{verb} does not close its undo group"

    @pytest.mark.parametrize("verb", VERBS)
    def test_reports_what_it_changed(self, verb):
        """A verb that returns ok:true and no count cannot be checked without
        another round trip, which is the cost this whole file is fighting."""
        body = defun_body(source(SELECT_LSP), verb)
        assert '\\"changed\\":' in body or '\\"moved\\":' in body, (
            f"{verb} reports no count"
        )


class TestSelectionContract:
    def test_sel_never_reads_the_implied_selection(self):
        """(ssget "_I") returned NONE on 4 of 4 attempts on 2026-08-22, with
        PICKFIRST=1. Typing the dispatch trigger is what discards it. A rung
        that is always nil is not a fallback, it is a wasted round trip."""
        code = strip_lisp(source(SELECT_LSP))
        assert '(ssget "_I")' not in code

    def test_sel_prefers_deliberate_handoff_over_leftovers(self):
        """(ssget "_P") is whatever command ran last, which is not necessarily
        what the user meant. It is the last rung, never the first."""
        body = defun_body(source(SELECT_LSP), "mcp:sel")
        cond = body[body.index("(cond") :]
        assert cond.index("*mcp-pickfirst*") < cond.index("*mcp-sel*")
        assert cond.index("*mcp-sel*") < cond.index('(ssget "_P")')

    def test_the_stash_holds_handles_not_selection_sets(self):
        """A handle survives an intervening command, the dispatch boundary and
        the ~128 open-sset ceiling. A selection set survives none of those."""
        body = defun_body(source(SELECT_LSP), "c:HS")
        assert "(mcp:handles-of" in body, "c:HS stashes something other than handles"

    def test_sel_show_zooms_as_well_as_highlights(self):
        """Highlighting alone was tried on two entities outside the current
        view; the answer was "I don't see it selected". An echo nobody can see
        confirms nothing."""
        body = defun_body(source(SELECT_LSP), "mcp:sel-show")
        assert '(command "_.ZOOM"' in body
        assert "(sssetfirst" in body

    def test_sel_show_highlights_after_zooming(self):
        """ZOOM is a command, and a command clears the implied selection — the
        same mechanism this file works around. Highlighting first highlights
        nothing."""
        body = defun_body(source(SELECT_LSP), "mcp:sel-show")
        assert body.index('(command "_.ZOOM"') < body.index("(sssetfirst")


class TestDumpAnswersInOneCall:
    """Four round trips on 2026-08-22 — count, types, coordinates, lengths —
    at roughly 108 K cache-read each."""

    @pytest.mark.parametrize(
        "field", ["count", "source", "dwg", "ctab", "bbox", "entities", "truncated"]
    )
    def test_dump_reports(self, field):
        assert f'\\"{field}\\":' in defun_body(source(SELECT_LSP), "mcp:sel-dump")

    @pytest.mark.parametrize("kind", ["LINE", "CIRCLE", "ARC", "TEXT", "MTEXT", "INSERT"])
    def test_geometry_is_chosen_by_entity_type(self, kind):
        """Group code 10 is a LINE's start, a CIRCLE's centre and a TEXT's
        insertion. Asking a TEXT for Width — an MTEXT-only property — was a
        real failed round trip that day; dispatching on type is what stops it
        being possible."""
        assert f'"{kind}")' in defun_body(source(SELECT_LSP), "mcp:ent-json")

    def test_dump_reports_each_entity_space(self):
        """Both wrong-space failures that day were invisible until afterwards.
        Group 410 in the dump makes them visible before the edit."""
        assert '\\"space\\":' in defun_body(source(SELECT_LSP), "mcp:ent-json")

    def test_dump_prints_literal_text(self):
        """A wcmatch pattern written against remembered text matched 0 of 152
        that day. Patterns get written against strings that have been read."""
        body = defun_body(source(SELECT_LSP), "mcp:ent-json")
        assert '\\"text\\":' in body

    def test_dump_is_bounded(self):
        """A handed-over selection is tens of entities. (ssget "_P") after a
        select-all is not, and the 10 s IPC timeout does not care which one was
        meant."""
        body = defun_body(source(SELECT_LSP), "mcp:sel-dump")
        assert "*mcp-max-selection*" in body
        assert '\\"truncated\\":' in body


class TestTextAlignmentPoint:
    def test_align_moves_the_alignment_point_too(self):
        """For any justification other than left, group 11 — not group 10 — is
        what positions the glyphs. Moving only 10 changes the data without
        changing the drawing."""
        body = defun_body(source(SELECT_LSP), "mcp:align-axis")
        assert "(assoc 11 data)" in body
        assert '(= "TEXT"' in body

    def test_chunked_mtext_is_declined_not_truncated(self):
        """Writing group 1 alone on an MTEXT split across group 3 chunks
        truncates it. A verb that quietly destroys text is worse than one that
        declines."""
        body = defun_body(source(SELECT_LSP), "mcp:text-sub")
        assert "(assoc 3 data)" in body


class TestWhoami:
    """The identity probe. Its whole reason to exist is that the getvar list was
    retyped from memory five times in one session on 2026-08-25, so what matters
    is that it answers the full question in one call rather than most of it."""

    @pytest.mark.parametrize(
        "var", ["DWGNAME", "DWGPREFIX", "CTAB", "TILEMODE", "DBMOD", "FILEDIA", "DWGTITLED"]
    )
    def test_reports(self, var):
        body = defun_body(source(SELECT_LSP), "mcp:whoami")
        assert f'(getvar "{var}")' in body, f"whoami does not report {var}"

    def test_reports_the_folder_not_just_the_name(self):
        """Two drawings with the same name in different folders is the ordinary
        case. DWGNAME alone cannot tell them apart, and this probe is what gets
        consulted before something destructive."""
        body = defun_body(source(SELECT_LSP), "mcp:whoami")
        assert '\\"prefix\\":' in body

    def test_dirty_state_is_a_boolean_not_just_the_raw_flag(self):
        """DBMOD is a bit field. A caller asking "are there unsaved changes"
        should not have to know that."""
        body = defun_body(source(SELECT_LSP), "mcp:whoami")
        assert '\\"dirty\\":' in body


class TestVerifyWrite:
    """The save/export check, whose contract is that evidence comes from the
    file and not from the return value."""

    def test_verdict_does_not_come_from_the_caught_error(self):
        """Measured 2026-08-22: DXFOUT aimed at a directory that does not exist
        returned "no-error" and wrote nothing. An error that is never raised
        cannot be caught, so ok has to be derived from the file changing."""
        body = defun_body(source(SELECT_LSP), "mcp:verify-write")
        assert '"{\\"ok\\":" (if changed' in body, "ok is not derived from `changed`"

    def test_compares_the_file_before_and_after(self):
        body = defun_body(source(SELECT_LSP), "mcp:verify-write")
        assert body.count("(vl-file-systime path)") == 2, "needs a before AND an after"

    def test_size_backs_up_the_timestamp(self):
        """A rewrite fast enough to land inside the same second is invisible to
        mtime alone."""
        body = defun_body(source(SELECT_LSP), "mcp:verify-write")
        assert body.count("(vl-file-size path)") == 2
        assert "(/= before-size after-size)" in body

    def test_reports_the_error_without_trusting_it(self):
        body = defun_body(source(SELECT_LSP), "mcp:verify-write")
        assert "vl-catch-all-error-p" in body
        assert '\\"error\\":' in body

    def test_filedia_is_restored_to_what_it_was(self):
        """Not to 1 — the caller may have set it deliberately. Forcing it to 0
        and leaving it there is how a later interactive command loses its
        dialog."""
        body = defun_body(source(SELECT_LSP), "mcp:verify-write")
        assert '(setq fd (getvar "FILEDIA"))' in body
        assert '(setvar "FILEDIA" 0)' in body
        assert '(setvar "FILEDIA" fd)' in body

    def test_flushes_a_half_finished_command(self):
        """Without the bare (vl-cmdf), a command still waiting on input leaves
        CMDACTIVE set and the next call inherits it."""
        body = defun_body(source(SELECT_LSP), "mcp:verify-write")
        assert "(vl-cmdf)" in body
        assert '\\"cmdactive\\":' in body

    def test_a_missing_file_afterwards_is_never_a_pass(self):
        body = defun_body(source(SELECT_LSP), "mcp:verify-write")
        assert "((null after) nil)" in body
