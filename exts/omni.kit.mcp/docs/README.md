# omni.kit.mcp

Generic MCP socket bridge for Omniverse Kit. One bridge, one port, per Kit
process: projects are plain tool packages (`MCP_NAMESPACE` +
`register(registrar)`) autoloaded by the bridge and advertised canonically as
`<namespace>.<tool>` over an NDJSON-over-TCP protocol, with every handler
dispatched on Kit's main thread. Builtins: `run_python` (the escape hatch),
`list_tools` (discovery), `reload_tools` (hot reload), `list_bridges`
(this box's advertised bridges).

See the repository root `README.md` for setup, the project contract, and the
protocol spec.
