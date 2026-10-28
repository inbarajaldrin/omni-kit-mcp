"""IExt lifecycle for omni.kit.mcp — the bridge owns the socket, always.

One bridge, one port, per Kit process. on_startup: build the singleton, arm
the run_python session staleness guard (stage-open -> clear), load the
configured tool packages (autoload.py) — every one, or the doorway refuses to
boot (autoload.ToolModuleLoadRefusal) — bind the socket. Consumers never
call start()/stop() — projects are plain tool packages registered persistently
via ``scripts/install.py add-project`` (or per-launch via KIT_MCP_TOOL_* env).

Port resolution follows knobs.PORT and the bind address knobs.BIND (env
override, else persistent settings — see knobs.py, the single home of every
knob's identity; BIND defaults to localhost because the bridge serves
run_python).

A DECLARED bind that fails REFUSES, naming the address and the errno's own
meaning (bind.py). It never falls back to another port: a bridge that moved
would be one no client can find.

No port configured => the socket stays down (logged); tool modules requested
with no port is a hard error, since registering tools nobody can reach is
always a misconfig.
"""

import carb
import omni.ext

from . import knobs
from .bind import declared_bind_refusal
from .autoload import (ToolModuleLoadRefusal, has_autoload_request,
                       load_configured_tool_modules, refuse_boot)
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
        self._stage_sub = _guard_sessions_against_stage_open(bridge)

        try:
            results = load_configured_tool_modules(bridge) if has_autoload_request() else {}
        except ToolModuleLoadRefusal as refusal:
            # NO SKIP. The socket below is never bound: a doorway that came up without a
            # configured namespace would answer 'unknown tool' long after the cause.
            # The record + quit are what turn this into a boot failure for whoever is waiting,
            # instead of a readiness deadline with no cause in it.
            self._stage_sub = None
            _destroy_bridge()
            refuse_boot(refusal)
            raise

        port = _resolve_port()
        bind_host = knobs.BIND.read() or "localhost"
        if port:
            try:
                bridge.start(port, host=bind_host)
            except OSError as error:
                # NO FALLBACK. The address was DECLARED and a client is going to dial
                # exactly it; a bridge that quietly moved would be a bridge nobody can
                # find. The errno decides what the log says, because "the address does
                # not exist" and "the port is taken" send a reader to different places.
                refusal = declared_bind_refusal(bind_host, port, error)
                carb.log_error(f"[omni.kit.mcp] {refusal}")
                raise refusal from error
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
        self._stage_sub = None   # release the stage-event subscription
        _destroy_bridge()


def _guard_sessions_against_stage_open(bridge):
    """Subscribe: stage-open -> drop persistent run_python sessions.

    Saved session vars are live in-process objects (prim references, physics
    handles); a new stage kills what they point at, so serving them after a
    stage-open would hand out dead handles. Returns the subscription (keep a
    reference or it unsubscribes), or None in a Kit app without omni.usd —
    no stages there means nothing to go stale."""
    try:
        import omni.usd
        stream = omni.usd.get_context().get_stage_event_stream()
        opened = int(omni.usd.StageEventType.OPENED)
        return stream.create_subscription_to_pop_by_type(
            opened, lambda _e: bridge.clear_sessions(),
            name="omni.kit.mcp sessions stage-open guard")
    except ImportError:
        carb.log_warn(
            "[omni.kit.mcp] omni.usd unavailable — persistent run_python "
            "sessions will NOT auto-clear on stage open")
        return None
