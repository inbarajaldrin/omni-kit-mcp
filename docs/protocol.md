# Wire protocol, admission and hold

What the bridge speaks on its socket, and the rules it admits requests by. For scripts the
[reference client](../README.md#scripting-the-bridge-no-mcp-needed) speaks all of this for you.

## Framing (NDJSON over TCP)

One JSON object per `\n`-terminated line, each direction:

```
→ {"type": "robot_demo.spawn_robot", "params": {"name": "robot_1"}}
← {"status": "success", "result": {"path": "/World/robot_1"}}
← {"status": "error", "message": "...", "traceback": "...", ...}
→ {"type": "list_tools", "params": {}}
← {"status": "success", "result": {"tools": {"robot_demo.spawn_robot": {"description": ..., "parameters": ..., "namespace": "robot_demo", "owner_id": ...}}}}
```

A malformed line gets one error response; the connection stays healthy. Many
clients may hold connections concurrently; each is isolated. Tool execution is
serialized on Kit's main thread (USD/PhysX are not thread-safe).

## Endpoint resolution

You usually don't specify one. Each running bridge
advertises itself in a per-user runtime dir (`$XDG_RUNTIME_DIR/omni-kit-mcp/`).
A client resolves its port as: explicit `port=`/`--port` → `OMNI_KIT_MCP_PORT`
→ **auto-discovered when exactly one bridge is running** → a loud error listing
candidates when several are (e.g. the app on 9009 *and* a Lab run on 9010 — then
name one). The host rides along: explicit `host=`/`--host` → `OMNI_KIT_MCP_HOST`
→ **the discovered portfile's advertised host** — so a bridge bound to a
tailnet IP is dialed on that IP, not loopback. The single-process common case
is zero-config; only genuinely ambiguous multi-process boxes ever need a port.
(Agents via the gateway are even simpler — the gateway resolves once at
startup and they just call tools.)

## Request admission

A tool that advances the simulator from inside its own call (`app.update()`
under the bridge's task) leaves no room for any other request: Kit steps its
asyncio loop from the update tick, so a request started beside it is refused
entry and destroyed, unanswered. The bridge therefore admits requests through
one queue:

- **Order.** One FIFO queue by arrival; only its head is ever considered. An
  ordinary request starts when no exclusive request is running; an exclusive
  one starts when nothing is. Neither class jumps the other.
- **Exclusive tools.** A handler function carrying
  `__omni_kit_mcp_exclusive__ = True` gets the main loop to itself.
  `run_python` always does. `kit_mcp.marks` derives, from a package's syntax
  tree, which of its handlers must carry the mark; hold your roster to it in
  a test.
- **Queue budget.** A request states `"queue_s"`: the longest it may wait to
  *start*. With none stated it starts at once or is refused at once. The
  reference client states it for you: its timeout, less an execution reserve
  (`execution_reserve_s`, default half the timeout), less one second. The
  budget bounds the wait, not the tool: nothing interrupts a running tool.
- **One request per connection.** There are no request ids, so a second frame
  sent before the first is answered is refused, after that answer. An
  optional top-level `"id"` is echoed on the reply.
- **A caller that has gone is never served.** A queued request whose
  connection closed is removed and does not run.

Typed refusals, as `"refusal"` on the error envelope (`"refused": true`):

| refusal | meaning |
|---|---|
| `DOORWAY_BUSY` | did not run: could not start inside its queue budget, or the queue is full. Names what is running and for how long. The bridge is alive. |
| `DOORWAY_PIPELINED` | a second frame on a connection still owed an answer |
| `DOORWAY_ADMISSION_UNDECLARED` | the tool's package declares no admission contract (`DOORWAY_ADMISSION = 1` at module level) |
| `DOORWAY_REQUEST_CANCELLED` / `DOORWAY_REQUEST_LOST` / `DOORWAY_SCHEDULER_FAILED` | admitted, and its task was cancelled, destroyed before it ran, or could not be scheduled |
| `DOORWAY_HELD` | did not run: another caller holds the doorway (below). Names the holder and its reason. |
| `DOORWAY_HOLD_REFUSED` | a hold or release that cannot be granted: asked in-process (a hold belongs to a connection), a token shorter than 32 characters, no holder or reason, a hold already standing, or a release without the standing token |
| `DOORWAY_HOLD_TOKEN_UNREADABLE` | (client side) a declared token file exists but is empty or unreadable |

`status` reports `admission`: the contract, each tool module's declaration,
what is running and waiting, counts, and `unmarked_pumps` — tools the bridge
caught advancing the loop without the mark. In-process callers (a panel, a
host extension) use `bridge.submit_local(name, params, on_reply, queue_s=)`,
never `run_coroutine(bridge.dispatch(...))`.

## Holding the doorway

One caller can reserve the doorway for a piece of work, for example a
benchmark episode that must not be disturbed. The holder's connection sends
`{"type": "doorway.hold", "params": {"token", "holder", "reason"}}` and keeps
the connection open. From then on every frame whose top-level `"hold"` field
is not that token is refused `DOORWAY_HELD` without running; `status` stays
answerable. The hold ends when its connection closes, whether by
`doorway.release`, the holder exiting, or the holder being killed, so a
crashed holder never strands the doorway. In-process callers carry no token
and are refused while a hold stands.

The reference client wraps this as `kit_mcp.client.DoorwayHold`:

```python
with DoorwayHold("my-benchmark", "episode 3", port=9010) as hold:
    env = {**os.environ, HOLD_TOKEN_ENV: hold.token}   # let the work's own processes through
    ...
```

A process started before the hold, whose environment cannot change, can name a
token file with `OMNI_KIT_MCP_HOLD_TOKEN_FILE`; the client reads it on every
call.

## Boot refusal and caller liveness

A configured tool module that fails to import stops the app at startup rather
than letting the doorway come up without it: the bridge raises
`ToolModuleLoadRefusal` naming every failed module with its traceback, serves
none of them, writes a `boot-refusal-<pid>-<ns>.json` record to the log
directory for a launcher to read, and asks Kit to quit (exit code 78).

A long-running tool can call `omni_kit_mcp.bridge.request_caller_disconnected()`
to learn that its caller has gone and stop early.

## Call telemetry (optional)

Set `OMNI_KIT_MCP_AUTHORING_TELEMETRY` to a file path and every completed tool
call appends one fsynced JSON line: tool name, timing, ordinal, outcome and a
digest of the arguments. Payloads are never written. Unset, telemetry costs
nothing.
