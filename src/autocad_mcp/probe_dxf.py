"""ezdxf adapter for the probes — the harness that lets Layer 1 be tested.

probes.py is pure arithmetic over entity(get)-shaped dicts, which is exactly
what makes it portable to AutoLISP. This module is the part that cannot be
ported: it walks a DXF document and produces those dicts. In AutoCAD the same
job is done by `ssget "_X"` + `entget`, in a dozen lines of mcp_probes.lsp.

Keeping the walk here and the arithmetic there is what makes a golden .snap a
meaningful contract: both sides feed the same shapes into the same logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import ezdxf

from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
from autocad_mcp.probes import BBox, snapshot


def entity_info(entity) -> dict:
    """One entity as the dict entity(get) would return, plus what probes need."""
    info = {
        "type": entity.dxftype(),
        "handle": entity.dxf.handle,
        "layer": entity.dxf.get("layer", "0"),
    }
    info.update(EzdxfBackend._entity_geometry(entity))
    if info["type"] == "LWPOLYLINE":
        # Not part of entity(get)'s payload, but bbox_is_exact needs it: a
        # bulged segment arcs outside the vertex hull, so the vertex box
        # under-reports. DXF group 42 per vertex; group 42 on the whole
        # entity in AutoLISP's entget.
        info["has_bulge"] = any(abs(p[4]) > 1e-12 for p in entity.get_points("xyseb"))
    return info


#: The space every probe used to assume, and still defaults to.
MODEL = "Model"


def spaces(doc) -> list[str]:
    """Every space in the document: "Model" then each layout by name.

    Named the way AutoLISP names them — DXF group 410 carries "Model" or the
    layout's tab name, and `ssget "_X" '((410 . <name>))'` takes the same
    string. Two sides, one vocabulary.
    """
    return [MODEL] + [name for name in doc.layout_names() if name != "Model"]


def entities_in(doc, space: str = MODEL):
    """The entity container for one space.

    Draft 3 is the case that makes this necessary: 29,717 entities in model
    space, 26 in Layout1, the border and title block entirely in the latter and
    the diagram entirely in the former. A probe that silently means "model
    space" answers a different question than the one asked and looks like it
    answered the right one.
    """
    if space == MODEL:
        return doc.modelspace()
    if space not in doc.layout_names():
        raise KeyError(f"no such space: {space!r}; have {spaces(doc)}")
    return doc.layout(space)


def collect(doc, space: str = MODEL) -> tuple[list[dict], dict[str, list[dict]]]:
    """One space's entities and the block definitions, as probe-shaped dicts.

    Blocks are document-wide, not per-space: an INSERT in a layout references
    the same definition as one in model space.
    """
    entities = [entity_info(e) for e in entities_in(doc, space)]
    blocks = {
        block.name: [entity_info(e) for e in block]
        for block in doc.blocks
        if not block.name.startswith("*")
    }
    return entities, blocks


def layer_spaces(doc, layer: str) -> list[str]:
    """Which spaces a layer actually has entities in.

    A layer is a document-wide name, not a place. `border line 02` and
    `ECSI_Backpan` both exist in Draft 3's layer table and never appear in the
    same space, which is why comparing their bounding boxes produces a number
    that means nothing. mcp:overlap asks this before it answers.
    """
    return [s for s in spaces(doc) if any(True for _ in entities_in(doc, s).query(f'*[layer=="{layer}"]'))]


def header_extents(doc) -> BBox | None:
    """$EXTMIN/$EXTMAX, or None when they hold the empty-drawing sentinels."""
    emin = doc.header.get("$EXTMIN")
    emax = doc.header.get("$EXTMAX")
    if emin is None or emax is None:
        return None
    if abs(emin[0]) > 1e19 or abs(emax[0]) > 1e19:
        return None
    return BBox(float(emin[0]), float(emin[1]), float(emax[0]), float(emax[1]))


def hidden_layer_count(doc) -> int:
    """Layers that are off or frozen.

    They matter only on the AutoLISP side, where ssget cannot select on them
    and the grid quietly under-reports. Counted here so both snapshots carry
    the same line and a divergence has a stated cause.
    """
    return sum(1 for layer in doc.layers if layer.is_off() or layer.is_frozen())


def snapshot_doc(doc, cols: int = 24, rows: int = 12, space: str = MODEL) -> str:
    entities, blocks = collect(doc, space)
    return snapshot(
        entities, blocks, header_extents(doc),
        cols=cols, rows=rows, hidden_layers=hidden_layer_count(doc), space=space,
    )


def snapshot_file(path: str | Path, cols: int = 24, rows: int = 12, space: str = MODEL) -> str:
    return snapshot_doc(ezdxf.readfile(str(path)), cols=cols, rows=rows, space=space)


# ---------------------------------------------------------------------------
# CLI — the rung that costs no context at all
#
# The capture ladder's cheapest useful rung is "export a DXF and read it here",
# because parsing happens on this machine and nothing but the answer enters the
# conversation. That rung was documented before it was runnable: the functions
# above have been importable for a while, but there was no command, so reaching
# for it meant writing a throwaway script first and the ladder got skipped.
# ---------------------------------------------------------------------------


def text_items(doc, space: str = MODEL) -> list[dict]:
    """Every string in a space, with where it sits and what layer it is on.

    This is the answer to "what does that label say", which is otherwise a
    screenshot — and a screenshot is the one way of reading text that can be
    confidently wrong.
    """
    out = []
    for info in (entity_info(e) for e in entities_in(doc, space)):
        if info.get("text") is None:
            continue
        out.append(
            {
                "type": info["type"],
                "handle": info["handle"],
                "layer": info["layer"],
                "text": info["text"],
                "insert": info.get("insert"),
                "height": info.get("height"),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="python -m autocad_mcp.probe_dxf",
        description="Read a DXF exported from AutoCAD, without AutoCAD and without a screenshot.",
    )
    parser.add_argument("dxf", type=Path, help="path to a .dxf (drawing(save_as_dxf) writes one)")
    parser.add_argument("--space", default=MODEL, help='layout to read (default: "Model")')
    parser.add_argument("--cols", type=int, default=24, help="snapshot grid columns")
    parser.add_argument("--rows", type=int, default=12, help="snapshot grid rows")
    parser.add_argument("--text", action="store_true", help="dump every string instead of the grid")
    parser.add_argument("--spaces", action="store_true", help="list the layouts and exit")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    if not args.dxf.exists():
        print(f"not found: {args.dxf}", file=sys.stderr)
        return 1

    try:
        doc = ezdxf.readfile(str(args.dxf))
    except (OSError, ezdxf.DXFError) as exc:
        print(f"cannot read {args.dxf}: {exc}", file=sys.stderr)
        return 1

    if args.spaces:
        names = spaces(doc)
        print(_json.dumps(names) if args.json else "\n".join(names))
        return 0

    try:
        if args.text:
            items = text_items(doc, args.space)
            if args.json:
                print(_json.dumps(items, indent=2))
            else:
                for it in items:
                    where = it["insert"]
                    at = f"({where[0]}, {where[1]})" if where else "?"
                    print(f'{it["handle"]:>8}  {it["layer"]:<20} {at:<24} {it["text"]!r}')
                print(f"\n{len(items)} string(s) in {args.space}")
            return 0

        text = snapshot_doc(doc, cols=args.cols, rows=args.rows, space=args.space)
        print(_json.dumps({"space": args.space, "snapshot": text}, indent=2) if args.json else text)
    except KeyError as exc:
        # spaces() already names what is available; surface that rather than a traceback.
        print(str(exc).strip("\"'"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
