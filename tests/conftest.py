"""Make the repo's python packages importable from a bare checkout."""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXT_ROOT = os.path.join(_REPO_ROOT, "exts", "omni.kit.mcp")
for _p in (_REPO_ROOT, _EXT_ROOT):   # kit_mcp · omni_kit_mcp
    if _p not in sys.path:
        sys.path.insert(0, _p)


import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_dir(tmp_path, monkeypatch):
    """Keep bridge portfiles out of the real XDG_RUNTIME_DIR during tests.
    Tests that need a specific dir override this via their own monkeypatch."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
