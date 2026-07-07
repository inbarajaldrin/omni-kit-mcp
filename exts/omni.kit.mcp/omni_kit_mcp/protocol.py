"""Wire protocol for the omni.kit.mcp bridge — NDJSON frames + response envelope.

Framing (NDJSON): every message is exactly one JSON object serialized on a
single line and terminated by ``\\n``. Message boundaries are therefore exact —
no parse-until-valid guessing — and one malformed line yields one error
response instead of poisoning the connection buffer.

Request : {"type": "<canonical tool name>", "params": {...}}
Response: {"status": "success", "result": <payload>}
        | {"status": "error",   "message": "...", ...extra diagnostic keys}

Handlers never speak this envelope. A tool handler returns its payload (any
JSON-serializable value; ``None`` becomes ``{}``) or raises. The bridge wraps
exactly once. ``ToolError`` lets a handler attach structured diagnostics
(e.g. run_python's captured stdout) to the error frame.
"""

import json
from typing import Any, Dict, Iterator, Optional


class ToolError(Exception):
    """Handler-raised failure with optional structured diagnostics.

    ``details`` keys are merged into the error frame alongside ``message``
    (reserved keys ``status``/``message`` in details are ignored).
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


def success(result: Any) -> Dict[str, Any]:
    """Wrap a handler payload in the success envelope (single wrap, always)."""
    return {"status": "success", "result": {} if result is None else result}


def error(message: str, **extra: Any) -> Dict[str, Any]:
    """Build an error envelope; extra keys (traceback, output, ...) ride along."""
    frame = {"status": "error", "message": message}
    for k, v in extra.items():
        if k not in ("status", "message") and v is not None:
            frame[k] = v
    return frame


def encode_frame(obj: Any) -> bytes:
    """One JSON object -> one NDJSON line (UTF-8, newline-terminated)."""
    return json.dumps(obj, default=_json_fallback).encode("utf-8") + b"\n"


def _json_fallback(obj: Any) -> str:
    """Last-resort serializer so a handler returning e.g. a Gf.Vec3d or numpy
    scalar degrades to its repr instead of killing the response frame."""
    return repr(obj)


class FrameDecoder:
    """Incremental NDJSON decoder: feed raw bytes, iterate complete frames.

    Each yielded item is ``(obj, None)`` for a parsed frame or ``(None, err)``
    for a line that wasn't valid JSON — the caller answers the bad line with an
    error response and the connection stays healthy.
    """

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> Iterator[tuple]:
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode("utf-8")), None
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                yield None, f"invalid JSON frame: {e}"
