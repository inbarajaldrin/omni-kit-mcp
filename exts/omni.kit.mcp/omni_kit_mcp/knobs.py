"""Config knobs — each cross-context configuration value defined exactly once.

A Knob is a configuration value with one identity: an env var (the per-launch
override), carb settings keys (the persistent forms scripts/install.py
writes), and one precedence rule — env > settings. Readers (extension.py,
autoload.py) call the read methods; the writer (scripts/install.py) imports
the key names. The gateway (kit_mcp) shares the env-var NAME as a documented
cross-process contract, pinned by test rather than by import — env names are
interface, not code.

Dependency-free: carb is imported lazily inside read_settings() so the module
imports anywhere (installer, tests, lint).
"""

import os
from dataclasses import dataclass
from typing import Optional, Tuple

# The carb settings subtree all bridge knobs live under. install.py derives its
# user.config.json path (persistent -> exts -> omni.kit.mcp) from this.
SETTINGS_SUBTREE = "exts/omni.kit.mcp"


@dataclass(frozen=True)
class Knob:
    """One configuration value's complete identity."""

    env: str    # per-launch override (the cross-process public name)
    leaf: str   # settings leaf under SETTINGS_SUBTREE (what install.py writes)

    @property
    def settings_keys(self) -> Tuple[str, str]:
        """Persistent form first (user.config.json), bare form second (.kit files)."""
        return (f"/persistent/{SETTINGS_SUBTREE}/{self.leaf}",
                f"/{SETTINGS_SUBTREE}/{self.leaf}")

    def read_env(self) -> Optional[str]:
        value = os.getenv(self.env)
        return value if value else None

    def read_settings(self):
        """First non-empty carb setting among the keys; None outside Kit."""
        try:
            import carb
            settings = carb.settings.get_settings()
        except Exception:
            return None
        for key in self.settings_keys:
            try:
                value = settings.get(key)
            except Exception:
                continue
            if value:
                return value
        return None

    def read(self):
        """The one precedence rule: env wins, else settings, else None."""
        env = self.read_env()
        return env if env is not None else self.read_settings()


PORT = Knob(env="OMNI_KIT_MCP_PORT", leaf="autostartPort")
# Bind address for the bridge socket. Default (unset) binds localhost — the
# bridge serves run_python (remote code execution by design), so listening
# beyond loopback is an explicit, recorded per-box decision (e.g. a tailnet
# IP). Never default this to 0.0.0.0.
BIND = Knob(env="OMNI_KIT_MCP_BIND", leaf="autostartHost")
TOOL_MODULES = Knob(env="KIT_MCP_TOOL_MODULES", leaf="toolModules")
TOOL_PATHS = Knob(env="KIT_MCP_TOOL_PATHS", leaf="toolPaths")
