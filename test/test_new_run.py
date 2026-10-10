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

    def test_create_project_echoes_the_name_when_the_server_returns_only_the_id(self):
        client = _patched_client({"id": 9})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_project("  New  "))
        assert out["data"]["project"] == {"id": 9, "name": "New"}
        assert "New" in out["summary"] and "None" not in out["summary"]

    def test_create_project_refuses_a_blank_name_without_calling(self):
        client = _patched_client({})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_project("  "))
        assert "error" in out
        client.call.assert_not_called()


class TestListPipelines:
    def test_queries_each_view_type_and_merges(self):
        # The server ANDs a multi value type filter, so each view is asked alone.
        pages = {
            "1": {"total": 2, "data": [{"id": 3, "name": "RNA-seq", "summary": "s", "version": "2"}, {"id": 5, "name": "ATAC"}]},
            "2": {"total": 1, "data": [{"id": 5, "name": "ATAC"}]},
            "3": {"total": 1, "data": [{"id": 8, "name": "Shared"}]},
        }
        client = _patched_client(side_effect=lambda **kw: pages[kw["params"]["type"]])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.list_pipelines(search="rna", limit=10))
        assert [c.kwargs for c in client.call.call_args_list] == [
            {"method": "GET", "endpoint": "/api/v1/pipeline/", "params": {"type": t, "take": 10, "searchKeyword": "rna"}}
            for t in ("1", "2", "3")
        ]
        assert [p["id"] for p in out["data"]["pipelines"]] == [3, 5, 8]
        assert out["data"]["pipelines"][0] == {"id": 3, "name": "RNA-seq", "version": "2", "summary": "s"}

    def test_never_sends_a_multi_value_or_unknown_type(self):
        client = _patched_client({"total": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_pipelines()
        sent = [c.kwargs["params"]["type"] for c in client.call.call_args_list]
        assert sent == ["1", "2", "3"]

    def test_merged_list_is_capped_at_the_limit(self):
        page = {"total": 3, "data": [{"id": i} for i in (1, 2, 3)]}
        other = {"total": 2, "data": [{"id": i} for i in (4, 5)]}
        client = _patched_client(side_effect=[page, other, other])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.list_pipelines(limit=4))
        assert [p["id"] for p in out["data"]["pipelines"]] == [1, 2, 3, 4]

    def test_error_is_returned_not_raised(self):
        client = _patched_client(side_effect=RuntimeError("boom"))
        with patch.object(server, "get_client", return_value=client):
            assert json.loads(server.list_pipelines()) == {"error": "boom"}


PARAMS = {
    "pipelineType": "via",
    "inputs": {
        "pipe": {
            "schema": [
                {"varName": "params.min_len", "type": "input", "defaultVal": "20", "optional": True, "desc": "Minimum length"},
                {"varName": "run_cleanup", "type": "checkbox", "defaultVal": "true"},
            ],
            "inputNodes": [
                {"varName": "params.reads", "qualifier": "file", "inDescOpt": "Paired reads"},
                {"varName": "params.genome", "qualifier": "val", "defVal": "hg38", "dropDown": "hg38,mm10"},
            ],
        },
        "processes": {
            "3": {"schema": [{"varName": "params.min_len", "type": "input", "defaultVal": "99"}]},
        },
    },
}


DETAILS = {
    "name": "r1", "permission": 3, "groupId": None,
    "runEnvironment": {"selectedId": None, "availableList": [ENV]},
    "inputs": [], "processOptions": {"grp": {"a": 1}},
}


class TestCreateRun:
    def test_creates_then_returns_the_form(self):
        client = _patched_client(side_effect=[{"id": 41, "projectPipelineId": 41}, DETAILS, PARAMS])
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
        client = _patched_client(side_effect=[{"projectPipelineId": 42}, DETAILS, PARAMS])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_run(pipeline_id=3, project_id=7, name="r1"))
        assert out["data"]["run_id"] == 42

    def test_no_usable_environment_is_said_plainly(self):
        empty = {**DETAILS, "runEnvironment": {"selectedId": None, "availableList": []}}
        client = _patched_client(side_effect=[{"id": 43}, empty, PARAMS])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_run(pipeline_id=3, project_id=7, name="r1"))
        assert "no run environment" in out["summary"].lower()

    def test_refusal_from_the_server_is_passed_through(self):
        client = _patched_client(side_effect=RuntimeError("No permission to read this project"))
        with patch.object(server, "get_client", return_value=client):
            assert json.loads(server.create_run(3, 7, "r1")) == {"error": "No permission to read this project"}


class TestListRunEnvironments:
    def test_reads_the_runs_details(self):
        client = _patched_client(side_effect=[DETAILS, PARAMS])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.list_run_environments("41"))
        assert [c.kwargs for c in client.call.call_args_list] == [
            {"method": "GET", "endpoint": "/api/v1/run/41/details"},
            {"method": "GET", "endpoint": "/api/v1/run/41/parameter-list"},
        ]
        assert out["data"]["run_environments"][0]["username"] == "jdoe"
        assert [d["name"] for d in out["data"]["input_definitions"]] == ["reads", "genome", "min_len"]


