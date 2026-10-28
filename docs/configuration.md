# Configuration

## Register while the app is down

Apply registrations while the app is **down**: a running app rewrites
`user.config.json` from its in-memory settings during its graceful-exit flush,
silently discarding edits made underneath it. (Corollary: a hard `kill -9`
skips that flush, so a registration written while the app ran survives a hard
kill — but don't lean on it; install-while-down is the reliable path.)

## Knobs

| Knob | Persistent (carb setting under `/persistent/exts/omni.kit.mcp/`) | Per-launch env override |
|---|---|---|
| Bridge port | `autostartPort` | `OMNI_KIT_MCP_PORT` |
| Bind address | `autostartHost` | `OMNI_KIT_MCP_BIND` |
| Tool packages | `toolModules` | `KIT_MCP_TOOL_MODULES` |
| Package paths | `toolPaths` | `KIT_MCP_TOOL_PATHS` |
| Queue cap (s a request may state) | `queueCapSeconds` | `OMNI_KIT_MCP_QUEUE_CAP_S` (default 900) |
| Queue limit (waiting requests) | `queueLimit` | `OMNI_KIT_MCP_QUEUE_LIMIT` (default 64) |

**Remote access.** The bridge binds `localhost` by default — it serves
`run_python` (remote code execution by design), so listening beyond loopback
is an explicit per-box decision. Options: an SSH tunnel (zero config), or set
the bind address to a private/tailnet IP (`install.py --bind-host <ip>`;
never `0.0.0.0` on a public host). Remote callers can't read the box's
portfiles, so the `list_bridges` builtin exists: ask any reachable bridge
(typically the stable installed-app port) and it reports every bridge on its
box.

Front-end (`kit-mcp`) env: `OMNI_KIT_MCP_PORT` (optional — auto-discovered
when exactly one bridge runs; set it to pin a process), `OMNI_KIT_MCP_HOST`,
`KIT_MCP_NAMESPACE` (comma allowlist), `KIT_MCP_BUILTINS` (`0` hides
`run_python`/`reload_tools`), `KIT_MCP_NAME` / `KIT_MCP_INSTRUCTIONS`.

## Multiple instances on one box

Ports are per-launch configuration, not global state: give each Kit process
its own `OMNI_KIT_MCP_PORT` (a persistent install writes one default; the env
var overrides it per launch). A configured port that is already taken — a second
instance of the same app on the same port — refuses at startup with the reason
(`EADDRINUSE`) rather than moving somewhere no client could predict, so give each
instance its own port. Every instance writes its own `{pid}-{port}.json`
portfile in the runtime dir, so
`--list-bridges` shows them all, and no-port discovery errors loudly (naming
the candidates) rather than guessing when several run.

A client confirms it reached the instance it **intended** with the `status`
builtin: pid, port, bind host, headless-vs-headful, app + version, uptime,
and the registered namespaces. Same-box discovery reads the same identity
from the portfile; a remote client — which cannot see another machine's
portfiles — asks the socket itself. Headless (`--no-window`) and headful
instances expose the identical tool surface.
