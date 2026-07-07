"""kit_mcp — the caller side of the omni.kit.mcp bridge.

Two layers with different weights:

- ``kit_mcp.client`` — the reference NDJSON client + CLI. Stdlib-only; imports
  anywhere. This is the layer scripts, tests, and doctors use. The bridge's
  wire protocol is not MCP — this client needs no SDK.
- ``kit_mcp.server`` — the MCP gateway (FastMCP front-end that makes bridge
  tools native for Claude/Cursor via .mcp.json). The only component in the
  ecosystem that speaks the Model Context Protocol; requires the ``mcp`` SDK.

The server import is lazy (PEP 562) so ``import kit_mcp`` / ``kit_mcp.client``
work in environments without the SDK; touching a server attribute (or running
the ``kit-mcp`` command) is what requires it.
"""

# Both layers resolve lazily (PEP 562): the package itself imports for free,
# `python -m kit_mcp.client` runs without the runpy double-import warning, and
# the SDK is only demanded when a server attribute is actually touched.
_CLIENT_ATTRS = {"client", "BridgeClient", "BridgeError", "call"}
_SERVER_ATTRS = {
    "server", "mcp", "main",
    "register_pre_dispatch_hook", "register_meta_category_hook",
}


def __getattr__(name):
    # importlib, not `from . import x`: the `from` form checks the package
    # attribute first, which re-enters this __getattr__ (infinite recursion
    # when the SDK is missing and the submodule can't import).
    import importlib
    if name in _CLIENT_ATTRS:
        client = importlib.import_module(".client", __name__)
        return client if name == "client" else getattr(client, name)
    if name in _SERVER_ATTRS:
        server = importlib.import_module(".server", __name__)
        return server if name == "server" else getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BridgeClient",
    "BridgeError",
    "call",
    "server",
    "mcp",
    "main",
    "register_pre_dispatch_hook",
    "register_meta_category_hook",
]
