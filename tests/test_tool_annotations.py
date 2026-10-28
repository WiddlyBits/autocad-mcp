"""Pins the readOnlyHint/destructiveHint annotation on every tool.

Clients use these hints to decide what to auto-approve. The spec defaults an
unannotated destructiveHint to true, so a missing value is not neutral: it marks
an additive tool as destructive. A wrong readOnlyHint is worse: `system` used to
claim read-only while `execute_lisp` can run anything.

If someone adds a tool, test_every_tool_is_classified fails until it is added
to EXPECTED below.
"""

import pytest

from autocad_mcp.server import mcp

# name -> (readOnlyHint, destructiveHint); None = not applicable to read-only tools
EXPECTED = {
    "drawing": (False, True),  # save, purge
    "entity": (False, True),  # erase
    "layer": (False, False),
    "block": (False, False),
    "annotation": (False, False),
    "pid": (False, False),
    "view": (True, None),
    "system": (False, True),  # execute_lisp
}


async def _annotations():
    return {t.name: t.annotations for t in await mcp.list_tools()}


async def test_every_tool_is_classified():
    assert sorted(await _annotations()) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_annotation_hints(name):
    read_only, destructive = EXPECTED[name]
    ann = (await _annotations())[name]
    assert ann.readOnlyHint is read_only
    if not read_only:
        assert ann.destructiveHint is destructive, (
            f"{name} must set destructiveHint explicitly; the spec default is true"
        )
