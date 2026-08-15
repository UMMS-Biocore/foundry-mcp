"""Registration checks for the process git and script asset tools.

These assert what the server actually exposes, not what the module defines.

The distinction is the point. A decorated function that fails to register is
still a valid, importable, testable Python function, so nothing else in this
suite would notice its absence. The equivalent defect shipped once on the
backend: a request handler was written, typechecked and merged without its
route, and every test passed because an exported function nothing calls is
perfectly valid code. Asking the server for its tool list is the only check
that catches it.
"""

import asyncio

import pytest

from foundry_mcp import server


PROCESS_GIT_TOOLS = [
    "connect_process_repository",
    "export_process_revision",
    "list_process_script_assets",
    "put_process_script_asset",
    "delete_process_script_asset",
]


def _tools():
    return asyncio.run(server.mcp.list_tools())


@pytest.mark.parametrize("name", PROCESS_GIT_TOOLS)
def test_tool_is_registered(name):
    assert name in {tool.name for tool in _tools()}


@pytest.mark.parametrize("name", PROCESS_GIT_TOOLS)
def test_tool_has_a_description(name):
    # The description is the only thing a model sees when choosing a tool, so an
    # undocumented one is registered but effectively unusable.
    tool = next(t for t in _tools() if t.name == name)
    assert tool.description and tool.description.strip()


def test_tool_names_are_unique():
    # A duplicate name silently replaces the earlier tool rather than erroring.
    names = [tool.name for tool in _tools()]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "name,required",
    [
        ("connect_process_repository", ["process_id", "repo_url"]),
        ("export_process_revision", ["process_id"]),
        ("list_process_script_assets", ["process_id"]),
        ("put_process_script_asset", ["process_id", "path", "contents"]),
        ("delete_process_script_asset", ["process_id", "path"]),
    ],
)
def test_tool_requires_its_arguments(name, required):
    # A parameter that silently gained a default would let a caller omit it and
    # get a confusing server side error instead of a clear client side one.
    tool = next(t for t in _tools() if t.name == name)
    assert set(required) <= set(tool.inputSchema.get("required", []))


def test_mutating_tools_warn_that_they_change_the_process():
    # These reset the revision to draft and invalidate evidence. A model choosing
    # between them and a read only tool has to be able to tell from the text.
    for name in ["put_process_script_asset", "delete_process_script_asset", "connect_process_repository"]:
        tool = next(t for t in _tools() if t.name == name)
        assert "WARNING" in tool.description
