"""entity(get) must return the same shape from either backend.

There are two implementations of entity(get) — `EzdxfBackend._entity_geometry`
and the `mcp-cmd-entity-get` cond in lisp-code/mcp_dispatch.lsp — and until this
file existed nothing compared them. 439 green tests missed three divergences
that a live run found in one call each: TEXT/ATTDEF/MTEXT dropped `rotation`
entirely, POLYLINE had no branch at all (734 of them in Draft 3, every one
reporting identity and no geometry), and every angle the LISP emitted was in
radians while ezdxf's were in degrees.

`execute_lisp` needs a running AutoCAD, so the LISP half cannot be *executed*
here. What can be checked offline is its source: which keys each branch writes,
and whether the angles among them are converted. Both sides are derived
mechanically — the ezdxf key sets by building a real entity of each type and
calling the real method, the LISP key sets by parsing the real cond — so this
test cannot drift into agreeing with a copy of itself.

What it does NOT cover, stated so it is not assumed: values. Two backends can
emit `insert` and disagree about where it is. Only a live run answers that.
"""

import re
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
from tests.test_probes_lisp import _defun_body, strip_lisp

DISPATCH_LSP = Path(__file__).parent.parent / "lisp-code" / "mcp_dispatch.lsp"

#: Keys whose value is an angle. ezdxf reports degrees; AutoLISP's entget
#: reports radians whatever the DXF file stores, so every one of these has to
#: be converted on the LISP side or the two backends answer in different units.
ANGLE_KEYS = {"rotation", "start_angle", "end_angle"}

#: One entity per type entity(get) claims to understand. Real ezdxf objects,
#: so the expected key set comes from the shipping code rather than from a list
#: someone maintained by hand.
BUILDERS = {
    "LINE": lambda msp: msp.add_line((0, 0), (1, 1)),
    "CIRCLE": lambda msp: msp.add_circle((0, 0), radius=1),
    "ARC": lambda msp: msp.add_arc((0, 0), radius=1, start_angle=0, end_angle=90),
    "ELLIPSE": lambda msp: msp.add_ellipse((0, 0), major_axis=(1, 0), ratio=0.5),
    "LWPOLYLINE": lambda msp: msp.add_lwpolyline([(0, 0), (1, 1)]),
    "POLYLINE": lambda msp: msp.add_polyline2d([(0, 0), (1, 1)]),
    "TEXT": lambda msp: msp.add_text("x"),
    "ATTDEF": lambda msp: msp.add_attdef("TAG"),
    "MTEXT": lambda msp: msp.add_mtext("x"),
    "INSERT": lambda msp: msp.add_blockref("B", (0, 0)),
    "POINT": lambda msp: msp.add_point((0, 0)),
}


@pytest.fixture(scope="module")
def ezdxf_keys() -> dict[str, set[str]]:
    """Type -> the keys _entity_geometry actually returns for it."""
    doc = ezdxf.new()
    doc.blocks.new(name="B")
    msp = doc.modelspace()
    return {
        kind: set(EzdxfBackend._entity_geometry(build(msp)))
        for kind, build in BUILDERS.items()
    }


def _match_paren(text: str, start: int) -> int:
    """Index just past the form opening at `start`. Comments and strings masked."""
    masked = strip_lisp(text)
    depth = 0
    for i in range(start, len(text)):
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    raise AssertionError("unclosed form")


@pytest.fixture(scope="module")
def lisp_branches() -> dict[str, str]:
    """Type -> the source of the cond branch that handles it.

    Parsed out of the shipping dispatcher rather than transcribed, for the same
    reason the ezdxf side is built rather than listed.
    """
    body = _defun_body(DISPATCH_LSP.read_text(encoding="utf-8"), "mcp-cmd-entity-get")
    branches: dict[str, str] = {}
    for m in re.finditer(r"\((?:=|member)\s+etype\s", body):
        test_end = _match_paren(body, m.start())
        branch_start = body.rindex("(", 0, m.start())
        branch = body[branch_start : _match_paren(body, branch_start)]
        for kind in re.findall(r'"([A-Z]+)"', body[m.start() : test_end]):
            branches[kind] = branch
    return branches


