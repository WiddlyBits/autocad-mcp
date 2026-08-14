"""The golden .snap round-trip: structure committed as text, and diffed.

Modelled on Playwright's ARIA snapshots. The drawing's structure — extents,
per-layer footprints, every string, a coarse occupancy grid — is committed as
text, and an unintended change to any of it fails here with a readable diff
instead of being noticed three edits later in a screenshot.

The fixtures are also the contract between the two layers. mcp_probes.lsp is a
transcription of autocad_mcp/probes.py, and the way to prove the transcription
is faithful is to run mcp:snapshot on the same drawing in AutoCAD and diff its
output against the file this test guards.
"""

import os
from pathlib import Path

import ezdxf
import pytest

from autocad_mcp.probe_dxf import snapshot_doc, snapshot_file
from tests.generate_golden import (
    GOLDEN_DIR,
    REBASELINE_ENV,
    SNAPSHOT_SOURCES,
    RebaselineRefused,
    rebaseline_snapshots,
    snapshot_path,
)


def _diff(expected: str, actual: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="committed .snap",
            tofile="this run",
        )
    )


@pytest.mark.parametrize("stem", SNAPSHOT_SOURCES)
class TestGoldenSnapshots:
    def test_fixture_is_committed(self, stem):
        assert snapshot_path(stem).exists(), (
            f"{stem}.snap is missing. A golden file that is generated on demand "
            "is not a golden file."
        )

    def test_snapshot_matches_the_fixture(self, stem):
        expected = snapshot_path(stem).read_text(encoding="utf-8")
        actual = snapshot_file(GOLDEN_DIR / f"{stem}.dxf")
        assert actual == expected, (
            f"\n{stem}.snap no longer describes {stem}.dxf.\n"
            f"{_diff(expected, actual)}\n"
            "If this change was not intended, the fixture is right and the "
            "code is wrong. Fix the code."
        )

    def test_snapshot_is_deterministic(self, stem):
        """Same input, same bytes. Otherwise the fixture is noise."""
        path = GOLDEN_DIR / f"{stem}.dxf"
        assert snapshot_file(path) == snapshot_file(path)

    def test_ends_with_a_newline(self, stem):
        """So the last line diffs like every other line."""
        assert snapshot_path(stem).read_text(encoding="utf-8").endswith("\n")

    def test_carries_a_format_version(self, stem):
        assert snapshot_path(stem).read_text(encoding="utf-8").startswith("# mcp-snapshot v")


class TestSnapshotCatchesChange:
    """A fixture nobody can break is a fixture that proves nothing.

    Each of these is an edit that a screenshot might or might not reveal
    depending on where you looked. All of them must fail the round-trip.
    """

    @pytest.fixture
    def doc(self):
        return ezdxf.readfile(str(GOLDEN_DIR / "basic_shapes.dxf"))

    @pytest.fixture
    def baseline(self):
        return snapshot_path("basic_shapes").read_text(encoding="utf-8")

    def test_a_moved_entity(self, doc, baseline):
        line = next(e for e in doc.modelspace() if e.dxftype() == "LINE")
        line.dxf.start = (line.dxf.start.x + 5, line.dxf.start.y, 0)
        assert snapshot_doc(doc) != baseline

    def test_an_added_entity(self, doc, baseline):
        doc.modelspace().add_circle((150, 20), 5, dxfattribs={"layer": "SHAPES"})
        assert snapshot_doc(doc) != baseline

    def test_a_deleted_entity(self, doc, baseline):
        msp = doc.modelspace()
        msp.delete_entity(next(e for e in msp if e.dxftype() == "CIRCLE"))
        assert snapshot_doc(doc) != baseline

    def test_edited_text(self, doc, baseline):
        """The change a screenshot is worst at: a label that still looks like
        a label."""
        text = next(e for e in doc.modelspace() if e.dxftype() == "TEXT")
        text.dxf.text = "BASIC SHAPE"
        assert snapshot_doc(doc) != baseline

    def test_an_entity_moved_to_another_layer(self, doc, baseline):
        next(e for e in doc.modelspace() if e.dxftype() == "CIRCLE").dxf.layer = "BORDER"
        assert snapshot_doc(doc) != baseline

    def test_a_resized_entity(self, doc, baseline):
        next(e for e in doc.modelspace() if e.dxftype() == "CIRCLE").dxf.radius = 31
        assert snapshot_doc(doc) != baseline

    def test_a_sub_cell_nudge_on_a_layer_envelope_is_caught(self, doc, baseline):
        """Far too small to move the occupancy grid. The per-layer bbox lines
        are what catch it — 0.001 shows at six decimal places."""
        circle = next(e for e in doc.modelspace() if e.dxftype() == "CIRCLE")
        circle.dxf.center = (circle.dxf.center.x, circle.dxf.center.y + 0.001, 0)
        changed = snapshot_doc(doc)
        assert changed != baseline
        grid_lines = [l for l in changed.splitlines() if l.startswith("grid |")]
        assert grid_lines == [l for l in baseline.splitlines() if l.startswith("grid |")]

    def test_a_sub_cell_interior_nudge_is_a_known_blind_spot(self, doc, baseline):
        """The limit of the format, stated rather than assumed.

        A snapshot records each layer's *envelope* and a coarse grid. An entity
        that is not on its layer's envelope, moved by less than a grid cell, is
        invisible to both — here the circle spans x 70..130 while the arc
        already sets the layer's xmax at 170, so 0.001 in x changes no line.

        This is the rung boundary, not a defect: that question belongs to L1,
        entity(get) on the handle, which reports the coordinate directly. A
        snapshot is a structural regression check, not a coordinate ledger.
        """
        circle = next(e for e in doc.modelspace() if e.dxftype() == "CIRCLE")
        circle.dxf.center = (circle.dxf.center.x + 0.001, circle.dxf.center.y, 0)
        assert snapshot_doc(doc) == baseline

    def test_the_same_entity_moved_far_enough_is_caught(self, doc, baseline):
        """The blind spot is bounded: once the move reaches the envelope or a
        cell boundary, the snapshot sees it."""
        circle = next(e for e in doc.modelspace() if e.dxftype() == "CIRCLE")
        circle.dxf.center = (circle.dxf.center.x + 45, circle.dxf.center.y, 0)
        assert snapshot_doc(doc) != baseline

    def test_an_untouched_document_still_matches(self, doc, baseline):
        """The control arm: reading and re-writing must not drift."""
        assert snapshot_doc(doc) == baseline


