"""protocol.py — NDJSON framing + envelope."""

import json

import pytest

from omni_kit_mcp import protocol


def test_encode_frame_is_one_line():
    data = protocol.encode_frame({"a": 1, "s": "x\ny"})
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1  # embedded newlines are escaped by json
    assert json.loads(data.decode()) == {"a": 1, "s": "x\ny"}


def test_encode_frame_falls_back_to_repr():
    class Odd:
        def __repr__(self):
            return "<odd>"

    decoded = json.loads(protocol.encode_frame({"v": Odd()}).decode())
    assert decoded["v"] == "<odd>"


def test_decoder_multiple_and_partial_frames():
    d = protocol.FrameDecoder()
    out = list(d.feed(b'{"a": 1}\n{"b": 2}\n{"c":'))
    assert out == [({"a": 1}, None), ({"b": 2}, None)]
    out = list(d.feed(b" 3}\n"))
    assert out == [({"c": 3}, None)]


def test_decoder_bad_line_is_isolated():
    d = protocol.FrameDecoder()
    out = list(d.feed(b'not json\n{"ok": true}\n'))
    assert out[0][0] is None and "invalid JSON" in out[0][1]
    assert out[1] == ({"ok": True}, None)


def test_decoder_skips_blank_lines():
    d = protocol.FrameDecoder()
    assert list(d.feed(b"\n\n{}\n")) == [({}, None)]


def test_envelopes():
    assert protocol.success({"x": 1}) == {"status": "success", "result": {"x": 1}}
    assert protocol.success(None) == {"status": "success", "result": {}}
    err = protocol.error("boom", traceback="tb", status="ignored", nothing=None)
    assert err == {"status": "error", "message": "boom", "traceback": "tb"}


def test_tool_error_carries_details():
    e = protocol.ToolError("bad", details={"output": "partial"})
    assert str(e) == "bad" and e.details == {"output": "partial"}