def _keys(branch: str) -> set[str]:
    """The JSON keys a branch writes: the `,\\"name\\":` literals in its strcat."""
    return set(re.findall(r',\\"(\w+)\\":', branch))


class TestEveryTypeIsHandledOnBothSides:
    def test_no_type_is_missing_from_the_lisp(self, ezdxf_keys, lisp_branches):
        """The POLYLINE case: ezdxf measured it, the LISP had no branch, and
        the only symptom was geometry-free output on 734 entities."""
        missing = sorted(set(ezdxf_keys) - set(lisp_branches))
        assert missing == [], f"mcp-cmd-entity-get has no branch for {missing}"

    def test_the_lisp_claims_no_type_ezdxf_cannot_measure(self, ezdxf_keys, lisp_branches):
        extra = sorted(set(lisp_branches) - set(ezdxf_keys))
        assert extra == [], f"LISP-only entity types, unreachable on ezdxf: {extra}"


@pytest.mark.parametrize("kind", sorted(BUILDERS))
class TestKeySetsAgree:
    def test_same_keys(self, kind, ezdxf_keys, lisp_branches):
        assert kind in lisp_branches, f"no LISP branch for {kind}"
        assert _keys(lisp_branches[kind]) == ezdxf_keys[kind]

    def test_angles_are_converted_to_degrees(self, kind, ezdxf_keys, lisp_branches):
        """entget returns radians, ezdxf returns degrees, and nothing downstream
        can tell which it received. probes.py reads these fields as degrees."""
        if kind not in lisp_branches:
            pytest.skip("covered by test_same_keys")
        branch = lisp_branches[kind]
        for key in ANGLE_KEYS & ezdxf_keys[kind]:
            emitted = re.search(r',\\"%s\\":"\s*(.+?)\)\)\)' % key, branch, re.S)
            assert emitted and "mcp-rad2deg" in emitted.group(1), (
                f"{kind}.{key} is emitted straight from entget, so it is in "
                f"radians while ezdxf reports degrees"
            )


class TestFieldsThatNeedMoreThanAssoc:
    """Three group codes where a single `assoc` returns something plausible and
    wrong. A key-set comparison passes on all of them, which is why they are
    pinned by name."""

    def test_mtext_reads_the_group_3_chunks(self, lisp_branches):
        """Group 1 holds only the tail of a long MTEXT. Reading it alone
        truncates every long note to its last fragment — and looks like text."""
        assert "mcp-mtext-text" in lisp_branches["MTEXT"]

    def test_polyline_traverses_vertices_rather_than_collecting_group_10(
        self, lisp_branches
    ):
        """A heavy POLYLINE has no repeated group 10 on its header; the
        vertices are sub-entities. mcp-collect-points returns [] for it, which
        is an empty answer rather than an error."""
        branch = lisp_branches["POLYLINE"]
        assert "mcp-polyline-vertices" in branch
        assert "mcp-collect-points" not in branch

    def test_polyline_traversal_stops_at_the_end_of_the_polyline(self):
        """Without the VERTEX guard the walk runs off into whatever entity
        follows SEQEND and reports its points as the polyline's."""
        body = _defun_body(
            DISPATCH_LSP.read_text(encoding="utf-8"), "mcp-polyline-vertices"
        )
        assert '"VERTEX"' in body


class TestLispDoesNotDivergeFromTheProbes:
    """mcp_dispatch.lsp and mcp_probes.lsp both read text and angles out of
    entget, and probes.py consumes entity(get)'s output. All three have to
    agree about units, or a bbox lands 57 degrees off."""

    def test_probes_also_treat_group_50_as_radians(self):
        probes = (DISPATCH_LSP.parent / "mcp_probes.lsp").read_text(encoding="utf-8")
        assert "(/ (* 180.0 (mcp:group data 50 0.0)) pi)" in probes
