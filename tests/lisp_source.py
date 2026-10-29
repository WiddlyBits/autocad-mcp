"""Offline readers for AutoLISP source: blank comments/strings, slice one defun."""


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
