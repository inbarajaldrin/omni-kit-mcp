# Domain glossary

Terms this repo uses precisely. Code, docs, and reviews use these words for
these things and no others.

- **Bridge** — the in-Kit NDJSON-over-TCP server (`omni.kit.mcp`). One per Kit
  process. Owns the tool registry and the main-thread dispatch. *Not itself an
  MCP server* — see Gateway.
- **Gateway** — `kit_mcp.server`, the FastMCP front-end. The only component in
  the ecosystem that speaks the Model Context Protocol; everything else speaks
  the wire protocol.
- **Wire protocol** — the bridge's own contract: NDJSON frames,
  `{"type","params"}` requests, `{"status","result"|"message"}` envelopes.
  Defined by this repo; hardcoding it *is* implementing it.
- **Reference client** — `kit_mcp/client.py`: the one caller-side
  implementation of the wire protocol (framing, envelope decoding, health,
  retry). Stdlib-only and single-file by contract — copyable as-is. Every
  consumer imports it or copies it; nobody re-implements it.
- **Portfile / discovery** — each running bridge advertises `{pid, port, host,
  app}` in `$XDG_RUNTIME_DIR/omni-kit-mcp/<pid>-<port>.json`. Clients given no
  port discover a lone bridge (and error on ambiguity). Ports are per Kit
  *process*, so numbers are convention (installer 9009, Lab 9010), never
  identity — discovery, not a fixed map, is the resolution.
- **Tool package** — a plain Python package with `MCP_NAMESPACE` and
  `register(registrar)`; the consumer contract.
- **Owner / namespace** — a registered provider and its public tool prefix;
  tools are canonical `<namespace>.<tool>`.
- **Knob** — a configuration value with exactly one identity (env name,
  settings keys, precedence rule), defined once in `omni_kit_mcp/knobs.py`.
  Readers call it; the writer (`scripts/install.py`) imports its names.
- **Iron rule** — UI widgets never call tool logic directly; every widget
  dispatches through `bridge.dispatch(...)`, the same entrance a socket
  request takes.
- **Payload contract** — handlers return their payload or raise
  (`ToolError` for structured diagnostics); the bridge alone builds envelopes.

## The hardcoding rule

The only things hardcoded anywhere are **the contracts this repo defines**:
the wire protocol, verb names, knob *names*, env-var *names*. Every fact about
a machine or an install — ports in use, paths, versions, layouts — is
resolved at runtime or recorded by setup, never baked into code. Cross-process
names (env vars) are shared as documented contract pinned by test, not by
import.
