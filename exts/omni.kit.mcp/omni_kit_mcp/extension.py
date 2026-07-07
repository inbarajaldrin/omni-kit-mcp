"""IExt lifecycle for omni.kit.mcp — the bridge owns the socket, always.

One bridge, one port, per Kit process. on_startup: build the singleton, load
the configured tool packages (autoload.py), bind the socket. Consumers never
call start()/stop() — projects are plain tool packages registered persistently
via ``scripts/install.py add-project`` (or per-launch via KIT_MCP_TOOL_* env).

Port resolution follows knobs.PORT and the bind address knobs.BIND (env
override, else persistent settings — see knobs.py, the single home of every
knob's identity; BIND defaults to localhost because the bridge serves
run_python). If the configured port is already taken — a second instance of
the same app — the bridge falls back to an OS-assigned ephemeral port and
advertises it via its runtime portfile, so discovery still finds it.

No port configured => the socket stays down (logged); tool modules requested
with no port is a hard error, since registering tools nobody can reach is
always a misconfig.
"""

import carb
import omni.ext

from . import knobs
from .autoload import has_autoload_request, load_tool_modules
from .bridge import _create_bridge, _destroy_bridge


def _resolve_port() -> int:
    """Configured bridge port (knobs.PORT precedence), or 0 if unset."""
    env = knobs.PORT.read_env()
    if env:
        try:
            return int(env)
        except ValueError:
            raise RuntimeError(f"{knobs.PORT.env} is not an integer: {env!r}")
    value = knobs.PORT.read_settings()
    try:
        return int(value) if value else 0
    except (TypeError, ValueError):
        return 0


class McpBridgeExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        # carb.log_*, not print(): Kit routes print() into its log sink under a
        # generic [py stdout] channel; carb.log_* stays greppable per-channel.
        carb.log_info("[omni.kit.mcp] startup")
        bridge = _create_bridge()

        results = load_tool_modules(bridge) if has_autoload_request() else {}

        port = _resolve_port()
        bind_host = knobs.BIND.read() or "localhost"
        if port:
            try:
                bridge.start(port, host=bind_host)
            except OSError:
                # Another Kit process already holds the configured port (e.g. a
                # second instance of the same app). Fall back to an ephemeral
                # port — the portfile advertises it, so discovery still finds us.
                carb.log_warn(
                    f"[omni.kit.mcp] port {port} is taken (another instance?) — "
                    f"binding an ephemeral port instead; clients discover it via "
                    f"the runtime portfile or --list-bridges.")
                bridge.start(0, host=bind_host)
            carb.log_info(
                f"[omni.kit.mcp] bridge on port {bridge._port}; tool modules: {results or 'none'}")
        elif results:
            raise RuntimeError(
                "omni.kit.mcp: tool modules are configured but no port is set. "
                "Set OMNI_KIT_MCP_PORT or the autostartPort setting "
                "(scripts/install.py writes it).")
        else:
            carb.log_warn(
                "[omni.kit.mcp] no port configured — socket not started. "
                "Run scripts/install.py or set OMNI_KIT_MCP_PORT.")

    def on_shutdown(self):
        carb.log_info("[omni.kit.mcp] shutdown")
        _destroy_bridge()