class TestUpdateRunEnvironment:
    def test_sends_only_the_fields_given(self):
        client = _patched_client({"ok": True})
        with patch.object(server, "get_client", return_value=client):
            server.update_run("41", permission=3, run_environment_id=550, launch_directory="/home/jdoe/foundry-connect-runs/r1")
        client.call.assert_called_once_with(
            method="PATCH", endpoint="/api/v1/run/41/save",
            data={"permission": 3, "groupId": None, "runEnvironmentId": 550,
                  "launchDirectory": "/home/jdoe/foundry-connect-runs/r1"},
        )

    def test_existing_full_call_is_unchanged(self):
        client = _patched_client({"ok": True})
        with patch.object(server, "get_client", return_value=client):
            server.update_run("41", [{"name": "a", "value": "b"}], {"g": {}}, 3)
        client.call.assert_called_once_with(
            method="PATCH", endpoint="/api/v1/run/41/save",
            data={"inputs": [{"name": "a", "value": "b", "type": "input"}], "processOptions": {"g": {}}, "permission": 3, "groupId": None},
        )

    @staticmethod
    def _details_client(details):
        client = MagicMock()
        client.call.side_effect = lambda **kw: details if kw["endpoint"].endswith("/details") else ""
        return client

    def test_permission_defaults_to_the_saved_one(self):
        client = self._details_client({"permission": 3, "groupId": None})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_run("41", run_environment_id=550))
        assert "error" not in out
        assert client.call.call_args_list[-1].kwargs == {
            "method": "PATCH", "endpoint": "/api/v1/run/41/save",
            "data": {"permission": 3, "groupId": None, "runEnvironmentId": 550},
        }

    def test_a_group_shared_run_keeps_its_saved_group(self):
        client = self._details_client({"permission": 15, "groupId": 7})
        with patch.object(server, "get_client", return_value=client):
            server.update_run("41", name="r2")
        data = client.call.call_args_list[-1].kwargs["data"]
        assert (data["permission"], data["groupId"]) == (15, 7)

    def test_a_run_with_no_saved_permission_is_refused(self):
        client = self._details_client({})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_run("41", name="r2"))
        assert "permission" in out["error"]
        assert not any(c.kwargs["method"] == "PATCH" for c in client.call.call_args_list)

    def test_a_given_permission_skips_the_lookup(self):
        client = self._details_client({"permission": 63})
        with patch.object(server, "get_client", return_value=client):
            server.update_run("41", permission=3, name="r2")
        assert [c.kwargs["method"] for c in client.call.call_args_list] == ["PATCH"]

    def test_relative_launch_directory_is_refused(self):
        client = _patched_client({})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_run("41", permission=3, launch_directory="runs/r1"))
        assert "absolute" in out["error"]
        client.call.assert_not_called()


NEW_TOOLS = ["list_projects", "create_project", "list_pipelines", "create_run", "list_run_environments"]


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_is_registered_with_a_description(name):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert name in tools
    assert tools[name].description and tools[name].description.strip()


class TestReviewFixes:
    def test_dot_logins_give_no_default(self):
        for login in (".", "..", ".hidden"):
            assert server._default_execution_directory({**ENV, "username": login}, "r") is None

    def test_form_inputs_round_trip_collection_fields(self):
        raw = [{"id": 5, "name": "reads", "type": "vmetaCollection", "value": "set A",
                "collectionId": None, "vmetaCollectionId": "abc"}]
        form = server._run_form({**DETAILS, "inputs": raw}, "r1")
        assert form["inputs"] == raw

    def test_form_keeps_the_external_pipeline_dict_shape(self):
        raw = {"input": {"value": "/data/s.csv", "type": "input"}, "outdir": {"value": "x", "type": "input"}}
        form = server._run_form({**DETAILS, "inputs": raw}, "r1")
        assert form["inputs"] == raw


