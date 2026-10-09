import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from src.foundry_mcp import server


def _patched_client(call_return=None, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.call.side_effect = side_effect
    else:
        client.call.return_value = call_return
    return client


ENV = {
    "id": 550, "name": "Cluster", "transport": "ssh", "executor": "lsf",
    "hostname": "hpc.example.org", "username": "jdoe", "defWorkdir": None,
    "defPublishdir": None, "jobQueue": "short", "bashVariable": "x",
}


class TestDefaultExecutionDirectory:
    def test_declared_default_wins_as_is(self):
        env = {**ENV, "defWorkdir": "/project/lab/runs"}
        assert server._default_execution_directory(env, "My run") == "/project/lab/runs"

    def test_home_pattern_from_environment_login(self):
        assert server._default_execution_directory(ENV, "RNA seq 1") == "/home/jdoe/foundry-connect-runs/RNA_seq_1"

    @pytest.mark.parametrize("name", ["../../etc", "..", "a/../../b", ".hidden"])
    def test_run_name_cannot_escape_the_runs_folder(self, name):
        path = server._default_execution_directory(ENV, name)
        tail = path[len("/home/jdoe/foundry-connect-runs/"):]
        assert path.startswith("/home/jdoe/foundry-connect-runs/")
        assert "/" not in tail and tail not in ("", ".", "..") and not tail.startswith(".")

    def test_empty_run_name_becomes_run(self):
        assert server._default_execution_directory(ENV, "  ") == "/home/jdoe/foundry-connect-runs/run"

    @pytest.mark.parametrize("user", [None, "", "  ", "a/b", "two words"])
    def test_no_usable_login_means_no_default(self, user):
        assert server._default_execution_directory({**ENV, "username": user}, "r") is None


class TestCompactRunEnvironment:
    def test_keeps_only_the_fields_a_user_chooses_by(self):
        assert server._compact_run_environment(ENV) == {
            "id": 550, "name": "Cluster", "transport": "ssh", "executor": "lsf",
            "hostname": "hpc.example.org", "username": "jdoe",
            "defWorkdir": None, "defPublishdir": None,
        }


class TestProjects:
    def test_list_projects_reads_the_callers_projects(self):
        client = _patched_client({"total": 1, "data": [{"id": 7, "name": "P", "owner": {"username": "u"}, "own": 1, "createdAt": "x"}]})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.list_projects(name="P", limit=5))
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/project/", params={"type": "1", "take": 5, "name": "P"},
        )
        assert out["data"]["projects"] == [{"id": 7, "name": "P", "owner": {"username": "u"}}]

    def test_create_project_posts_a_private_project(self):
        client = _patched_client({"id": 9, "name": "New"})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_project("New", summary="s"))
        client.call.assert_called_once_with(
            method="POST", endpoint="/api/v1/project/", data={"name": "New", "summary": "s"},
        )
        assert out["data"]["project"]["id"] == 9

    def test_create_project_refuses_a_blank_name_without_calling(self):
        client = _patched_client({})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_project("  "))
        assert "error" in out
        client.call.assert_not_called()


class TestListPipelines:
    def test_lists_every_pipeline_the_caller_can_read(self):
        client = _patched_client({"total": 1, "data": [{"id": 3, "name": "RNA-seq", "summary": "s", "version": "2", "pipelineType": "x"}]})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.list_pipelines(search="rna", limit=10))
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/pipeline/",
            params={"type": "1,2,3,4", "take": 10, "searchKeyword": "rna"},
        )
        assert out["data"]["pipelines"][0]["id"] == 3

    def test_error_is_returned_not_raised(self):
        client = _patched_client(side_effect=RuntimeError("boom"))
        with patch.object(server, "get_client", return_value=client):
            assert json.loads(server.list_pipelines()) == {"error": "boom"}