class TestRebaselineIsDeliberate:
    """Rebaselining must stay a decision a person makes.

    The failure mode is specific: a snapshot test goes red, and the cheapest
    way to green is to regenerate the fixture — which converts a caught
    regression into a committed one and leaves a diff that looks like intent.
    Two independent gates, neither of which is something you trip over.
    """

    def test_refuses_without_the_flag(self):
        with pytest.raises(RebaselineRefused):
            rebaseline_snapshots(confirmed=False, env={REBASELINE_ENV: "1"})

    def test_refuses_without_the_environment_variable(self):
        with pytest.raises(RebaselineRefused):
            rebaseline_snapshots(confirmed=True, env={})

    def test_refuses_when_the_variable_is_not_exactly_one(self):
        for value in ("0", "true", "yes", ""):
            with pytest.raises(RebaselineRefused):
                rebaseline_snapshots(confirmed=True, env={REBASELINE_ENV: value})

    def test_the_variable_is_not_set_in_this_environment(self):
        """A test run must never be able to rebaseline as a side effect."""
        assert os.environ.get(REBASELINE_ENV) != "1"

    def test_a_refused_rebaseline_writes_nothing(self, tmp_path):
        before = {s: snapshot_path(s).read_bytes() for s in SNAPSHOT_SOURCES}
        with pytest.raises(RebaselineRefused):
            rebaseline_snapshots(confirmed=True, env={})
        assert {s: snapshot_path(s).read_bytes() for s in SNAPSHOT_SOURCES} == before

    def test_the_cli_exits_nonzero_rather_than_writing(self, capsys):
        from tests.generate_golden import main

        before = {s: snapshot_path(s).read_bytes() for s in SNAPSHOT_SOURCES}
        assert main(["--rebaseline-snapshots"]) == 2
        assert {s: snapshot_path(s).read_bytes() for s in SNAPSHOT_SOURCES} == before

    def test_the_default_run_does_not_mention_snapshots_as_a_thing_it_did(self):
        """Regenerating the DXFs must not quietly regenerate the fixtures with
        them — that would rebaseline through the back door."""
        import inspect

        from tests.generate_golden import main

        source = inspect.getsource(main)
        assert "rebaseline_snapshots" in source
        assert source.index("args.rebaseline") < source.index("rebaseline_snapshots(")


class TestSnapshotContent:
    """What the format is for, asserted on the committed text itself."""

    @pytest.fixture
    def snap(self):
        return snapshot_path("pid_example").read_text(encoding="utf-8")

    def test_every_string_in_the_drawing_is_readable(self, snap):
        for tag in ("TK-101", "P-101", "V-101"):
            assert f'"{tag}"' in snap

    def test_each_layer_gets_a_footprint(self, snap):
        for layer in ("PID-EQUIPMENT", "PID-PROCESS-PIPING", "PID-VALVES", "PID-ANNOTATION"):
            assert f"layer {layer} count " in snap

    def test_approximate_footprints_are_labelled(self, snap):
        """A text layer's bbox is a nominal glyph estimate. Saying so is the
        difference between "these do not overlap" and "these might not"."""
        assert "layer PID-ANNOTATION count 3 " in snap
        assert snap.count("exact no") >= 1

    def test_the_grid_is_hollow_where_the_drawing_is_hollow(self, snap):
        """A bbox-based grid would be a solid block of '#' and say nothing."""
        rows = [l for l in snap.splitlines() if l.startswith("grid |")]
        assert any("." in row for row in rows)

    def test_computed_extents_are_reported_separately_from_the_header(self, snap):
        """$EXTMIN only updates on a regen or zoom-extents, so after an erase
        it routinely describes geometry that is gone."""
        assert "extents-header " in snap
        assert "extents-computed " in snap

    def test_truncation_is_stated_in_the_file(self, snap):
        """A truncated snapshot that does not say so would compare partial
        data against a full fixture and call it a match."""
        assert "truncated no" in snap


def test_probes_module_and_lisp_port_are_kept_together():
    """The .snap is only a contract if both sides are expected to produce it."""
    lisp = (Path(__file__).parent.parent / "lisp-code" / "mcp_probes.lsp").read_text(
        encoding="utf-8"
    )
    assert "mcp-snapshot v" in lisp, (
        "mcp_probes.lsp must emit the same snapshot header as probes.py, or the "
        "golden fixture cannot be used to validate the AutoLISP port."
    )
