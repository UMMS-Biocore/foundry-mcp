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


DETAILS = {
    "name": "r1", "permission": 3, "groupId": None,
    "runEnvironment": {"selectedId": None, "availableList": [ENV]},
    "inputs": [], "processOptions": {"grp": {"a": 1}},
}


class TestCreateRun:
    def test_creates_then_returns_the_form(self):
        client = _patched_client(side_effect=[{"id": 41, "projectPipelineId": 41}, DETAILS])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_run(pipeline_id=3, project_id=7, name="r1"))
        assert client.call.call_args_list[0].kwargs == {
            "method": "POST", "endpoint": "/api/v1/run/new-run-attempt",
            "data": {"pipelineId": 3, "projectId": 7, "name": "r1"},
        }
        assert client.call.call_args_list[1].kwargs == {"method": "GET", "endpoint": "/api/v1/run/41/details"}
        data = out["data"]
        assert data["run_id"] == 41
        assert data["run_environments"][0]["id"] == 550
        assert "bashVariable" not in data["run_environments"][0]
        assert data["proposed_execution_directory"] == {"550": "/home/jdoe/foundry-connect-runs/r1"}
        assert data["process_options"] == {"grp": {"a": 1}}

    def test_works_with_a_server_that_only_returns_projectPipelineId(self):
        client = _patched_client(side_effect=[{"projectPipelineId": 42}, DETAILS])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_run(pipeline_id=3, project_id=7, name="r1"))
        assert out["data"]["run_id"] == 42

    def test_no_usable_environment_is_said_plainly(self):
        empty = {**DETAILS, "runEnvironment": {"selectedId": None, "availableList": []}}
        client = _patched_client(side_effect=[{"id": 43}, empty])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_run(pipeline_id=3, project_id=7, name="r1"))
        assert "no run environment" in out["summary"].lower()

    def test_refusal_from_the_server_is_passed_through(self):
        client = _patched_client(side_effect=RuntimeError("No permission to read this project"))
        with patch.object(server, "get_client", return_value=client):
            assert json.loads(server.create_run(3, 7, "r1")) == {"error": "No permission to read this project"}


class TestListRunEnvironments:
    def test_reads_the_runs_details(self):
        client = _patched_client(DETAILS)
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.list_run_environments("41"))
        client.call.assert_called_once_with(method="GET", endpoint="/api/v1/run/41/details")
        assert out["data"]["run_environments"][0]["username"] == "jdoe"
