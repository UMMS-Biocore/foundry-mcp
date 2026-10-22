"""Tests for get_run_advanced_settings and update_run_advanced_settings.

DETAILS_12366 is run 12366's advancedSettings as the server returned them on
2026-10-22: Singularity off, settings for all processes on queue gpu, 8 GB,
1 CPU, 30 minutes. The save shapes were checked live on run 12388, a
duplicate of 12366.
"""
import asyncio
import copy
import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from src.foundry_mcp import server

DETAILS_12366 = {
    "name": "GPU smoke check",
    "permission": 63,
    "groupId": None,
    "advancedSettings": {
        "docker": {"enabled": False, "image": "quay.io/viascientific/pipeline_base_image:1.0", "options": ""},
        "singularity": {"enabled": False, "image": "", "options": ""},
        "globalExec": {"enabled": True, "options": {
            "job_queue": "gpu", "job_memory": "8", "job_cpu": "1", "job_time": "30", "job_clu_opt": ""}},
        "processExec": {"enabled": False, "options": {}},
    },
}

WITH_OVERRIDE = copy.deepcopy(DETAILS_12366)
WITH_OVERRIDE["advancedSettings"]["processExec"] = {"enabled": True, "options": {
    "procGnum-1": {"check": "on", "queue": "gpu", "memory": "12", "cpu": "2", "time": "45", "opt": ""}}}

WITH_SINGULARITY = copy.deepcopy(DETAILS_12366)
WITH_SINGULARITY["advancedSettings"]["singularity"] = {
    "enabled": True, "image": "docker://quay.io/viascientific/rnaseq:4.0", "options": "-B /share:/share"}


def _client(details=DETAILS_12366, status="NotSubmitted"):
    client = MagicMock()

    def call(method, endpoint, data=None, **kwargs):
        if endpoint.endswith("/details"):
            return copy.deepcopy(details)
        if endpoint.endswith("/status"):
            return {"runStatus": status}
        if endpoint.endswith("/save"):
            return ""
        raise AssertionError(f"unexpected {method} {endpoint}")

    client.call.side_effect = call
    return client


def _run(tool, client, **kwargs):
    with patch.object(server, "get_client", return_value=client):
        return json.loads(tool(**kwargs))


def _saves(client):
    return [c.kwargs["data"] for c in client.call.call_args_list if c.kwargs.get("method") == "PATCH"]


def _update(client, **kwargs):
    return _run(server.update_run_advanced_settings, client, run_id="12388", confirm_update=True, **kwargs)


class TestGetRunAdvancedSettings:
    def test_reads_12366(self):
        out = _run(server.get_run_advanced_settings, _client(status="Completed"), run_id="12366")
        data = out["data"]
        assert data["container"]["type"] == "off"
        assert data["container"]["singularity"]["enabled"] is False
        assert data["exec_all"] == {"enabled": True, "queue": "gpu", "memory": "8", "cpu": "1",
                                    "time": "30", "options": ""}
        assert data["process_exec"] == {"enabled": False, "processes": {}}
        assert data["status"] == "Completed"
        assert data["editable"] is False
        assert "duplicate_run" in out["next_steps"][0]

    def test_reads_per_process_overrides_by_their_field_names(self):
        out = _run(server.get_run_advanced_settings, _client(details=WITH_OVERRIDE), run_id="12388")
        assert out["data"]["process_exec"]["processes"]["procGnum-1"] == {
            "queue": "gpu", "memory": "12", "cpu": "2", "time": "45", "options": ""}
        assert out["data"]["editable"] is True

    def test_reads_the_container_in_use(self):
        out = _run(server.get_run_advanced_settings, _client(details=WITH_SINGULARITY), run_id="12388")
        assert out["data"]["container"]["type"] == "singularity"

    def test_writes_nothing(self):
        client = _client()
        _run(server.get_run_advanced_settings, client, run_id="12388")
        assert _saves(client) == []

    def test_an_unreadable_status_is_an_error(self):
        client = _client()
        client.call.side_effect = [copy.deepcopy(DETAILS_12366), RuntimeError("502")]
        with pytest.raises(ToolError, match="502"):
            _run(server.get_run_advanced_settings, client, run_id="12388")


