"""Live check for system(switch_doc): A -> B -> A, DWGNAME asserted at each hop.

Read-only: it switches tabs and reads DWGNAME, nothing else. Needs AutoCAD LT
with both drawings open and mcp_dispatch.lsp loaded in each.

    uv run python scripts/verify_switch_doc.py "A.dwg" "B.dwg"

If the Start tab is in front when it begins, it first checks the Start-tab
guard: backend detection must raise start_tab_active, not fall to ezdxf.

Exit 0 only when every hop is confirmed by DWGNAME read independently of the
switch_doc payload.
"""

from __future__ import annotations

import asyncio
import json
import sys

from autocad_mcp import client, doc_switch, server
from autocad_mcp.config import detect_backend


async def dwgname() -> str | None:
    out = json.loads(await server.system(operation="execute_lisp", data={"code": '(getvar "DWGNAME")'}))
    return out.get("payload") if out.get("ok") else None


async def main(a: str, b: str) -> int:
    frame = doc_switch.find_autocad_frame()
    if not frame:
        print("ABORT: no AutoCAD frame")
        return 2
    _, docs, active = doc_switch.list_documents(frame)
    print(f"frame {frame}; open: {doc_switch.open_document_titles(frame)}; active: {dict(docs).get(active)}")
    failures = 0

    if dict(docs).get(active) == doc_switch.START_TITLE:
        try:
            got = detect_backend()
            print(f"FAIL start guard: detect_backend() returned {got!r}")
            failures += 1
        except RuntimeError as e:
            ok = str(e).startswith("start_tab_active")
            print(f"{'PASS' if ok else 'FAIL'} start guard: {str(e)[:90]}...")
            failures += not ok

    print(f"{'hop':<4} {'requested':<40} {'before':<40} {'after':<40} {'dwgname (payload)':<40} {'dwgname (independent)':<40} result")
    for i, target in enumerate((a, b, a), 1):
        out = json.loads(await server.system(operation="switch_doc", data={"dwg": target}))
        p = out.get("payload") or {}
        independent = await dwgname() if out.get("ok") else None
        ok = bool(out.get("ok")) and (independent or "").lower() == target.lower()
        failures += not ok
        print(f"{i:<4} {target:<40} {str(p.get('before')):<40} {str(p.get('after')):<40} "
              f"{str(p.get('dwgname')):<40} {str(independent):<40} {'PASS' if ok else 'FAIL ' + str(out.get('error'))}")
    client._backend = None
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
