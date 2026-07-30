"""
Tests that guard the dependency constraints the server depends on at import time.

Background: on 2026-07-30 the staging MCP container entered a crash loop with
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. Nothing in this repo
changed. `pyproject.toml` declared `mcp>=1.0.0` with no upper bound, PyPI published
mcp 2.0.0 (which removed `mcp.server.fastmcp` and renamed FastMCP to
`mcp.server.mcpserver.MCPServer`), and a routine image rebuild re-resolved the
dependency and picked it up.

These tests fail loudly if that upper bound is loosened or the environment drifts
past it, instead of the break surfacing as a crash-looping container.
"""

import importlib.metadata
from pathlib import Path

import pytest

# tomllib is stdlib on 3.11+; this package still declares requires-python >=3.9.
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9 / 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_dependencies():
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["dependencies"]


class TestMcpUpperBound:
    """The `mcp` SDK must stay on 1.x until server.py migrates to the 2.x API."""

    @pytest.mark.skipif(
        tomllib is None, reason="needs Python 3.11+ or tomli to parse pyproject.toml"
    )
    def test_pyproject_declares_an_upper_bound_on_mcp(self):
        """An unbounded `mcp` requirement is what caused the 2026-07-30 outage."""
        mcp_requirements = [
            dep for dep in _declared_dependencies() if dep.split(">=")[0].strip() == "mcp"
        ]
        assert mcp_requirements, "pyproject.toml no longer declares an `mcp` dependency"
        assert mcp_requirements == ["mcp>=1.0.0,<2"], (
            "The `mcp` dependency must keep its `<2` upper bound. mcp 2.0.0 removed "
            "`mcp.server.fastmcp`, which src/foundry_mcp/server.py imports at module "
            "scope. Lift the bound only together with the 2.x API migration."
        )

    def test_installed_mcp_is_still_1x(self):
        """Catch an environment that resolved past the bound."""
        version = importlib.metadata.version("mcp")
        major = int(version.split(".")[0])
        assert major == 1, (
            f"Installed mcp is {version}; server.py requires the 1.x module layout. "
            "Reinstall dependencies from pyproject.toml."
        )


class TestServerImportSurface:
    """The exact imports that server.py performs at module scope must resolve."""

    def test_fastmcp_import_path_resolves(self):
        """This is the import that crash-looped staging when mcp 2.0.0 landed."""
        try:
            from mcp.server.fastmcp import FastMCP  # noqa: F401
        except ModuleNotFoundError as exc:  # pragma: no cover - only on a bad resolve
            pytest.fail(
                f"`from mcp.server.fastmcp import FastMCP` failed ({exc}). This is the "
                "mcp 2.x layout; the `<2` pin in pyproject.toml is missing or ignored."
            )

    def test_transport_security_import_path_resolves(self):
        """server.py line 31; present in both 1.x and 2.x, asserted for completeness."""
        from mcp.server.transport_security import TransportSecuritySettings  # noqa: F401
