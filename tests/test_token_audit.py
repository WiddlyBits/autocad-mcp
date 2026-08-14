"""Tests for the session cost auditor.

The auditor's job is to be trusted about money, so these tests pin the two things
that are easy to get quietly wrong: deduplicating requests (charging per record
instead of per request inflates every figure) and the residency multiplier (the
whole reason a screenshot costs more than its sticker price).
"""

import base64
import json
import struct
import zlib

import pytest

from tests.token_audit import (
    LADDER_LOOKBACK,
    audit,
    image_size,
    render,
    visual_tokens,
)


def _png_bytes(width: int, height: int) -> bytes:
    """A real, minimal PNG of the given dimensions."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _assistant(request_id: str, cache_read: int, content: list) -> dict:
    return {
        "type": "assistant",
        "requestId": request_id,
        "message": {
            "role": "assistant",
            "content": content,
            "usage": {
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": 0,
                "input_tokens": 1,
                "output_tokens": 10,
            },
        },
    }


def _tool_use(uid: str, name: str, **args) -> dict:
    return {"type": "tool_use", "id": uid, "name": name, "input": args}


def _tool_result(uid: str, blocks: list) -> dict:
    return {
        "type": "user",
        "toolUseResult": {},
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": uid, "content": blocks}
        ]},
    }


def _image_block(width: int, height: int) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(_png_bytes(width, height)).decode(),
        },
    }


def _write(tmp_path, records) -> "Path":  # noqa: F821 - pytest tmp_path
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------


class TestVisualTokens:
    @pytest.mark.parametrize(
        "w, h, expected",
        [(1928, 1218, 3036), (1999, 1778, 4608), (500, 400, 270), (28, 28, 1)],
    )
    def test_known_sizes(self, w, h, expected):
        assert visual_tokens(w, h) == expected


class TestImageSize:
    def test_reads_png_header(self):
        assert image_size(_png_bytes(640, 480)) == (640, 480)

    def test_ignores_unknown_format(self):
        assert image_size(b"not an image at all") is None


# ---------------------------------------------------------------------------
# Request accounting
# ---------------------------------------------------------------------------


class TestRequestAccounting:
    def test_records_sharing_a_request_id_are_charged_once(self, tmp_path):
        """Streaming emits several records per request; charging each inflates everything."""
        path = _write(
            tmp_path,
            [
                _assistant("req_1", 1000, [{"type": "text", "text": "a"}]),
                _assistant("req_1", 1000, [{"type": "text", "text": "b"}]),
                _assistant("req_2", 2000, [{"type": "text", "text": "c"}]),
            ],
        )
        a = audit(path)
        assert a.requests == 2
        assert a.cache_read == 3000

    def test_counts_user_turns_separately_from_tool_results(self, tmp_path):
        path = _write(
            tmp_path,
            [
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "text", "text": "do the thing"}
                ]}},
                _assistant("req_1", 100, [_tool_use("t1", "Bash", command="ls")]),
                _tool_result("t1", [{"type": "text", "text": "output"}]),
            ],
        )
        a = audit(path)
        assert a.user_turns == 1


# ---------------------------------------------------------------------------
# Residency — the point of the whole exercise
# ---------------------------------------------------------------------------


class TestResidency:
    def test_an_early_image_costs_more_than_a_late_one(self, tmp_path):
        """Same image, same entry price, different bill."""
        records = [
            _assistant("req_1", 100, [_tool_use("t1", "mcp__autocad-mcp__view",
                                                operation="get_screenshot")]),
            _tool_result("t1", [_image_block(280, 280)]),
        ]
        records += [
            _assistant(f"req_pad{i}", 100, [{"type": "text", "text": "..."}])
            for i in range(8)
        ]
        records += [
            _assistant("req_late", 100, [_tool_use("t2", "mcp__autocad-mcp__view",
                                                   operation="get_screenshot")]),
            _tool_result("t2", [_image_block(280, 280)]),
        ]
        a = audit(_write(tmp_path, records))

        assert len(a.images) == 2
        early, late = a.images
        assert early.tokens == late.tokens  # identical entry price
        assert early.resident_tokens > late.resident_tokens

    def test_resident_cost_is_entry_cost_times_later_requests(self, tmp_path):
        records = [
            _assistant("req_1", 100, [_tool_use("t1", "mcp__autocad-mcp__view",
                                                operation="get_screenshot")]),
            _tool_result("t1", [_image_block(280, 280)]),
            _assistant("req_2", 100, [{"type": "text", "text": "x"}]),
            _assistant("req_3", 100, [{"type": "text", "text": "y"}]),
        ]
        a = audit(_write(tmp_path, records))
        (img,) = a.images
        assert img.tokens == visual_tokens(280, 280)
        assert img.resident_tokens == img.tokens * 2


# ---------------------------------------------------------------------------
# Ladder compliance
# ---------------------------------------------------------------------------


class TestLadderCompliance:
    def test_screenshot_after_a_cheap_probe_is_compliant(self, tmp_path):
        path = _write(
            tmp_path,
            [
                _assistant("r1", 10, [_tool_use("t1", "mcp__autocad-mcp__drawing",
                                                operation="info")]),
                _assistant("r2", 10, [_tool_use("t2", "mcp__autocad-mcp__view",
                                                operation="get_screenshot")]),
            ],
        )
        a = audit(path)
        assert a.screenshots_total == 1
        assert a.screenshots_without_probe == 0

    def test_screenshot_with_no_probe_in_range_is_flagged(self, tmp_path):
        filler = [
            _assistant(f"r{i}", 10, [_tool_use(f"f{i}", "Bash", command="echo")])
            for i in range(LADDER_LOOKBACK + 1)
        ]
        path = _write(
            tmp_path,
            [
                _assistant("r0", 10, [_tool_use("t0", "mcp__autocad-mcp__drawing",
                                                operation="info")]),
                *filler,
                _assistant("rs", 10, [_tool_use("ts", "mcp__autocad-mcp__view",
                                                operation="get_screenshot")]),
            ],
        )
        a = audit(path)
        assert a.screenshots_without_probe == 1

    def test_include_screenshot_counts_as_a_screenshot(self, tmp_path):
        path = _write(
            tmp_path,
            [
                _assistant("r1", 10, [_tool_use("t1", "mcp__autocad-mcp__entity",
                                                operation="create_line",
                                                include_screenshot=True)]),
            ],
        )
        assert audit(path).screenshots_total == 1


# ---------------------------------------------------------------------------
# Duplicate-payload detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    def test_flags_structured_content_with_a_repeated_blob(self, tmp_path):
        blob = base64.b64encode(_png_bytes(56, 56)).decode()
        rec = {
            "type": "user",
            "toolUseResult": {
                "content": [{"type": "image", "data": blob}],
                "structuredContent": {"result": [{"type": "image", "data": blob}]},
            },
            "message": {"role": "user", "content": []},
        }
        a = audit(_write(tmp_path, [rec]))
        assert a.structured_content_records == 1
        assert a.duplicated_blob_records == 1

    def test_clean_result_is_not_flagged(self, tmp_path):
        rec = {
            "type": "user",
            "toolUseResult": {"content": [{"type": "text", "text": "ok"}]},
            "message": {"role": "user", "content": []},
        }
        a = audit(_write(tmp_path, [rec]))
        assert a.duplicated_blob_records == 0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_malformed_lines_are_skipped(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            "\n".join(
                [
                    "{not json",
                    "",
                    json.dumps(_assistant("r1", 500, [{"type": "text", "text": "ok"}])),
                ]
            ),
            encoding="utf-8",
        )
        a = audit(path)
        assert a.requests == 1
        assert a.cache_read == 500

    def test_renders_without_error_on_an_empty_session(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        assert "Requests" in render(audit(path))
