"""Measure what a Claude Code session actually cost, and where it went.

Run over a session transcript:

    uv run python -m tests.token_audit <session.jsonl>
    uv run python -m tests.token_audit <session.jsonl> --json

Transcripts live in ``~/.claude/projects/<project-slug>/*.jsonl``.

Why this exists
---------------
Cost is not the price of a tool call; it is context size multiplied by request
count. Anything that enters the context window is re-read by every request that
follows it, so a single screenshot early in a long session is charged again and
again. That is invisible in any per-call accounting, which is why sessions can
feel cheap and bill enormously.

This script reports the multiplied figure (``resident_tokens``) next to the
entry price, so the two can be told apart. It reads the transcript in a single
streaming pass and never holds the file in memory — transcripts reach tens of
megabytes, most of it base64 image data.

The numbers it reports are measurements, not estimates, with one exception:
``resident_tokens`` assumes an image stays in context from the request that
introduced it through the end of the session. Compaction can evict it earlier,
so treat that column as an upper bound.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Vision models tile an image into 28x28 patches, so cost depends on dimensions
# alone — not file size, not format, not JPEG quality.
_PATCH = 28

# Tools that answer a question in text for tens of tokens. A screenshot taken
# without one of these in the preceding few calls is the pattern this script is
# looking for: reaching for pixels before trying the cheap question.
CHEAP_PROBES = {
    "mcp__autocad-mcp__drawing",
    "mcp__autocad-mcp__entity",
    "mcp__autocad-mcp__layer",
    "mcp__autocad-mcp__block",
    "mcp__autocad-mcp__system",
}

# How many preceding tool calls count as "the model tried something cheap first".
LADDER_LOOKBACK = 3


def visual_tokens(width: int, height: int) -> int:
    """Token cost of an image of this size, as billed by the vision API."""
    return -(-width // _PATCH) * -(-height // _PATCH)


def image_size(raw: bytes) -> tuple[int, int] | None:
    """Decode pixel dimensions from PNG or JPEG bytes, without PIL."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
        return (
            int.from_bytes(raw[16:20], "big"),
            int.from_bytes(raw[20:24], "big"),
        )

    if raw[:2] == b"\xff\xd8":  # JPEG: walk segments to a start-of-frame marker
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (
                    int.from_bytes(raw[i + 7 : i + 9], "big"),
                    int.from_bytes(raw[i + 5 : i + 7], "big"),
                )
            i += 2 + int.from_bytes(raw[i + 2 : i + 4], "big")
    return None


@dataclass
class ImageRecord:
    request_index: int
    width: int
    height: int
    tokens: int
    source_tool: str
    bytes_b64: int

    @property
    def resident_multiplier(self) -> int:
        return self._later_requests

    _later_requests: int = 0

    @property
    def resident_tokens(self) -> int:
        return self.tokens * max(1, self._later_requests)


@dataclass
class Audit:
    path: Path
    requests: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    user_turns: int = 0
    images: list[ImageRecord] = field(default_factory=list)
    tool_calls: Counter = field(default_factory=Counter)
    tool_result_bytes: Counter = field(default_factory=Counter)
    structured_content_records: int = 0
    duplicated_blob_records: int = 0
    screenshots_without_probe: int = 0
    screenshots_total: int = 0

    @property
    def image_tokens(self) -> int:
        return sum(i.tokens for i in self.images)

    @property
    def resident_image_tokens(self) -> int:
        return sum(i.resident_tokens for i in self.images)

    @property
    def avg_context(self) -> float:
        if not self.requests:
            return 0.0
        return (self.cache_read + self.cache_creation) / self.requests

    @property
    def tokens_per_turn(self) -> float:
        """Harness efficiency: total context re-read per thing the user asked for."""
        if not self.user_turns:
            return 0.0
        return self.cache_read / self.user_turns


def _blocks(message: dict):
    """Yield every content block, descending into tool_result payloads."""
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        yield block
        if block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict):
                        yield sub


def _tool_label(block: dict) -> str:
    """Tool name plus its operation, so `drawing(info)` is distinct from `drawing(save)`."""
    name = block.get("name", "?")
    args = block.get("input") or {}
    op = args.get("operation")
    return f"{name}({op})" if op else name


def _is_screenshot_call(block: dict) -> bool:
    args = block.get("input") or {}
    if args.get("include_screenshot"):
        return True
    return block.get("name", "").endswith("view") and args.get("operation") == "get_screenshot"


