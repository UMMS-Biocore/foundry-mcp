"""Tests for the git import tools and the pipeline git tools.

Registration is asserted against the server's tool list (see
test_process_git_tools.py for why), and each tool is held to the exact request
it sends, since these are thin wrappers whose only job is the request shape.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from src.foundry_mcp import server


GIT_TOOLS = {
    "check_process_import": ["repo_url"],
    "import_process_from_repository": [
        "repo_url", "repo_path", "revision", "expected_commit_id", "menu_group_id", "confirm_import",
    ],
    "connect_pipeline_repository": ["pipeline_id", "repo_url", "repo_path"],
    "get_pipeline_repository": ["pipeline_id"],
    "export_pipeline_revision": ["pipeline_id"],
    "check_pipeline_import": ["repo_url", "slug"],
    "import_pipeline_from_repository": ["repo_url", "slug", "rev", "menu_group_id", "pipeline_group_id"],
    "list_pipeline_menu_groups": [],
}

REPO = "https://github.com/example/pipelines"
COMMIT = "a" * 40


def _tools():
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


@pytest.mark.parametrize("name", sorted(GIT_TOOLS))
def test_tool_is_registered_with_a_description(name):
    tool = _tools().get(name)
    assert tool is not None
    assert tool.description and tool.description.strip()


@pytest.mark.parametrize("name,required", sorted(GIT_TOOLS.items()))
def test_tool_requires_its_arguments(name, required):
    assert set(required) <= set(_tools()[name].inputSchema.get("required", []))


@pytest.mark.parametrize(
    "name",
    ["import_process_from_repository", "connect_pipeline_repository", "export_pipeline_revision",
     "import_pipeline_from_repository"],
)
def test_writing_tools_warn(name):
    assert "WARNING" in _tools()[name].description


def _client(result=None, error=None):
    client = MagicMock()
    if error is not None:
        client.call.side_effect = error
    else:
        client.call.return_value = result
    return client


def _run(tool, client, **kwargs):
    with patch.object(server, "get_client", return_value=client):
        return json.loads(tool(**kwargs))


class TestProcessImport:
    def test_check_enumerates_when_no_path_is_given(self):
        client = _client({"processes": []})
        _run(server.check_process_import, client, repo_url=REPO)
        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/process/git/import/check",
            data={"repoUrl": REPO, "provider": "github"},
        )

    def test_check_sends_path_revision_and_login(self):
        client = _client({"revision": {}})
        _run(server.check_process_import, client, repo_url=REPO, repo_path="trim", revision=3,
             service_token_id=9, provider="gitlab")
        assert client.call.call_args.kwargs["data"] == {
            "repoUrl": REPO, "provider": "gitlab", "repoPath": "trim", "revision": 3, "serviceTokenId": 9,
        }

    def test_import_sends_the_reviewed_commit_and_consent(self):
        client = _client({"processId": 41, "processGid": 40, "name": "trim"})
        out = _run(server.import_process_from_repository, client, repo_url=REPO, repo_path="trim",
                   revision=3, expected_commit_id=COMMIT, menu_group_id=2, confirm_import=True)
        assert out["processId"] == 41
        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/process/git/import",
            data={
                "repoUrl": REPO, "provider": "github", "repoPath": "trim", "revision": 3,
                "expectedCommitId": COMMIT, "menuGroupId": 2, "confirmImport": True,
            },
        )

    @pytest.mark.parametrize("confirm", [False, None, "true", 1])
    def test_import_without_consent_is_refused_before_any_request(self, confirm):
        client = _client({})
        with pytest.raises(ToolError, match="confirm_import must be true"):
            _run(server.import_process_from_repository, client, repo_url=REPO, repo_path="trim",
                 revision=3, expected_commit_id=COMMIT, menu_group_id=2, confirm_import=confirm)
        client.call.assert_not_called()


class TestPipelineGit:
    def test_connect_always_sends_the_login_so_omitting_it_clears_it(self):
        client = _client({"repoUrl": REPO})
        _run(server.connect_pipeline_repository, client, pipeline_id=7, repo_url=REPO, repo_path="rnaseq")
        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/pipeline/7/git/connect",
            data={"repoUrl": REPO, "repoPath": "rnaseq", "provider": "github", "defaultBranch": "main",
                  "serviceTokenId": None},
        )

    def test_get_connection(self):
        client = _client(None)
        assert _run(server.get_pipeline_repository, client, pipeline_id=7) is None
        client.call.assert_called_once_with(method="GET", endpoint="/api/v1/pipeline/7/git/connection")

    def test_export(self):
        client = _client({"tag": "foundry/pipeline/rnaseq/rev-1", "committed": True})
        _run(server.export_pipeline_revision, client, pipeline_id=7)
        client.call.assert_called_once_with(method="POST", endpoint="/api/v1/pipeline/7/git/export", data={})

    def test_check_import_defaults_to_the_highest_revision(self):
        client = _client({"canApply": True})
        _run(server.check_pipeline_import, client, repo_url=REPO, slug="rnaseq")
        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/pipeline/git/import/check",
            data={"repoUrl": REPO, "slug": "rnaseq", "rev": None, "serviceTokenId": None},
        )

    def test_import_pins_the_reviewed_revision(self):
        client = _client({"pipelineId": 12, "created": True, "processIds": [3, 4]})
        _run(server.import_pipeline_from_repository, client, repo_url=REPO, slug="rnaseq", rev=2,
             menu_group_id=5, pipeline_group_id=6, service_token_id=9)
        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/pipeline/git/import",
            data={"repoUrl": REPO, "slug": "rnaseq", "rev": 2, "serviceTokenId": 9, "menuGroupId": 5,
                  "pipelineGroupId": 6},
        )

    def test_list_pipeline_menu_groups(self):
        client = _client({"data": [{"id": 6, "name": "RNA"}]})
        assert _run(server.list_pipeline_menu_groups, client)["data"][0]["id"] == 6
        client.call.assert_called_once_with(method="GET", endpoint="/api/v1/menu-group/pipeline")


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        (server.export_pipeline_revision, {"pipeline_id": 7}),
        (server.check_pipeline_import, {"repo_url": REPO, "slug": "rnaseq"}),
        (server.check_process_import, {"repo_url": REPO}),
    ],
)
def test_a_refusal_is_raised_not_returned(tool, kwargs):
    # A refused export or import must carry isError, never read as a success.
    client = _client(error=RuntimeError("Error 302: Bad Request: no such folder"))
    with pytest.raises(ToolError, match="no such folder"):
        _run(tool, client, **kwargs)
