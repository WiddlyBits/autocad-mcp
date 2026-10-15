"""Verbs that run a command must measure the drawing before they report success.

(command ...) returns nothing whether the command worked or was refused, so a
bare (cons T "moved") is a claim with no evidence behind it.
"""

import pytest

from tests.test_dispatch_claim import DISPATCH_LSP
from tests.test_probes_lisp import _defun_body, strip_lisp

TEXT = DISPATCH_LSP.read_text(encoding="utf-8")

#: verb defun -> the success string that must sit behind a comparison.
ENTITY_VERBS = {
    "mcp-cmd-entity-move": "moved",
    "mcp-cmd-entity-rotate": "rotated",
    "mcp-cmd-entity-scale": "scaled",
    "mcp-cmd-entity-fillet": "filleted",
    "mcp-cmd-entity-chamfer": "chamfered",
}


def test_file_parens_balance():
    stripped = strip_lisp(TEXT)
    assert stripped.count("(") == stripped.count(")")


@pytest.mark.parametrize("verb,word", ENTITY_VERBS.items())
def test_entity_verb_reads_the_entity_before_and_after(verb, word):
    body = _defun_body(TEXT, verb)
    assert "(entget ent" in body, f"{verb} never reads the entity before the command"
    assert "(mcp-entity-changed" in body, f"{verb} never compares it afterwards"
    # The success string must sit inside an (if ...) whose other arm is an error.
    assert body.index("mcp-entity-changed") < body.index(f'\\"{word}\\"')
    assert "(cons nil" in body[body.index(f'\\"{word}\\"'):]


def test_purge_reports_table_counts():
    seg = TEXT[TEXT.index('(= cmd-name "drawing-purge")'):]
    seg = seg[: seg.index('((= cmd-name "drawing-open")')]
    assert seg.index("(mcp-purge-counts)") < seg.index("_.-PURGE") < seg.rindex("(mcp-purge-counts)")
    assert '\\"removed\\"' in seg


def test_undo_compares_the_drawing_before_and_after():
    seg = TEXT[TEXT.index('(= cmd-name "undo")'):]
    seg = seg[: seg.index('(= cmd-name "redo")')]
    assert seg.index("(mcp-world-snapshot)") < seg.index("_.UNDO") < seg.rindex("(mcp-world-snapshot)")
    assert "(equal undo-before undo-after)" in seg
    assert "(cons nil" in seg


def test_new_locals_are_declared_not_global():
    header = TEXT[TEXT.index("(defun mcp-dispatch-command"):].split("\n", 1)[0]
    for name in ("purge-before", "purge-after", "undo-before", "undo-after"):
        assert name in header
