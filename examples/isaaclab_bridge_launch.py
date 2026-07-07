# isaaclab_bridge_launch.py — template: wire the omni.kit.mcp bridge into an
# Isaac Lab STANDALONE script (train/play/custom env loop).
#
# Isaac Lab differs from the Isaac Sim app in three ways that change the setup:
#   1. The script owns the Kit app (AppLauncher + Lab's isaaclab.python.*.kit
#      experience), so scripts/install.py's persistent registration does NOT
#      apply — enable the bridge per-script via --kit_args, as below.
#   2. The script owns the main loop: bridge commands execute only while the
#      app is pumped (env.step()/sim.step()/app.update()). A request sent while
#      the script blocks between steps stalls until the next pump. Keep
#      stepping (training loops naturally do).
#   3. Configuration is env-var only (no carb persistence): OMNI_KIT_MCP_PORT,
#      KIT_MCP_TOOL_PATHS, KIT_MCP_TOOL_MODULES.
#
# Everything else — protocol, tool packages, canonical names, the kit-mcp
# front-end — is identical to the Isaac Sim setup (see repo README).

import argparse
import os

# Bridge config must be in the environment BEFORE the app launches.
# A Lab standalone run is its OWN Kit process — "one bridge, one port, per Kit
# process" means each concurrently-running process needs its own port. The
# installed Isaac Sim app typically holds 9009; Lab runs take 9010 by convention.
os.environ.setdefault("OMNI_KIT_MCP_PORT", "9010")
# Optional: autoload your project's tool package(s), same contract as Isaac Sim:
# os.environ.setdefault("KIT_MCP_TOOL_PATHS", os.path.expanduser("~/myproj/isaac"))
# os.environ.setdefault("KIT_MCP_TOOL_MODULES", "myproj_tools")

_OMNI_KIT_MCP_EXTS = os.path.expanduser("~/Documents/omni-kit-mcp/exts")

from isaaclab.app import AppLauncher

parser = argparse.ArgumentParser(description="My Lab script, bridge-enabled.")
# ... your own arguments here ...
AppLauncher.add_app_launcher_args(parser)
args = parser.parse_args()

# Enable the bridge at boot. If the user passed their own --kit_args, append.
# Kit args use the SPACE form ("--enable omni.kit.mcp"); the "=" form is
# silently ignored for --enable. To also livestream to a remote viewer, add:
#   LIVESTREAM=2 in env, and "--/app/livestream/publicEndpointAddress=<box-ip>"
#   to kit_args — the PUBLIC_IP env var alone does NOT set the endpoint, and an
#   empty endpoint yields an SDP with no ICE candidates (track connects, no
#   frames ever arrive).
_bridge_args = f"--ext-folder {_OMNI_KIT_MCP_EXTS} --enable omni.kit.mcp"
args.kit_args = f"{args.kit_args} {_bridge_args}".strip() if args.kit_args else _bridge_args

app_launcher = AppLauncher(args)
simulation_app = app_launcher.app
# Bridge is now listening on OMNI_KIT_MCP_PORT (run_python + your tools).

# ---- your normal Isaac Lab code below; imports must come after app launch ----
import isaaclab.sim as sim_utils

sim = sim_utils.SimulationContext(sim_utils.SimulationCfg(dt=1 / 120))
sim.reset()

while simulation_app.is_running():
    sim.step()   # each step pumps the app — bridge requests dispatch here

simulation_app.close()
