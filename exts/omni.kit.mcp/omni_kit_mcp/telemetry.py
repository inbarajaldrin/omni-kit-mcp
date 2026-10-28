"""Optional, domain-blind telemetry for the bridge's registered-tool boundary.

The sink is deliberately selected by environment.  With no declaration this
module is never constructed and dispatch pays one ``is None`` test.  With one,
each completed call is one durable JSON line.  Payloads stay out: only the tool
name, timing, ordinal, outcome and a digest of the arguments cross this layer.
"""

import hashlib
import json
import os
import threading
import time


SCHEMA = "pa.authoring-telemetry/1"
SINK_ENV = "OMNI_KIT_MCP_AUTHORING_TELEMETRY"


def configured():
    path = (os.getenv(SINK_ENV) or "").strip()
    return Sink(path) if path else None


class Sink:
    """Crash-safe append sink.  An I/O failure is counted and never escapes."""

    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self._lock = threading.Lock()
        self._sequence = self._last_sequence()
        self._dropped = 0

    def _last_sequence(self):
        try:
            with open(self.path, "rb") as handle:
                lines = handle.read().splitlines()
            for line in reversed(lines):
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if row.get("schema") == SCHEMA and isinstance(row.get("sequence"), int):
                    return row["sequence"]
        except OSError:
            pass
        return 0

    @staticmethod
    def _arguments_digest(params):
        encoded = json.dumps(params or {}, default=repr, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def record(self, tool, params, started_at, response):
        """Append and fsync one call; return False instead of delaying failure into dispatch."""
        completed = time.time()
        with self._lock:
            sequence = self._sequence + 1
            row = {
                "schema": SCHEMA,
                "event": "tool_call",
                "sequence": sequence,
                "tool": str(tool),
                "invoked_at_wall_s": started_at,
                "completed_at_wall_s": completed,
                "duration_wall_s": max(0.0, completed - started_at),
                "outcome": (response or {}).get("status", "unknown"),
                "arguments_sha256": self._arguments_digest(params),
                "dropped_before": self._dropped,
            }
            try:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    payload = (json.dumps(row, sort_keys=True, separators=(",", ":"))
                               + "\n").encode("utf-8")
                    if os.write(fd, payload) != len(payload):
                        raise OSError("short append to authoring telemetry sink")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except Exception:  # telemetry must never replace the tool's answer
                self._dropped += 1
                return False
            self._sequence = sequence
            self._dropped = 0
            return True