def audit(path: Path) -> Audit:
    result = Audit(path=path)
    seen_requests: set[str] = set()
    recent_tools: list[str] = []
    pending_tool_by_id: dict[str, str] = {}

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = rec.get("type")
            if rtype == "user":
                # A genuine user turn, not a tool result being fed back. Decide
                # from the content blocks rather than the toolUseResult field,
                # which is present but empty on some records.
                msg = rec.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    result.user_turns += bool(content.strip())
                elif isinstance(content, list):
                    kinds = {b.get("type") for b in content if isinstance(b, dict)}
                    if "tool_result" not in kinds and "text" in kinds:
                        result.user_turns += 1

            # Usage is reported per assistant record, but several records can
            # share one requestId (streamed iterations). Charging each record
            # would multiply the bill; dedupe by requestId.
            req = rec.get("requestId")
            if rtype == "assistant" and req and req not in seen_requests:
                seen_requests.add(req)
                usage = (rec.get("message") or {}).get("usage") or {}
                result.requests += 1
                result.cache_read += usage.get("cache_read_input_tokens") or 0
                result.cache_creation += usage.get("cache_creation_input_tokens") or 0
                result.input_tokens += usage.get("input_tokens") or 0
                result.output_tokens += usage.get("output_tokens") or 0

            # The duplicate-payload signature: an MCP result echoed into
            # structuredContent as well as a real content block.
            if '"structuredContent"' in line:
                result.structured_content_records += 1
                if line.count('"data"') > 1:
                    result.duplicated_blob_records += 1

            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue

            for block in _blocks(msg):
                btype = block.get("type")

                if btype == "tool_use":
                    label = _tool_label(block)
                    result.tool_calls[label] += 1
                    if block.get("id"):
                        pending_tool_by_id[block["id"]] = label

                    if _is_screenshot_call(block):
                        result.screenshots_total += 1
                        window = recent_tools[-LADDER_LOOKBACK:]
                        if not any(t.split("(")[0] in CHEAP_PROBES for t in window):
                            result.screenshots_without_probe += 1
                    recent_tools.append(label)

                elif btype == "tool_result":
                    label = pending_tool_by_id.get(block.get("tool_use_id"), "?")
                    result.tool_result_bytes[label] += len(json.dumps(block))

                elif btype == "image":
                    source = block.get("source") or {}
                    data = source.get("data") or ""
                    try:
                        raw = base64.b64decode(data[:2048] + "==", validate=False)
                    except Exception:
                        raw = b""
                    size = image_size(raw)
                    if size:
                        result.images.append(
                            ImageRecord(
                                request_index=result.requests,
                                width=size[0],
                                height=size[1],
                                tokens=visual_tokens(*size),
                                source_tool=recent_tools[-1] if recent_tools else "?",
                                bytes_b64=len(data),
                            )
                        )

    for img in result.images:
        img._later_requests = max(0, result.requests - img.request_index)
    return result


def _fmt(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def render(a: Audit) -> str:
    out: list[str] = []
    out.append(f"Session: {a.path.name}")
    out.append("")
    out.append("  Requests (deduped by requestId) : %d" % a.requests)
    out.append("  User turns                      : %d" % a.user_turns)
    out.append("  Cache-read tokens               : %s" % _fmt(a.cache_read))
    out.append("  Cache-creation tokens           : %s" % _fmt(a.cache_creation))
    out.append("  Output tokens                   : %s" % _fmt(a.output_tokens))
    out.append("  Avg context per request         : %s" % _fmt(a.avg_context))
    out.append("  Cache-read per user turn        : %s" % _fmt(a.tokens_per_turn))
    out.append("")

    out.append("  Images: %d" % len(a.images))
    if a.images:
        out.append("    Entry cost (sum of visual tokens) : %s" % _fmt(a.image_tokens))
        out.append(
            "    Resident cost (x later requests)  : %s   <- the figure that explains the bill"
            % _fmt(a.resident_image_tokens)
        )
        share = 100 * a.resident_image_tokens / a.cache_read if a.cache_read else 0
        out.append("    Share of all cache-read           : %.1f%%" % share)
        out.append("")
        out.append("    %-28s %11s %8s %10s" % ("source", "dimensions", "tokens", "resident"))
        for img in a.images[:12]:
            out.append(
                "    %-28s %11s %8d %10s"
                % (
                    img.source_tool[:28],
                    f"{img.width}x{img.height}",
                    img.tokens,
                    _fmt(img.resident_tokens),
                )
            )
        if len(a.images) > 12:
            out.append("    ... %d more" % (len(a.images) - 12))
    out.append("")

    if a.screenshots_total:
        bad = a.screenshots_without_probe
        out.append(
            "  Ladder compliance: %d of %d screenshots had no cheap probe in the previous %d calls"
            % (bad, a.screenshots_total, LADDER_LOOKBACK)
        )
        out.append("")

    out.append("  Tool calls by payload:")
    rows = sorted(a.tool_result_bytes.items(), key=lambda kv: -kv[1])[:12]
    total_bytes = sum(a.tool_result_bytes.values()) or 1
    out.append("    %-34s %6s %10s %7s" % ("tool", "calls", "bytes", "share"))
    for label, nbytes in rows:
        out.append(
            "    %-34s %6d %10s %6.1f%%"
            % (label[:34], a.tool_calls.get(label, 0), _fmt(nbytes), 100 * nbytes / total_bytes)
        )
    out.append("")

    out.append("  structuredContent records : %d" % a.structured_content_records)
    out.append("  ...with a duplicated blob : %d" % a.duplicated_blob_records)
    if a.duplicated_blob_records:
        out.append(
            "    Payloads are being echoed into structuredContent. If this is a server\n"
            "    you control, register its tools with structured_output=False."
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcript", type=Path, nargs="+")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    for path in args.transcript:
        if not path.exists():
            print(f"not found: {path}", file=sys.stderr)
            return 1
        a = audit(path)
        if args.json:
            print(
                json.dumps(
                    {
                        "session": a.path.name,
                        "requests": a.requests,
                        "user_turns": a.user_turns,
                        "cache_read": a.cache_read,
                        "cache_creation": a.cache_creation,
                        "output_tokens": a.output_tokens,
                        "avg_context": round(a.avg_context, 1),
                        "images": len(a.images),
                        "image_entry_tokens": a.image_tokens,
                        "image_resident_tokens": a.resident_image_tokens,
                        "screenshots_total": a.screenshots_total,
                        "screenshots_without_probe": a.screenshots_without_probe,
                        "structured_content_records": a.structured_content_records,
                        "duplicated_blob_records": a.duplicated_blob_records,
                        "tool_calls": dict(a.tool_calls),
                    },
                    indent=2,
                )
            )
        else:
            print(render(a))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
