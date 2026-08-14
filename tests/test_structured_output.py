"""Guards against re-introducing the structuredContent screenshot duplicate.

FastMCP wraps any annotated return type in a generated output schema, then
serialises the entire return value into structuredContent next to the real
content blocks. Without an explicit opt-out every screenshot goes out twice:
once as an image block, and once as raw base64 text inside a `{"result": [...]}`
blob.

The image block bills as ceil(w/28) * ceil(h/28) visual tokens. The base64
duplicate is effectively random text, so it tokenises at roughly one token per
character — for a 1280px capture that is an order of magnitude more than the
image itself, and it lands in ordinary context where it is re-read on every
subsequent request.

These tests pin the opt-out in place. If someone adds a ninth tool and forgets
`structured_output=False`, test_no_tool_declares_an_output_schema fails.
"""

import pytest

from autocad_mcp.server import mcp

EXPECTED_TOOL_COUNT = 8


async def test_all_tools_are_registered():
    """Sanity check: the count below is what the schema assertion covers."""
    tools = await mcp.list_tools()
    assert len(tools) == EXPECTED_TOOL_COUNT


async def test_no_tool_declares_an_output_schema():
    """An output schema is what triggers the duplicate payload."""
    tools = await mcp.list_tools()
    offenders = [t.name for t in tools if t.outputSchema is not None]
    assert offenders == [], (
        f"{offenders} declare an outputSchema, so FastMCP will duplicate every "
        f"result into structuredContent. Add structured_output=False to their "
        f"@mcp.tool() decorator."
    )


async def test_result_carries_no_structured_content():
    """End-to-end: a real call must not echo its payload into structuredContent."""
    result = await mcp.call_tool("system", {"operation": "status"})

    # FastMCP returns (content_blocks, structured_content) when a schema exists.
    structured = result[1] if isinstance(result, tuple) else None
    assert not structured, (
        "call_tool returned structuredContent; the payload is being sent twice."
    )


@pytest.mark.parametrize(
    "annotation, expect_schema",
    [
        (str, True),
        (str | list, True),
        (list, False),
        (dict, False),
    ],
)
async def test_any_annotated_return_type_generates_a_schema(annotation, expect_schema):
    """Documents the real mechanism, so the fix is not mistaken for cargo cult.

    It is tempting to assume the `str | list` union is what triggers this and that
    narrowing the annotation would fix it. It is not: on mcp 1.26.0 a bare `-> str`
    is wrapped too. What escapes is an *unparameterised* container — bare `list` or
    bare `dict` — or an entirely absent annotation.

    So dropping the union in favour of bare `list` would technically suppress the
    duplicate, but it would also discard the only type information the annotation
    carries. structured_output=False is the honest opt-out: it says "no structured
    output" instead of hiding behind a vague type.

    If a future SDK changes these rules, this test fails and someone revisits —
    that is the point of pinning it.
    """
    from mcp.server.fastmcp import FastMCP

    probe = FastMCP("probe")

    async def sample(x: int):
        return "ok"

    sample.__annotations__["return"] = annotation
    probe.tool()(sample)

    (tool,) = await probe.list_tools()
    assert (tool.outputSchema is not None) is expect_schema


async def test_structured_output_false_suppresses_the_duplicate():
    """The opt-out must actually remove the echoed payload, not just the schema."""
    from mcp.server.fastmcp import FastMCP

    probe = FastMCP("probe")
    sentinel = "BASE64BLOB"

    @probe.tool(structured_output=False)
    async def patched(x: int) -> str | list:
        return [sentinel]

    result = await probe.call_tool("patched", {"x": 1})
    structured = result[1] if isinstance(result, tuple) else None

    assert not structured
    assert sentinel not in repr(structured)
