# Writing tools

How a project's tools behave once registered. The short version of the contract is in the
[README](../README.md#adding-a-project-the-contract).

## Declared toolsets

A package with NO `register()` is
introspected instead: every public function (or exactly `__all__`) becomes a
tool. The typed parameter schema derives from the signature — type hints map
to JSON kinds, a default marks the parameter optional, pydantic-style models
advertise their `model_json_schema()` — and the description from the
docstring's first line. Sync and async functions alike (async handlers are
awaited on Kit's loop). Adding a public function to the library IS publishing
it. Programmatic form: `register_toolset(bridge, module)` (importable from
`omni_kit_mcp`). `status` reports a per-namespace `source_hash` of the served
code so a client can detect staleness; `reload_tools` re-derives the surface
after an edit.

## Handler contract

Handler contract: params arrive as kwargs; return a JSON-serializable payload
(`None` → `{}`) or raise (`ToolError` attaches diagnostics like captured
stdout). Sync or async. The bridge owns the envelope and the main-thread hop.

## Curated per-project agent views

Same binary, same socket, different env:

```json
"robot-demo-agent": {
  "command": "kit-mcp",
  "env": { "OMNI_KIT_MCP_PORT": "9009",
           "KIT_MCP_NAMESPACE": "robot_demo",
           "KIT_MCP_BUILTINS": "0" }
}
```

exposes only `robot_demo.*` — no `run_python` — for agents that should stay on the
stable API. `run_python` is the operator's escape hatch and how tools are
prototyped; registered tools are what agents rely on.

## Hot reload

Edit tool code, then (via any client): `reload_tools {"module": "myproj_tools"}`
→ the owner drains in-flight work, the module tree re-imports (stale bytecode
purged), tools re-register. Live handles that must survive (articulation
views, physics handles) belong in a submodule named exactly `_state` — that one
module is preserved across reloads (the name is exact, not a suffix: a tool
module merely ending in `_state`, e.g. `read_state`, reloads normally). Then
`refresh_tools` on the MCP front-end picks
up schema changes. No Kit restart.

**Windows don't reload themselves.** An `omni.ui.Window` built by the previous
module instance survives the reload with closures over the dead module's state
— the bridge tools are new but the on-screen buttons still drive old code.
Declare every window title the package builds in its `register()`:

```python
def register(registrar):
    registrar.window("My Project Panel")     # destroyed on reload/teardown
    ...
```

The bridge destroys declared windows when the owner unregisters (every reload)
and adopts any stale same-titled window at declaration — so reloads are
self-healing and the window-spawning tool just rebuilds fresh on its next
call. (Without the declaration, do it manually at the top of the spawning
tool: `w = ui.Workspace.get_window(title); w and (setattr(w, "visible", False), w.destroy())`.)

## Remote files (put_file / get_file / stat_file)

`run_python` executes on the **bridge's box**: imports and USD paths resolve
against *its* disk, never the caller's. When the driver is remote (e.g. a Mac
driving a GPU box), ship code and assets through the bridge instead of a
side-channel rsync/scp: `put_file {"path", "content_b64", "mkdirs"}` writes a
file on the box (pushing a `.py` purges the sibling `__pycache__` so the next
import can't serve stale bytecode); `get_file {"path"}` reads one back;
`stat_file {"path"}` reports existence/size/sha256 so a driver **ensures**
instead of blindly pushing — skip unchanged files, and verify assets that
arrived over any bulk channel. Content rides one NDJSON frame in memory —
right for tool code and tool-sized assets; ship multi-GB scenes over a mount
or rsync and still verify them with `stat_file`. After pushing a tool
package's files, `reload_tools` picks them up without a restart.
`examples/remote_driver.py` is the copyable template for the whole pattern
(ensure → reload → dispatch → pull results).

## Control panel

`omni.kit.mcp.panel` renders the live registry: one section per namespace,
parameter widgets generated from each tool's schema. Every button goes through
`bridge.dispatch(...)` — the same entrance a socket request takes — so a human
click and an agent call are the same code path by construction. Projects that
want bespoke UI ship a thin widgets-only extension obeying the same rule.

## Tool-author gotchas (Kit facts that cost real debugging time)

- **`prim.SetActive(False)` does not stop an OmniGraph.** A loaded graph keeps
  evaluating every tick with its prim deactivated (observed live: a
  ROS2SubscribeJointState→ArticulationController graph kept stomping
  articulation targets, making native commands appear dead). A tool that
  "toggles" a graph must `stage.RemovePrim(graph_path)` and rebuild to
  disable — never rely on SetActive.
- **Never pump Kit's loop from a handler — `await` the frame instead.** A tool
  handler runs as a coroutine ON Kit's main event loop (the bridge hops it there
  via `run_coroutine` — USD/PhysX are main-thread only). A *synchronous* pump
  from inside it re-enters the running loop, and asyncio forbids that: you get a
  hard `RuntimeError: Cannot enter into task <UI task> while another task
  <McpBridge._execute_and_reply> is being executed`, cascading over every pending
  UI coroutine — under a live viewport (webrtc) it can take the process down. The
  culprits are `omni.kit.app.get_app().update()`, `world.render()`, and
  `world.step(render=True)` (which calls `app.update()` internally). The supported
  pattern: make the handler `async def` and **await the app frame**, which yields
  control back to the loop (no re-entry) AND — while the timeline is playing —
  advances real sim time:
    - advance one frame: `await omni.kit.app.get_app().next_update_async()`
      (steps physics one dt when playing; this is THE async primitive)
    - settle then read at-rest state:
      ```python
      if not world.is_playing():
          await world.play_async()
      for _ in range(n):
          await omni.kit.app.get_app().next_update_async()
      vel = art.get_joint_velocities()   # real: sim time actually advanced
      ```
  Do NOT reach for `world.step_async` — Isaac's `World.step_async` is a broken
  override (sync `def`, takes `step_size` not `render`, runs only pre-step hooks,
  returns `None`; its docstring's `await world.step_async()` is misleading). And
  sync `world.step(render=False)` doesn't crash but freezes sim time inside the
  blocked call — `next_update_async` is the honest fix for both.
- **`time.sleep` inside `run_python` blocks Kit's main loop** — the sim can't
  advance while the handler sleeps. Anything that must span sim time
  (settling, motion, measurements) belongs in an `async` handler awaiting the
  steps above (or, for a sync handler, across two bridge calls).
- **Capturing a viewport headless: fire, then await frames — never await the
  capture on the main thread.** `capture_viewport_to_file` awaited directly
  deadlocks (it waits for a frame the blocked loop can't produce). Fire it, then
  `await omni.kit.app.get_app().next_update_async()` in a loop until the output
  file exists — the async yield lets the frame the write needs actually render.
- **Name tool packages uniquely.** Every co-loaded project shares one
  interpreter; `import scene_tools` resolves to whichever package of that name
  got there first, silently shadowing yours.
