# omni.kit.mcp

Generic MCP socket bridge for Omniverse Kit. One bridge, one port, per Kit
process: projects are plain tool packages (`MCP_NAMESPACE` + an optional
`register(registrar)` — without one, the package's public functions register
directly as a declared toolset) autoloaded by the bridge and advertised canonically as
`<namespace>.<tool>` over an NDJSON-over-TCP protocol, with every handler
dispatched on Kit's main thread. Builtins: `run_python` (the escape hatch),
`list_tools` (discovery), `reload_tools` (hot reload), `list_bridges`
(this box's advertised bridges), `put_file` / `get_file` / `stat_file` (remote file push/pull/verify),
`status` (instance identity: pid, port, headless-vs-headful, app version — multi-instance verification).

See the repository root `README.md` for setup and the project contract, and
`docs/protocol.md` there for the protocol.