class TestConsent:
    @pytest.mark.parametrize("confirm", [False, None])
    def test_without_consent_is_refused_before_any_request(self, confirm):
        client = _client()
        with pytest.raises(ToolError, match="confirm_update must be true"):
            _run(server.update_run_advanced_settings, client, run_id="12388", confirm_update=confirm,
                 container="off")
        client.call.assert_not_called()

    @pytest.mark.parametrize("confirm", ["true", "yes", "1", 1])
    def test_a_client_cannot_consent_with_a_string_or_number(self, confirm):
        """Through the MCP layer, where lax validation would turn these into True."""
        client = _client()
        with patch.object(server, "get_client", return_value=client), pytest.raises(Exception):
            asyncio.run(server.mcp.call_tool("update_run_advanced_settings", dict(
                run_id="12388", confirm_update=confirm, container="off")))
        assert _saves(client) == []

    @pytest.mark.parametrize("enabled", ["false", 0])
    def test_a_switch_must_be_a_real_bool(self, enabled):
        client = _client()
        with patch.object(server, "get_client", return_value=client), pytest.raises(Exception):
            asyncio.run(server.mcp.call_tool("update_run_advanced_settings", dict(
                run_id="12388", confirm_update=True, exec_all_enabled=enabled)))
        assert _saves(client) == []

    def test_a_client_consents_with_true(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            asyncio.run(server.mcp.call_tool("update_run_advanced_settings", dict(
                run_id="12388", confirm_update=True, container="off")))
        assert len(_saves(client)) == 1


class TestRefusals:
    @pytest.mark.parametrize("status", ["Completed", "Running", "Failed", "Initializing", "Submitted",
                                        "LaunchFailed", "Terminated", "NextSuc", "Unreachable"])
    def test_a_launched_run_is_refused(self, status):
        client = _client(status=status)
        with pytest.raises(ToolError, match="not changed"):
            _update(client, exec_all={"memory": "16"})
        assert _saves(client) == []

    @pytest.mark.parametrize("kwargs, fragment", [
        ({}, "Nothing to change"),
        ({"container": "podman"}, "container must be one of"),
        ({"container": "off", "container_image": "x"}, "takes no container_image"),
        ({"container": "docker", "container_image": "  "}, "must not be empty"),
        ({"exec_all": {"ram": "8"}}, "ram"),
        ({"exec_all": {"memory": "8 GB"}}, "must be a number"),
        ({"exec_all": {"cpu": {"n": 1}}}, "text or a number"),
        ({"exec_all": {}}, "is empty"),
        ({"exec_all": "8 GB"}, "must be an object"),
        ({"process_exec": {"STAR": {"memory": "8"}}}, "procGnum-"),
        ({"process_exec": {}}, "process_exec must be an object"),
        ({"process_exec": {"procGnum-1": {"time": "1h"}}}, "must be a number"),
    ])
    def test_a_malformed_argument_is_refused_before_any_request(self, kwargs, fragment):
        client = _client()
        with pytest.raises(ToolError, match=fragment):
            _update(client, **kwargs)
        client.call.assert_not_called()

    def test_an_image_without_a_container_needs_one_in_use(self):
        client = _client()
        with pytest.raises(ToolError, match="uses no container"):
            _update(client, container_options="-B /share:/share")
        assert _saves(client) == []

    def test_singularity_without_an_image_is_refused(self):
        client = _client()
        with pytest.raises(ToolError, match="no saved singularity image"):
            _update(client, container="singularity")
        assert _saves(client) == []

    def test_singularity_options_cannot_be_cleared(self):
        client = _client(details=WITH_SINGULARITY)
        with pytest.raises(ToolError, match="cannot clear"):
            _update(client, container_options="")
        assert _saves(client) == []


class TestSave:
    def test_turns_singularity_off_without_touching_executor_settings(self):
        client = _client(details=WITH_SINGULARITY)
        out = _update(client, container="off")
        body = _saves(client)[0]
        assert body == {"permission": 63, "groupId": None, "useDocker": False, "useSingularity": False}
        assert out["data"]["saved_fields"] == ["useDocker", "useSingularity"]
        assert out["data"]["previous"]["container"]["type"] == "singularity"

    def test_docker_keeps_the_saved_image_and_sends_both_switches(self):
        client = _client()
        _update(client, container="docker", container_options="--gpus all")
        body = _saves(client)[0]
        assert body["useDocker"] is True and body["useSingularity"] is False
        assert body["dockerConfig"] == {"image": "quay.io/viascientific/pipeline_base_image:1.0",
                                        "runOptions": "--gpus all"}

    def test_singularity_takes_the_given_image(self):
        client = _client()
        _update(client, container="singularity", container_image="docker://ummsbiocore/seurat:1.0",
                container_options="-B /share:/share")
        body = _saves(client)[0]
        assert body["useDocker"] is False and body["useSingularity"] is True
        assert body["singularityConfig"] == {"imagePath": "docker://ummsbiocore/seurat:1.0",
                                             "runOptions": "-B /share:/share"}

    def test_options_alone_apply_to_the_container_in_use(self):
        client = _client(details=WITH_SINGULARITY)
        _update(client, container_options="-B /home:/home")
        assert _saves(client)[0]["singularityConfig"] == {
            "imagePath": "docker://quay.io/viascientific/rnaseq:4.0", "runOptions": "-B /home:/home"}

    def test_exec_all_merges_onto_the_saved_values(self):
        client = _client()
        _update(client, exec_all={"memory": 16, "options": "-R rusage[ngpus_physical=1]"})
        body = _saves(client)[0]
        assert body["execAll"] == {"queue": "gpu", "memory": "16", "cpu": "1", "time": "30",
                                   "options": "-R rusage[ngpus_physical=1]"}
        assert body["execAllCheck"] is True
        assert "useDocker" not in body and "execEach" not in body

    def test_the_12366_values_save_as_given(self):
        client = _client()
        _update(client, container="off", exec_all={"queue": "gpu", "memory": "8", "cpu": "1", "time": "30"})
        body = _saves(client)[0]
        assert body["execAll"] == {"queue": "gpu", "memory": "8", "cpu": "1", "time": "30", "options": ""}
        assert body["useSingularity"] is False

    def test_a_switch_alone_keeps_the_values(self):
        client = _client()
        _update(client, exec_all_enabled=False)
        assert _saves(client)[0] == {"permission": 63, "groupId": None, "execAllCheck": False}

    def test_a_process_override_is_added_and_keeps_the_others(self):
        client = _client(details=WITH_OVERRIDE)
        _update(client, process_exec={"procGnum-3p12": {"memory": "72", "cpu": "4"}})
        body = _saves(client)[0]
        assert body["execEachCheck"] is True
        assert body["execEach"] == {
            "procGnum-1": {"check": True, "queue": "gpu", "memory": "12", "cpu": "2", "time": "45", "options": ""},
            "procGnum-3p12": {"check": True, "queue": "", "memory": "72", "cpu": "4", "time": "", "options": ""},
        }

    def test_an_override_merges_onto_its_saved_row(self):
        client = _client(details=WITH_OVERRIDE)
        _update(client, process_exec={"procGnum-1": {"time": "90"}})
        assert _saves(client)[0]["execEach"]["procGnum-1"] == {
            "check": True, "queue": "gpu", "memory": "12", "cpu": "2", "time": "90", "options": ""}

    def test_null_removes_an_override_by_sending_it_switched_off(self):
        # The server ignores an empty execEach, so the row goes with check false.
        client = _client(details=WITH_OVERRIDE)
        _update(client, process_exec={"procGnum-1": None})
        body = _saves(client)[0]
        assert body["execEach"] == {"procGnum-1": {"check": False, "queue": "gpu", "memory": "12", "cpu": "2",
                                                   "time": "45", "options": ""}}
        assert body["execEachCheck"] is False

    def test_returns_previous_and_read_back_settings(self):
        client = _client()
        out = _update(client, exec_all={"memory": "16"})
        assert out["data"]["previous"]["exec_all"]["memory"] == "8"
        assert "current" in out["data"]
        endpoints = [c.kwargs["endpoint"] for c in client.call.call_args_list]
        assert endpoints[-1].endswith("/details")

    def test_a_failed_save_is_an_error(self):
        client = _client()
        original = client.call.side_effect

        def call(method, endpoint, data=None, **kwargs):
            if method == "PATCH":
                raise RuntimeError("400 useSingularity")
            return original(method, endpoint, data=data, **kwargs)

        client.call.side_effect = call
        with pytest.raises(ToolError, match="400"):
            _update(client, container="off")
