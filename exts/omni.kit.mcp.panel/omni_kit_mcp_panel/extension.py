"""The MCP Tools panel — a generic, project-agnostic control surface.

Renders the live bridge registry: one collapsible section per namespace in
owner-registration order — bridge builtins first (collapsed to one line),
project namespaces appending below in the order they were registered, so a
newly loaded project never inserts between existing sections. Each tool is a
two-column card — label + Run button, then
one row per parameter, widgets generated from the tool's JSON schema
(enum -> ComboBox, boolean -> CheckBox, integer/number -> Int/FloatField,
everything else -> StringField; array/object fields accept JSON text).

The iron rule: widgets never call project logic directly. Every Run button
goes through ``bridge.dispatch(canonical_name, params)`` — the exact entrance
a socket request takes — so a human click and an agent call are the same code
path by construction, and get the same envelopes, errors, and future policy.

Params whose schema carries ``"required": true`` gate the dispatch client-side:
an empty required field shows an inline message instead of a doomed call.

Styling: pure omni.ui (the panel runs in any Kit app). When the host app ships
``isaacsim.gui.components`` its native style dict is applied so the panel
matches the Isaac look; elsewhere it falls back to a minimal palette.

Nothing here is per-project or generated on disk: the panel renders *data*.
A new project's tools appear on Refresh the moment its package registers.
"""

import json
from functools import partial

import carb
import omni.ext
import omni.ui as ui
from omni.kit.async_engine import run_coroutine

from omni_kit_mcp import get_mcp_bridge

WINDOW_TITLE = "MCP Tools"
_RESULT_MAX_CHARS = 500
_LABEL_WIDTH = 150
_ROW_HEIGHT = 22

_COLOR_OK = 0xFF6FC172       # ABGR green
_COLOR_ERR = 0xFF5B5BE5      # ABGR red
_COLOR_MUTED = 0xFF9E9E9E    # grey
_COLOR_ACCENT = 0xFFDBA656   # Isaac-ish blue accent (ABGR)


def _panel_style():
    """Isaac's native style when available; a small fallback palette otherwise."""
    try:
        from isaacsim.gui.components.style import get_style
        return get_style()
    except ImportError:
        return {
            "CollapsableFrame": {
                "background_color": 0xFF343432, "secondary_color": 0xFF343432,
                "border_radius": 4, "padding": 6, "margin": 2,
            },
            "CollapsableFrame:hovered": {"secondary_color": 0xFF3A3A3A},
            "Button": {"border_radius": 3, "margin": 1},
            "Field": {"border_radius": 2},
        }


class _ParamField:
    """One schema-driven parameter row + how to read a value out of it."""

    def __init__(self, name: str, schema: dict):
        self.name = name
        self.schema = schema
        self.ptype = schema.get("type", "string")
        self.enum = schema.get("enum")
        self.required = bool(schema.get("required"))

        desc = schema.get("description", "")
        label = f"{name} *" if self.required else name
        with ui.HStack(height=_ROW_HEIGHT, spacing=8):
            ui.Label(label, width=_LABEL_WIDTH, tooltip=desc,
                     alignment=ui.Alignment.LEFT_CENTER,
                     style={"color": _COLOR_MUTED})
            default = schema.get("default")
            if self.enum:
                start = self.enum.index(default) if default in self.enum else 0
                self._widget = ui.ComboBox(start, *[str(v) for v in self.enum])
            elif self.ptype == "boolean":
                self._widget = ui.CheckBox(width=20)
                if isinstance(default, bool):
                    self._widget.model.set_value(default)
            elif self.ptype == "integer":
                self._widget = ui.IntField(height=_ROW_HEIGHT - 4)
                if isinstance(default, int):
                    self._widget.model.set_value(default)
            elif self.ptype == "number":
                self._widget = ui.FloatField(height=_ROW_HEIGHT - 4)
                if isinstance(default, (int, float)):
                    self._widget.model.set_value(float(default))
            else:  # string / array / object — text; JSON-parsed for the latter two
                self._widget = ui.StringField(height=_ROW_HEIGHT - 4)
                placeholder = desc if self.ptype == "string" else f"JSON {self.ptype}"
                try:
                    self._widget.model.set_value(str(default) if isinstance(default, str) else "")
                    self._widget.tooltip = placeholder
                except Exception:
                    pass

    def value(self):
        """Read the widget; returns (value, include) — empty strings are omitted
        so tool defaults apply instead of forcing ''."""
        if self.enum:
            idx = self._widget.model.get_item_value_model().get_value_as_int()
            return self.enum[idx], True
        if self.ptype == "boolean":
            return self._widget.model.get_value_as_bool(), True
        if self.ptype == "integer":
            return self._widget.model.get_value_as_int(), True
        if self.ptype == "number":
            return self._widget.model.get_value_as_float(), True
        text = self._widget.model.get_value_as_string()
        if not text:
            return None, False
        if self.ptype in ("array", "object"):
            return json.loads(text), True  # raises -> surfaced in the status label
        return text, True


class McpPanelExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        carb.log_info("[omni.kit.mcp.panel] startup")
        self._window = ui.Window(WINDOW_TITLE, width=480, height=760)
        self._fields = {}   # canonical tool name -> [_ParamField]
        self._status = {}   # canonical tool name -> ui.Label
        self._rebuild()

    def on_shutdown(self):
        carb.log_info("[omni.kit.mcp.panel] shutdown")
        if self._window:
            self._window.destroy()
            self._window = None
        self._fields = {}
        self._status = {}

    # ==================== rendering ====================

    def _sections(self, bridge):
        """(title, [(name, meta)], collapsed) per namespace — pure
        owner-registration order. Builtins register first (at bridge
        construction) so they sit at the top, collapsed to one line; every
        project namespace APPENDS below in the order it was registered —
        nothing ever inserts between existing sections."""
        tools = bridge.get_registered_tools()
        by_ns = {}
        for name, meta in sorted(tools.items()):
            by_ns.setdefault(meta.get("namespace"), []).append((name, meta))

        sections = []
        for owner in bridge.get_owners().values():  # insertion-ordered
            ns = owner["namespace"]
            if ns not in by_ns:
                continue
            if ns is None:
                sections.append(("bridge builtins", by_ns.pop(ns), True))
            else:
                sections.append((ns, by_ns.pop(ns), False))
        for ns, items in by_ns.items():  # anything unowned (shouldn't happen)
            sections.append((ns, items, False))
        return sections, len(tools)

    def _rebuild(self):
        self._fields = {}
        self._status = {}
        try:
            bridge = get_mcp_bridge()
            sections, total = self._sections(bridge)
        except Exception as e:
            bridge, sections, total = None, [], 0
            carb.log_warn(f"[omni.kit.mcp.panel] bridge unavailable: {e}")

        with self._window.frame:
            with ui.VStack(spacing=6, style=_panel_style()):
                with ui.HStack(height=30, spacing=8):
                    ui.Button("Refresh", width=90, height=26, clicked_fn=self._rebuild,
                              tooltip="Re-read the bridge registry")
                    ui.Label(
                        f"{total} tools · {max(len(sections) - 1, 0)} project namespace(s)"
                        if bridge else "",
                        alignment=ui.Alignment.LEFT_CENTER,
                        style={"color": _COLOR_MUTED})
                if bridge is None:
                    ui.Label("Bridge not initialized — is omni.kit.mcp enabled?",
                             style={"color": _COLOR_ERR})
                    return
                if not sections:
                    ui.Label("No tools registered yet — load a tool package, "
                             "then Refresh.", style={"color": _COLOR_MUTED})
                    return
                with ui.ScrollingFrame(
                        horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF):
                    with ui.VStack(spacing=6):
                        for title, items, collapsed in sections:
                            with ui.CollapsableFrame(
                                    title=f"{title}  ({len(items)})",
                                    collapsed=collapsed, height=0):
                                with ui.VStack(spacing=4):
                                    for name, meta in items:
                                        self._build_tool_card(name, meta)

    def _build_tool_card(self, name: str, meta: dict):
        local = name.rsplit(".", 1)[-1]
        desc = meta.get("description", "")
        with ui.VStack(spacing=2, height=0):
            with ui.HStack(height=26, spacing=8):
                ui.Label(local, width=_LABEL_WIDTH, tooltip=desc,
                         alignment=ui.Alignment.LEFT_CENTER,
                         style={"color": _COLOR_ACCENT})
                ui.Button("Run", width=64, height=24, tooltip=desc,
                          clicked_fn=partial(self._run_tool, name))
                self._status[name] = ui.Label(
                    "", alignment=ui.Alignment.LEFT_CENTER, word_wrap=True,
                    style={"color": _COLOR_MUTED})
            params = meta.get("parameters", {})
            self._fields[name] = (
                [_ParamField(p, s) for p, s in params.items()] if params else [])
            ui.Spacer(height=3)

    # ==================== dispatch (the iron rule) ====================

    def _run_tool(self, name: str):
        status = self._status.get(name)

        def show(text, color=_COLOR_MUTED):
            if status:
                status.text = text
                status.style = {"color": color}

        try:
            params = {}
            missing = []
            for f in self._fields.get(name, []):
                value, include = f.value()
                if include:
                    params[f.name] = value
                elif f.required:
                    missing.append(f.name)
            if missing:
                show(f"missing required: {', '.join(missing)}", _COLOR_ERR)
                return
        except Exception as e:  # e.g. bad JSON in an array/object field
            show(f"param error: {e}", _COLOR_ERR)
            return

        show("running…")
        run_coroutine(self._dispatch_and_show(name, params, show))

    async def _dispatch_and_show(self, name: str, params: dict, show):
        """Through the registry's single entrance — same path as an agent call."""
        try:
            response = await get_mcp_bridge().dispatch(name, params)
        except Exception as e:
            response = {"status": "error", "message": str(e)}
        ok = response.get("status") == "success"
        body = response.get("result") if ok else response.get("message", "error")
        text = body if isinstance(body, str) else json.dumps(body, default=repr)
        if len(text) > _RESULT_MAX_CHARS:
            text = text[:_RESULT_MAX_CHARS] + "…"
        show(("✓ " if ok else "✗ ") + text, _COLOR_OK if ok else _COLOR_ERR)
