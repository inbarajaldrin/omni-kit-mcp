"""The doorway's own interpreter can run the doorway.

mcp 2 removed `mcp.server.fastmcp`, which `kit_mcp.server` imports, so the dependency
is bounded below 2; and the test extra must bring pytest, so the doorway's tests run
under the doorway's own interpreter rather than one where the server tests skip.
"""

import importlib
import os

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYPROJECT = os.path.join(_REPO_ROOT, "pyproject.toml")


def _load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 (requires-python >=3.10)
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            pytest.fail(
                "this interpreter has neither tomllib (3.11+) nor tomli; "
                "pyproject.toml cannot be parsed here"
            )
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _requirement(requirements, name):
    found = [Requirement(spec) for spec in requirements if Requirement(spec).name == name]
    assert len(found) == 1, f"expected one {name!r} requirement, found {found}"
    return found[0]


def test_server_imports_in_the_running_interpreter():
    server = importlib.import_module("kit_mcp.server")
    # The module under test is this checkout's, not an installed copy elsewhere.
    assert os.path.realpath(server.__file__).startswith(os.path.realpath(_REPO_ROOT) + os.sep)
    from mcp.server.fastmcp import FastMCP
    assert isinstance(server.mcp, FastMCP)


def test_mcp_requirement_excludes_the_fastmcp_rename():
    project = _load_toml(_PYPROJECT)["project"]
    mcp = _requirement(project["dependencies"], "mcp")
    assert "cli" in mcp.extras
    # mcp 2 renamed FastMCP to MCPServer; every 2.x must be outside the specifier.
    assert not mcp.specifier.contains(Version("2.0.0"), prereleases=True)
    assert not mcp.specifier.contains(Version("2.1.1"))
    assert mcp.specifier.contains(Version("1.0.0"))


def test_test_extra_carries_pytest():
    project = _load_toml(_PYPROJECT)["project"]
    test_extra = project.get("optional-dependencies", {}).get("test", [])
    assert _requirement(test_extra, "pytest")
