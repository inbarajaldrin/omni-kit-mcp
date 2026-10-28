# Isaac Lab


The bridge's only Kit dependency (`omni.kit.async_engine`) ships in Kit's core
extension set, so it runs in any Kit app — including Isaac Lab. Lab
*standalone scripts* (train/play) differ from the Isaac Sim app in three ways:
the script owns the Kit app (`AppLauncher` + Lab's own `isaaclab.python.*.kit`
experiences), so persistent registration doesn't apply — enable the bridge
per-script via `--kit_args`; the script owns the main loop, so bridge commands
execute only while the app is pumped (`env.step()` / `sim.step()`) — a script
blocked between steps stalls dispatch until the next pump; and configuration
is env-var only. Note the port: "one bridge, one port, *per Kit process*" —
a Lab standalone run is its own Kit process, so when it runs alongside the
installed app (which holds 9009) it needs its own port (9010 by convention).
See `examples/isaaclab_bridge_launch.py` for the template.
