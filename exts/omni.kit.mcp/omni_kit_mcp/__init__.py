"""omni.kit.mcp — generic MCP socket bridge for Omniverse Kit.

Public API:
    get_mcp_bridge() -> McpBridge   # the process-global bridge singleton
    McpBridge                       # owner registry + NDJSON socket server + main-thread dispatch
    ToolDefinition                  # one tool (local name, description, parameters, handler)
    OwnerRegistrar                  # owner-bound registration facade (what register() receives)
    ToolError                       # handler-raised failure with structured diagnostics
    load_tool_modules / reload_tool_module / prepend_tool_paths  # autoload machinery
"""

import importlib.util as _ilu

from .autoload import load_tool_modules, prepend_tool_paths, reload_tool_module
from .bridge import McpBridge, OwnerRegistrar, ToolDefinition, get_mcp_bridge
from .protocol import ToolError

# extension.py is Kit-only (carb + omni.ext). Import it only when running inside
# Kit, so the package's pure-stdlib surface stays importable off-Kit for lint /
# standalone tests. Gate on carb's presence rather than try/except ImportError
# so a *real* bug in extension.py still raises loudly inside Kit instead of
# silently leaving the extension unregistered.
if _ilu.find_spec("carb") is not None:
    from .extension import McpBridgeExtension
else:  # off-Kit: nobody instantiates the IExt, so None is safe
    McpBridgeExtension = None

__all__ = [
    "McpBridge",
    "OwnerRegistrar",
    "ToolDefinition",
    "ToolError",
    "get_mcp_bridge",
    "McpBridgeExtension",
    "load_tool_modules",
    "reload_tool_module",
    "prepend_tool_paths",
]
