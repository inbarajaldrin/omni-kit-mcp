#!/usr/bin/env python3
"""omni-kit-mcp installer — permanent, zero-launch-flag registration.

Edits the Kit app's persistent user.config.json (the file the Extensions
window's AUTOLOAD checkbox writes) so the bridge — and each project's tool
package — loads on EVERY launch with no --ext-folder/--enable flags and no
env vars. Idempotent; backs the config up first; --remove undoes exactly what
it added. Stdlib only.

  install (default)   register this repo's exts folder, auto-enable
                      omni.kit.mcp + omni.kit.mcp.panel, set the autostart port
  add-project         persistently register one tool package:
                        install.py add-project --path ~/myproj/isaac --module myproj_tools
  remove-project      unregister one tool package by module name
  status              print what is currently registered
  install --remove    undo the base registration

Settings written (all under /persistent in user.config.json):
  app/exts/userFolders            ext search paths (this repo's exts/, project dirs)
  app/exts/enabled                auto-enabled ext ids (exact versioned ids)
  exts/omni.kit.mcp/autostartPort bridge bind port (read by the ext at startup)
  exts/omni.kit.mcp/autostartHost bridge bind address (default localhost; see --bind-host)
  exts/omni.kit.mcp/toolModules   tool packages to autoload (owner per module)
  exts/omni.kit.mcp/toolPaths     sys.path additions so those modules import

  python3 scripts/install.py                      # base install (dry-run: add --dry-run)
  python3 scripts/install.py add-project --path ~/myproj/isaac --module myproj_tools
  python3 scripts/install.py status
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import time

HOME = os.path.expanduser("~")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FOLDER = os.path.join(REPO_ROOT, "exts")
DEFAULT_EXTS = ["omni.kit.mcp", "omni.kit.mcp.panel"]
# Deliberately uncommon default port: not IANA-listed, not a famous dev default
# (9000 php-fpm, 8080, 3000, 8888...), non-privileged. Override with --autostart-port.
DEFAULT_AUTOSTART_PORT = 9009

# Knob identities (settings leaf names, subtree) come from the same table the
# extension reads — writer and readers cannot drift. Requires the repo layout
# around this script (it registers REPO_ROOT/exts anyway).
sys.path.insert(0, os.path.join(REPO_ROOT, "exts", "omni.kit.mcp"))
from omni_kit_mcp.knobs import BIND, PORT, SETTINGS_SUBTREE, TOOL_MODULES, TOOL_PATHS  # noqa: E402

MCP_SETTINGS_KEY = SETTINGS_SUBTREE.split("/", 1)[1]  # subtree under persistent/exts/


def find_config(explicit):
    if explicit:
        return explicit
    # Prefer the "Isaac-Sim Full" app (normal/GUI launches), newest version;
    # fall back to any Kit app data dir.
    for pattern in (f"{HOME}/.local/share/ov/data/Kit/Isaac-Sim Full/*/user.config.json",
                    f"{HOME}/.local/share/ov/data/Kit/Isaac-Sim*/*/user.config.json",
                    f"{HOME}/.local/share/ov/data/Kit/*/*/user.config.json"):
        cands = glob.glob(pattern)
        if cands:
            return max(cands, key=os.path.getmtime)
    sys.exit("No Kit user.config.json found. Launch the app once, or pass --config.")


def _idx_dict_values(d):
    """user.config serializes lists as {'0': v, '1': v}. Return the value list."""
    if isinstance(d, list):
        return list(d)
    if not isinstance(d, dict):
        return []
    return [d[k] for k in sorted(d, key=lambda x: int(x) if x.isdigit() else x)]


def _to_idx_dict(values):
    return {str(i): v for i, v in enumerate(values)}


def _ext_version(ext_name, folders):
    """Read the ext's [package] version from its extension.toml (first
    standalone `version = "..."` line), or None."""
    for fol in folders:
        toml = os.path.join(fol, ext_name, "config", "extension.toml")
        if os.path.isfile(toml):
            with open(toml) as f:
                for line in f:
                    m = re.match(r'\s*version\s*=\s*"([^"]+)"', line)
                    if m:
                        return m.group(1)
    return None


class Config:
    """user.config.json with the list-as-index-dict quirk handled."""

    def __init__(self, path):
        self.path = path
        with open(path) as f:
            self.cfg = json.load(f)
        self.changes = []
        p = self.cfg.setdefault("persistent", {})
        self._app_exts = p.setdefault("app", {}).setdefault("exts", {})
        self._exts_tree = p.setdefault("exts", {})

    # -- app/exts lists --
    def get_list(self, key):
        return _idx_dict_values(self._app_exts.get(key, {}))

    def set_list(self, key, values):
        self._app_exts[key] = _to_idx_dict(values)

    # -- /persistent/exts/omni.kit.mcp/* --
    def mcp_settings(self):
        return self._exts_tree.setdefault(MCP_SETTINGS_KEY, {})

    def mcp_get_list(self, key):
        return _idx_dict_values(self.mcp_settings().get(key, {}))

    def mcp_set_list(self, key, values):
        s = self.mcp_settings()
        if values:
            s[key] = _to_idx_dict(values)
        else:
            s.pop(key, None)
        if not s:
            self._exts_tree.pop(MCP_SETTINGS_KEY, None)

    def write(self, dry_run):
        if not self.changes:
            print("Already in desired state — nothing to do.")
            return
        print("Changes:")
        for c in self.changes:
            print("  " + c)
        if dry_run:
            print("(dry-run — wrote nothing)")
            return
        backup = f"{self.path}.bak.{int(time.time())}"
        shutil.copy2(self.path, backup)
        print(f"backup: {backup}")
        with open(self.path, "w") as f:
            json.dump(self.cfg, f, indent=4)
        print("WROTE config. Restart the Kit app — changes apply on next launch.")
        print("NOTE: if the app is RUNNING right now, it will overwrite this file")
        print("with its in-memory settings on exit — restart it promptly, or")
        print("re-run this command after closing it.")


# ==================== commands ====================

def cmd_install(cfg: Config, args):
    folders = args.folder or [DEFAULT_FOLDER]
    exts = args.enable or DEFAULT_EXTS
    user_folders = cfg.get_list("userFolders")
    enabled = cfg.get_list("enabled")

    if args.remove:
        for fol in folders:
            if fol in user_folders:
                user_folders.remove(fol); cfg.changes.append(f"- userFolders: {fol}")
        for e in exts:
            for v in [x for x in enabled if x == e or x.startswith(e + "-")]:
                enabled.remove(v); cfg.changes.append(f"- enabled: {v}")
        s = cfg.mcp_settings()
        for key in (PORT.leaf, BIND.leaf, TOOL_MODULES.leaf, TOOL_PATHS.leaf):
            if key in s:
                del s[key]; cfg.changes.append(f"- /persistent/{SETTINGS_SUBTREE}/{key}")
        if not s:
            cfg._exts_tree.pop(MCP_SETTINGS_KEY, None)
    else:
        for fol in folders:
            if not os.path.isdir(fol):
                print(f"  WARN: folder does not exist: {fol}")
            if fol not in user_folders:
                user_folders.append(fol); cfg.changes.append(f"+ userFolders: {fol}")
        for e in exts:
            # Exact versioned id (e.g. omni.kit.mcp-0.2.0) — a bare name makes
            # Kit log "no longer available". Re-run after a bump self-corrects.
            ver = _ext_version(e, folders)
            ext_id = f"{e}-{ver}" if ver else e
            variants = [x for x in enabled if x == e or x.startswith(e + "-")]
            if variants != [ext_id]:
                enabled = [x for x in enabled if x not in variants]
                enabled.append(ext_id)
                stale = [v for v in variants if v != ext_id]
                cfg.changes.append(f"+ enabled: {ext_id}"
                                   + (f" (removed stale: {stale})" if stale else ""))
        if args.autostart_port:
            s = cfg.mcp_settings()
            if s.get(PORT.leaf) != args.autostart_port:
                s[PORT.leaf] = args.autostart_port
                cfg.changes.append(
                    f"+ /persistent/{SETTINGS_SUBTREE}/{PORT.leaf} = {args.autostart_port}")
        if args.bind_host:
            s = cfg.mcp_settings()
            if s.get(BIND.leaf) != args.bind_host:
                s[BIND.leaf] = args.bind_host
                cfg.changes.append(
                    f"+ /persistent/{SETTINGS_SUBTREE}/{BIND.leaf} = {args.bind_host}")

    cfg.set_list("userFolders", user_folders)
    cfg.set_list("enabled", enabled)


def cmd_add_project(cfg: Config, args):
    path = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.isdir(path):
        print(f"  WARN: path does not exist: {path}")
    if not os.path.isdir(os.path.join(path, args.module)) and \
       not os.path.isfile(os.path.join(path, args.module + ".py")):
        print(f"  WARN: {path} does not contain a module named {args.module!r}")

    paths = cfg.mcp_get_list(TOOL_PATHS.leaf)
    modules = cfg.mcp_get_list(TOOL_MODULES.leaf)
    if path not in paths:
        paths.append(path); cfg.changes.append(f"+ toolPaths: {path}")
    if args.module not in modules:
        modules.append(args.module); cfg.changes.append(f"+ toolModules: {args.module}")
    cfg.mcp_set_list(TOOL_PATHS.leaf, paths)
    cfg.mcp_set_list(TOOL_MODULES.leaf, modules)


def cmd_remove_project(cfg: Config, args):
    paths = cfg.mcp_get_list(TOOL_PATHS.leaf)
    modules = cfg.mcp_get_list(TOOL_MODULES.leaf)
    if args.module in modules:
        modules.remove(args.module); cfg.changes.append(f"- toolModules: {args.module}")
    if args.path:
        path = os.path.abspath(os.path.expanduser(args.path))
        if path in paths:
            paths.remove(path); cfg.changes.append(f"- toolPaths: {path}")
    cfg.mcp_set_list(TOOL_PATHS.leaf, paths)
    cfg.mcp_set_list(TOOL_MODULES.leaf, modules)


def cmd_status(cfg: Config, args):
    print(f"userFolders : {cfg.get_list('userFolders')}")
    print(f"enabled     : {cfg.get_list('enabled')}")
    s = dict(cfg.mcp_settings())
    print(f"autostartPort: {s.get(PORT.leaf)}")
    print(f"toolModules : {cfg.mcp_get_list('toolModules')}")
    print(f"toolPaths   : {cfg.mcp_get_list('toolPaths')}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="install",
                    choices=["install", "add-project", "remove-project", "status"])
    ap.add_argument("--config", help="Path to user.config.json (default: auto-detect).")
    ap.add_argument("--folder", action="append",
                    help=f"Ext search folder(s) (default: {DEFAULT_FOLDER}).")
    ap.add_argument("--enable", action="append",
                    help=f"Ext name(s) to auto-enable (default: {DEFAULT_EXTS}).")
    ap.add_argument("--autostart-port", type=int, default=DEFAULT_AUTOSTART_PORT,
                    help="Bridge bind port (0 to skip).")
    ap.add_argument("--bind-host", default=None,
                    help="Bridge bind address (default localhost). SECURITY: the "
                         "bridge serves run_python — bind a tailnet/LAN IP only on "
                         "boxes/networks you trust; never 0.0.0.0 on a public host.")
    ap.add_argument("--path", help="add/remove-project: dir containing the tool package.")
    ap.add_argument("--module", help="add/remove-project: tool package module name.")
    ap.add_argument("--remove", action="store_true",
                    help="install: undo exactly what this script adds.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print intended changes; write nothing.")
    args = ap.parse_args()

    cfg = Config(find_config(args.config))
    print(f"config: {cfg.path}")

    if args.command == "install":
        cmd_install(cfg, args)
    elif args.command == "add-project":
        if not (args.path and args.module):
            sys.exit("add-project requires --path and --module")
        cmd_add_project(cfg, args)
    elif args.command == "remove-project":
        if not args.module:
            sys.exit("remove-project requires --module")
        cmd_remove_project(cfg, args)
    elif args.command == "status":
        cmd_status(cfg, args)
        return

    cfg.write(args.dry_run)


if __name__ == "__main__":
    main()
