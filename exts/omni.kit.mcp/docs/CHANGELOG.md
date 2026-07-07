# Changelog

All notable changes to this extension are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] - 2026-07-04

### Added
- `McpBridge`: owner/namespace tool registry + NDJSON-over-TCP socket server +
  main-thread dispatch (`omni.kit.async_engine.run_coroutine`). One bridge, one
  port, per Kit process; the bridge owns the socket lifecycle.
- Wire protocol: NDJSON frames (one JSON object per `\n`-terminated line).
  Request `{type, params}`; responses `{status:"success", result}` /
  `{status:"error", message, ...diagnostics}`. Handlers return payloads (or
  raise — `ToolError` attaches diagnostics); the bridge wraps exactly once.
- Owner model: providers register as owners with a unique namespace; tools are
  advertised canonically as `<namespace>.<tool>`; bridge builtins stay
  bare-named. Snapshot-resolve dispatch with in-flight refcounts and draining
  `unregister_owner` makes hot-reload teardown safe.
- Multi-client: `listen(SOMAXCONN)`, per-connection isolation, per-client send
  locks — one client's disconnect/garbage never affects others.
- Consumer contract: a plain tool package declaring `MCP_NAMESPACE` and
  exposing `register(registrar)`; the autoloader owns the owner lifecycle and
  hands the entrypoint an `OwnerRegistrar`.
- Persistent registration: tool modules/paths from carb settings
  (`/persistent/exts/omni.kit.mcp/toolModules|toolPaths`, written by
  `scripts/install.py add-project`); `KIT_MCP_TOOL_*` env vars are per-launch
  overrides. Port from `OMNI_KIT_MCP_PORT` or the `autostartPort` setting.
- Builtins: `run_python` (persistent in-memory sessions), `list_tools`
  (discovery, internal), `reload_tools` (hot-reload one tool package in place;
  `*_state` submodules preserved, stale bytecode purged; doubles as first load
  for projects registered after launch), `list_bridges` (this box's advertised
  bridges — how remote callers find ephemeral-port siblings).
- Config knobs defined once (`knobs.py`): port (`autostartPort` /
  `OMNI_KIT_MCP_PORT`), bind address (`autostartHost` / `OMNI_KIT_MCP_BIND`,
  default localhost), tool modules/paths — env overrides settings, and
  `scripts/install.py` writes the same identities it reads.
- Port discovery: each bridge advertises `{pid, port, host, app}` in a
  per-user runtime portfile; clients given no port discover a lone bridge.
  A configured port already in use (second instance of the same app) falls
  back to an OS-assigned ephemeral port, still advertised.
- Injectable coroutine scheduler — the full socket server is unit-testable
  outside Kit (see repository `tests/`).
- Companion extension `omni.kit.mcp.panel`: registry-driven control panel;
  every button dispatches through `bridge.dispatch` (click == agent call).
