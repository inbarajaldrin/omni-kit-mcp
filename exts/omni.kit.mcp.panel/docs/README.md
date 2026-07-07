# omni.kit.mcp.panel

Registry-driven control panel for the `omni.kit.mcp` bridge. One collapsible
section per namespace, one row per registered tool, parameter widgets generated
from each tool's schema. Install once; every project's tools appear
automatically when their packages register — nothing per-project, nothing
generated on disk.

Every button dispatches through `bridge.dispatch(...)` — the same entrance a
socket request takes — so a human click and an agent call are the same code
path by construction.

See the repository root `README.md` for the full setup.