class TestInputDefinitions:
    def test_foundry_pipeline_lists_main_inputs_with_type_required_and_default(self):
        defs = {d["name"]: d for d in server._input_definitions(PARAMS)}
        assert set(defs) == {"reads", "genome", "min_len"}
        assert defs["reads"] == {
            "name": "reads", "type": "file", "required": True, "default": None,
            "description": "Paired reads", "options": None,
        }
        assert defs["genome"]["type"] == "val" and defs["genome"]["default"] == "hg38"
        assert defs["genome"]["options"] == ["hg38", "mm10"] and defs["genome"]["required"] is True
        assert defs["min_len"]["required"] is False and defs["min_len"]["default"] == "20"

    def test_required_inputs_come_first(self):
        names = [d["name"] for d in server._input_definitions(PARAMS)]
        assert names == ["reads", "genome", "min_len"]

    def test_non_params_options_are_not_inputs(self):
        assert "run_cleanup" not in [d["name"] for d in server._input_definitions(PARAMS)]

    def test_schema_flags_decide_the_type(self):
        params = {"inputs": {"pipe": {"schema": [
            {"varName": "params.a", "type": "input", "file": True},
            {"varName": "params.b", "type": "input", "singleFile": True},
            {"varName": "params.c", "type": "number"},
            {"varName": "params.d", "type": "dropdown", "opt": ["x", "y"]},
        ], "inputNodes": []}, "processes": {}}}
        defs = {d["name"]: d for d in server._input_definitions(params)}
        assert [defs[k]["type"] for k in "abcd"] == ["file", "singleFile", "number", "dropdown"]
        assert defs["d"]["options"] == ["x", "y"]

    def test_external_pipeline_keeps_the_section(self):
        params = {"pipelineType": "nfcore", "inputs": {
            "input_output_options": {"schema": [
                {"varName": "params.input_output_options.input", "type": "input", "singleFile": True, "optional": None, "desc": "Samplesheet"},
                {"varName": "params.input_output_options.save_all", "type": "checkbox", "defaultVal": False, "optional": True},
            ], "inputNodes": []},
        }}
        defs = server._input_definitions(params)
        assert defs[0] == {
            "name": "input", "section": "input_output_options", "type": "singleFile", "required": True,
            "default": None, "description": "Samplesheet", "options": None,
        }
        assert defs[1]["required"] is False and defs[1]["type"] == "checkbox"

    def test_unknown_shape_gives_no_definitions(self):
        assert server._input_definitions({}) == []
        assert server._input_definitions({"inputs": None}) == []

    def test_create_run_form_carries_the_definitions(self):
        client = _patched_client(side_effect=[{"id": 41}, DETAILS, PARAMS])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_run(pipeline_id=3, project_id=7, name="r1"))
        assert client.call.call_args_list[2].kwargs == {"method": "GET", "endpoint": "/api/v1/run/41/parameter-list"}
        assert out["data"]["input_definitions"][0]["name"] == "reads"
        assert out["data"]["inputs"] == []

    def test_a_failed_parameter_list_still_returns_the_form(self):
        client = _patched_client(side_effect=[{"id": 41}, DETAILS, RuntimeError("boom")])
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.create_run(pipeline_id=3, project_id=7, name="r1"))
        assert out["data"]["run_id"] == 41
        assert out["data"]["input_definitions"] is None
        assert "boom" in out["data"]["input_definitions_error"]


class TestUpdateRunInputType:
    """#30 (a): the form's definition types are not the saved input types."""

    def _sent_inputs(self, inputs):
        client = _patched_client({"ok": True})
        with patch.object(server, "get_client", return_value=client):
            server.update_run("41", inputs, permission=3)
        return client.call.call_args.kwargs["data"]["inputs"]

    def test_a_plain_value_without_a_type_is_saved_as_input(self):
        assert self._sent_inputs([{"name": "mate", "value": "single"}]) == [
            {"name": "mate", "type": "input", "value": "single"}]

    @pytest.mark.parametrize("form_type", ["dropdown", "val", "file", "number"])
    def test_a_definition_type_from_the_form_becomes_input(self, form_type):
        sent = self._sent_inputs([{"name": "x", "type": form_type, "value": "v"}])
        assert sent[0]["type"] == "input"

    def test_collection_ids_decide_the_type(self):
        sent = self._sent_inputs([
            {"name": "reads", "vmetaCollectionId": "abc"},
            {"name": "bam_files", "collectionId": 9},
        ])
        assert [i["type"] for i in sent] == ["vmetaCollection", "collection"]

    @pytest.mark.parametrize("saved_type", ["input", "collection", "vmetaCollection"])
    def test_a_saved_type_is_kept(self, saved_type):
        sent = self._sent_inputs([{"name": "x", "type": saved_type, "value": "v", "collectionId": 1, "vmetaCollectionId": "a"}])
        assert sent[0]["type"] == saved_type

    def test_the_callers_list_is_not_changed(self):
        inputs = [{"name": "mate", "value": "single"}]
        self._sent_inputs(inputs)
        assert inputs == [{"name": "mate", "value": "single"}]

    def test_external_pipeline_dict_is_sent_as_is(self):
        inputs = {"main": {"input": "/data/sheet.csv"}}
        assert self._sent_inputs(inputs) == inputs


class TestUpdateRunReply:
    """#30 (b): a save the server answers with an empty body still says what was saved."""

    @pytest.mark.parametrize("reply", ["", None, {}])
    def test_an_empty_server_reply_echoes_the_saved_fields(self, reply):
        client = _patched_client(reply)
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_run(
                "41", [{"name": "mate", "value": "single"}], permission=3,
                run_environment_id=550, launch_directory="/home/jdoe/foundry-connect-runs/r1"))
        assert "41" in out["summary"]
        assert out["data"]["run_id"] == "41"
        assert out["data"]["saved"] == {
            "inputs": ["mate"], "permission": 3, "runEnvironmentId": 550,
            "launchDirectory": "/home/jdoe/foundry-connect-runs/r1",
        }
        assert "preflight_run" in " ".join(out["next_steps"])

    def test_a_server_reply_with_content_is_passed_through(self):
        client = _patched_client({"ok": True})
        with patch.object(server, "get_client", return_value=client):
            assert json.loads(server.update_run("41", permission=3)) == {"ok": True}
