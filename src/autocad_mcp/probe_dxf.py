"""ezdxf adapter for the probes — the harness that lets Layer 1 be tested.

probes.py is pure arithmetic over entity(get)-shaped dicts, which is exactly
what makes it portable to AutoLISP. This module is the part that cannot be
ported: it walks a DXF document and produces those dicts. In AutoCAD the same
job is done by `ssget "_X"` + `entget`, in a dozen lines of mcp_probes.lsp.

Keeping the walk here and the arithmetic there is what makes a golden .snap a
meaningful contract: both sides feed the same shapes into the same logic.
"""

from __future__ import annotations

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


def collect(doc) -> tuple[list[dict], dict[str, list[dict]]]:
    """Modelspace entities and block definitions, as probe-shaped dicts."""
    entities = [entity_info(e) for e in doc.modelspace()]
    blocks = {
        block.name: [entity_info(e) for e in block]
        for block in doc.blocks
        if not block.name.startswith("*")
    }
    return entities, blocks


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


def snapshot_doc(doc, cols: int = 24, rows: int = 12) -> str:
    entities, blocks = collect(doc)
    return snapshot(
        entities, blocks, header_extents(doc),
        cols=cols, rows=rows, hidden_layers=hidden_layer_count(doc),
    )


def snapshot_file(path: str | Path, cols: int = 24, rows: int = 12) -> str:
    return snapshot_doc(ezdxf.readfile(str(path)), cols=cols, rows=rows)
