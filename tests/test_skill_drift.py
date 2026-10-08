"""Skills must only name things that exist on this branch.

On 2026-08-22 the workflow skill documented a preflight block and an
mcp_select.lsp that existed only on an unmerged branch. Nothing caught it,
because the check was memory. These tests are the check: every `mcp:`/`c:`
LISP function and every `tool(operation)` a skill names must be defined in
lisp-code/ or dispatched in server.py.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
LISP_DIR = ROOT / "lisp-code"
SERVER = ROOT / "src" / "autocad_mcp" / "server.py"

TOOLS = ("system", "drawing", "entity", "layer", "view", "block", "annotation", "pid")

# Named in a skill on purpose, as the thing that does not exist.
KNOWN_ABSENT = {("drawing", "save_as")}

LISP_REF = re.compile(r"(?<![\w/\\])((?:mcp|c):[A-Za-z][\w-]*)")
LISP_DEF = re.compile(r"\(defun\s+((?:mcp|c):[\w-]+)", re.IGNORECASE)
TOOL_REF = re.compile(
    r"\b(" + "|".join(TOOLS) + r")\((?:operation=)?\"?([a-z_]+)\"?\s*[,)]"
)


def _skill_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(SKILLS.rglob("*.md")))


def _defined_lisp() -> set[str]:
    names: set[str] = set()
    for lsp in LISP_DIR.glob("*.lsp"):
        names |= {m.lower() for m in LISP_DEF.findall(lsp.read_text(encoding="utf-8"))}
    return names


def _dispatched_ops() -> dict[str, set[str]]:
    """operation strings each tool function compares against, from the live AST.

    Commented-out tools (the pruned pid) are not in the AST, so they have no ops.
    """
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    ops: dict[str, set[str]] = {}
    for fn in tree.body:
        if not isinstance(fn, ast.AsyncFunctionDef) or fn.name not in TOOLS:
            continue
        found = ops.setdefault(fn.name, set())
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)):
                continue
            if node.left.id != "operation":
                continue
            for comp in node.comparators:
                elts = comp.elts if isinstance(comp, (ast.Tuple, ast.List, ast.Set)) else [comp]
                found |= {e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return ops


def test_extraction_finds_references():
    """Guard the regexes: an extractor that matches nothing passes everything."""
    text = _skill_text()
    assert len(set(LISP_REF.findall(text))) >= 15
    assert len(set(TOOL_REF.findall(text))) >= 10
    assert len(_defined_lisp()) >= 20


def test_every_lisp_name_in_skills_is_defined():
    defined = _defined_lisp()
    missing = sorted({n for n in LISP_REF.findall(_skill_text()) if n.lower() not in defined})
    assert not missing, f"skills name LISP functions not defined in lisp-code/: {missing}"


def test_every_tool_operation_in_skills_is_dispatched():
    ops = _dispatched_ops()
    missing = sorted(
        {(tool, op) for tool, op in TOOL_REF.findall(_skill_text())
         if op not in ops.get(tool, set()) and (tool, op) not in KNOWN_ABSENT}
    )
    assert not missing, f"skills name tool operations server.py does not dispatch: {missing}"


def test_known_absent_operations_are_still_absent():
    """If one of these gets implemented, the skill text calling it an error is now wrong."""
    ops = _dispatched_ops()
    present = sorted((t, o) for t, o in KNOWN_ABSENT if o in ops.get(t, set()))
    assert not present, f"documented as nonexistent but now dispatched: {present}"


@pytest.mark.parametrize("skill_md", sorted(SKILLS.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_skill_frontmatter_is_closed_and_complete(skill_md):
    """claude.ai rejects the upload without a closing ---; bb06517 dropped it from title-block-text."""
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---", "SKILL.md must open with ---"
    assert "---" in lines[1:], "frontmatter has no closing ---"
    front = lines[1:lines.index("---", 1)]
    keys = {ln.split(":", 1)[0] for ln in front if ln and not ln[0].isspace() and ":" in ln}
    assert {"name", "description"} <= keys, f"frontmatter keys: {sorted(keys)}"
    assert f"name: {skill_md.parent.name}" in front, "name must match the skill folder"
