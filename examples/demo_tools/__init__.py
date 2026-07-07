"""demo_tools — the template tool package for the omni.kit.mcp bridge.

Copy this package into your project to onboard it. A tool project is a plain
Python package (NOT a Kit extension) that declares a namespace and exposes one
entrypoint; the bridge autoloads it and advertises its tools canonically as
``<namespace>.<tool>`` (here: ``demo.ping``, ``demo.add_cube``).

Register it once, persistently (no launch flags ever again):

    python3 scripts/install.py add-project --path <this dir's parent> --module demo_tools

or per-launch:  KIT_MCP_TOOL_PATHS=<parent> KIT_MCP_TOOL_MODULES=demo_tools

Handler contract: receive params as kwargs, return a JSON-serializable payload
(or raise; ``ToolError`` attaches structured diagnostics). Never build the
wire envelope. Import Kit modules (omni, pxr) INSIDE handlers so the package
stays importable off-Kit for tests.

Hot reload: ``reload_tools {"module": "demo_tools"}`` re-imports this package
in place. Live handles that must survive a reload (articulation views, physics
handles) belong in a submodule named ``*_state`` (see ``demo_tools_state`` in
the docs) — state modules are preserved across reloads by convention.
"""

MCP_NAMESPACE = "demo"


def register(registrar):
    """The autoload entrypoint. Receives an owner-bound registrar — no owner
    ids, no ports, no lifecycle; just declare tools."""

    @registrar.tool(
        "Liveness check: echoes its message back with the namespace.",
        {"message": {"type": "string", "description": "Text to echo back."}},
    )
    def ping(message: str = "pong"):
        return {"namespace": MCP_NAMESPACE, "echo": message}

    @registrar.tool(
        "Spawn a cube prim under /World/Objects in the live stage.",
        {
            "name": {"type": "string", "default": "demo_cube", "description": "Prim name (path-safe)."},
            "size": {"type": "number", "default": 1.0, "description": "Edge length in meters."},
        },
    )
    def add_cube(name: str = "demo_cube", size: float = 1.0):
        # Kit imports live inside the handler: the handler runs on Kit's main
        # thread; the module itself stays importable anywhere.
        import omni.usd
        from pxr import UsdGeom

        stage = omni.usd.get_context().get_stage()
        for p in ("/World", "/World/Objects"):
            if not stage.GetPrimAtPath(p).IsValid():
                UsdGeom.Xform.Define(stage, p)
        path = f"/World/Objects/{name}"
        cube = UsdGeom.Cube.Define(stage, path)
        cube.GetSizeAttr().Set(size)
        return {"path": path, "size": size}
