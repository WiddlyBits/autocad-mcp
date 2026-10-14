"""Every command routes through the CMDNAMES guard.

2026-10-14, LT 2027, LINE waiting: create_line, execute_lisp and entity(count)
all returned command_active. This pins the structure that makes that true, so
a command added outside the guarded cond fails CI instead of drawing into a
live command.
"""

from pathlib import Path

LSP = (Path(__file__).parent.parent / "lisp-code" / "mcp_dispatch.lsp").read_text(encoding="utf-8")


def _balanced_end(text: str, start: int) -> int:
    """Index just past the form opening at text[start] == '('. Skips strings and ; comments."""
    depth, i, in_str = 0, start, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == ";":
            i = text.index("\n", i)
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError("unbalanced form")


def _dispatch_body() -> str:
    start = LSP.index("(defun mcp-dispatch-command")
    return LSP[start:_balanced_end(LSP, start)]


def test_guard_wraps_the_whole_command_table():
    body = _dispatch_body()
    head = body.index('(if (not (= "" (getvar "CMDNAMES")))')
    cond = body.index("(cond", head)
    # Guard is the if-test; its then-branch is the refusal; the cond is the else-branch.
    refusal = body.index("command_active", head)
    assert head < refusal < cond
    # Nothing but the cond follows the refusal inside the defun.
    assert body[_balanced_end(body, cond):].replace(")", "").strip() == ""


def test_every_command_branch_is_inside_the_cond():
    body = _dispatch_body()
    cond = body.index("(cond")
    inside = body[cond:_balanced_end(body, cond)]
    assert inside.count('((= cmd-name "') >= 40
    assert body.count('(= cmd-name "') == inside.count('(= cmd-name "')


def test_entrypoint_has_no_other_route_to_a_command():
    start = LSP.index("(defun c:mcp-dispatch")
    entry = LSP[start:_balanced_end(LSP, start)]
    assert "'mcp-dispatch-command" in entry
    assert "(command " not in entry
