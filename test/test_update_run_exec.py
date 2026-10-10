"""Tests for update_run's executor and Singularity settings, and the preflight
warning for a batch scheduler run that asks for no memory."""
import json
from unittest.mock import MagicMock, patch

import pytest

from src.foundry_mcp import server


def _client(details=None):
    client = MagicMock()

    def _call(**kwargs):
        if kwargs.get("endpoint", "").endswith("/details"):
            return details or {}
        return ""

    client.call.side_effect = _call
    return client


def _update(details=None, **kwargs):
    client = _client(details)
    with patch.object(server, "get_client", return_value=client):
        out = json.loads(server.update_run("41", permission=3, **kwargs))
    return out, client


def _saved_body(client):
    patches = [c.kwargs for c in client.call.call_args_list if c.kwargs.get("method") == "PATCH"]
    assert len(patches) == 1
    return patches[0]["data"]


# The shape get_run_details(verbose=True) returns under advancedSettings.
DETAILS_EXEC = {
    "globalExec": {"enabled": True, "options": {
        "job_queue": "short", "job_memory": "32", "job_cpu": "1",
        "job_time": "240", "job_clu_opt": ""}},
    "processExec": {"enabled": True, "options": {
        "procGnum-12": {"check": "on", "queue": "long", "memory": "200",
                        "cpu": "5", "time": "1440", "opt": ""}}},
}


class TestExecSettings:
    def test_the_details_shape_becomes_the_save_body(self):
        _, client = _update(exec_settings=DETAILS_EXEC)
        body = _saved_body(client)
        assert body["execAllCheck"] is True
        assert body["execAll"] == {"queue": "short", "memory": "32", "cpu": "1",
                                   "time": "240", "options": ""}
        assert body["execEachCheck"] is True
        assert body["execEach"] == {"procGnum-12": {
            "check": True, "queue": "long", "memory": "200", "cpu": "5",
            "time": "1440", "options": ""}}

    def test_plain_keys_and_numbers_are_accepted(self):
        _, client = _update(exec_settings={"globalExec": {"options": {"queue": "short", "memory": 64, "cpu": 4}}})
        body = _saved_body(client)
        assert body["execAllCheck"] is True
        assert body["execAll"] == {"queue": "short", "memory": "64", "cpu": "4", "time": "", "options": ""}
        assert "execEachCheck" not in body

    def test_disabling_sends_only_the_switch(self):
        _, client = _update(exec_settings={"processExec": {"enabled": False}})
        body = _saved_body(client)
        assert body["execEachCheck"] is False
        assert "execEach" not in body and "execAllCheck" not in body

    def test_an_off_process_row_is_kept_unchecked(self):
        settings = {"processExec": {"options": {"procGnum-3": {"check": "off", "memory": "8"}}}}
        _, client = _update(exec_settings=settings)
        assert _saved_body(client)["execEach"]["procGnum-3"]["check"] is False

    @pytest.mark.parametrize("settings, fragment", [
        ({"processExec": {"options": {"STAR": {"memory": "200"}}}}, "procGnum-"),
        ({"globalExec": {"options": {"ram": "32"}}}, "ram"),
        ({"docker": {"enabled": True}}, "docker"),
        ({"globalExec": {"enabled": "yes"}}, "enabled"),
        ({"globalExec": {"options": {"memory": {"gb": 32}}}}, "memory"),
        ("32 GB", "object"),
    ])
    def test_a_malformed_setting_is_refused_without_calling(self, settings, fragment):
        out, client = _update(exec_settings=settings)
        assert fragment in out["error"]
        client.call.assert_not_called()

    def test_the_empty_save_reply_names_what_was_saved(self):
        out, _ = _update(exec_settings=DETAILS_EXEC)
        saved = out["data"]["saved"]
        assert saved["execAll"]["memory"] == "32"
        assert saved["execEach"] == ["procGnum-12"]


SINGULARITY_DETAILS = {"advancedSettings": {"singularity": {
    "enabled": True, "image": "quay.io/viascientific/rnaseq:4.0", "options": "-B /home:/home"}}}


class TestSingularityOptions:
    def test_options_are_saved_with_the_current_image(self):
        _, client = _update(details=SINGULARITY_DETAILS, singularity_options="-B /home:/home -B /share:/share")
        body = _saved_body(client)
        assert body["useSingularity"] is True
        assert body["singularityConfig"] == {"imagePath": "quay.io/viascientific/rnaseq:4.0",
                                             "runOptions": "-B /home:/home -B /share:/share"}

    def test_a_run_without_a_singularity_image_is_refused(self):
        details = {"advancedSettings": {"singularity": {"enabled": False, "image": "", "options": ""}}}
        out, client = _update(details=details, singularity_options="-B /share:/share")
        assert "Singularity" in out["error"]
        assert not any(c.kwargs.get("method") == "PATCH" for c in client.call.call_args_list)

    def test_it_cannot_ride_with_a_run_environment_change(self):
        out, client = _update(details=SINGULARITY_DETAILS, singularity_options="-B /share:/share",
                              run_environment_id=550)
        assert "run_environment_id" in out["error"]
        client.call.assert_not_called()

    def test_an_empty_value_is_refused(self):
        out, client = _update(details=SINGULARITY_DETAILS, singularity_options="  ")
        assert "error" in out
        client.call.assert_not_called()


# ---------------------------------------------------------------------------
# preflight: executor_resources
# ---------------------------------------------------------------------------

def _env(**extra):
    env = {"id": 1126, "executor": "local", "executorJob": "lsf", "jobMemory": None}
    env.update(extra)
    return env


def _preflight(env, advanced=None):
    details = {
        "launchDirectory": "/home/u/runs/r1",
        "runEnvironment": {"selectedId": env["id"], "availableList": [env]},
        "inputs": [],
        "advancedSettings": advanced or {},
    }
    client = MagicMock()

    def _call(**kwargs):
        endpoint = kwargs.get("endpoint", "")
        if endpoint.endswith("/details"):
            return details
        if "/run/list" in endpoint:
            return {"data": [{"status": "Not Submitted"}]}
        return {}

    client.call.side_effect = _call
    with patch.object(server, "get_client", return_value=client):
        parsed = json.loads(server.preflight_run("41"))
    return next((c for c in parsed["data"]["checks"] if c["id"] == "executor_resources"), None)


class TestPreflightExecutorResources:
    def test_a_scheduler_run_with_no_memory_anywhere_warns(self):
        check = _preflight(_env())
        assert check["status"] == "warn"
        assert "lsf" in check["detail"]
        assert "exec_settings" in check["fix"]

    def test_run_wide_memory_passes(self):
        check = _preflight(_env(), {"globalExec": DETAILS_EXEC["globalExec"]})
        assert check["status"] == "pass"

    def test_a_disabled_block_does_not_count(self):
        off = {"globalExec": {"enabled": False, "options": DETAILS_EXEC["globalExec"]["options"]}}
        assert _preflight(_env(), off)["status"] == "warn"

    def test_per_process_memory_passes(self):
        check = _preflight(_env(), {"processExec": DETAILS_EXEC["processExec"]})
        assert check["status"] == "pass"

    def test_the_environment_default_memory_passes_when_the_run_defers_to_it(self):
        defer = {"globalExec": {"enabled": False, "options": {"job_memory": ""}}}
        assert _preflight(_env(jobMemory="16"), defer)["status"] == "pass"

    def test_the_environment_default_is_not_applied_to_a_run_with_no_settings(self):
        assert _preflight(_env(jobMemory="16"))["status"] == "warn"

    def test_run_wide_settings_with_blank_memory_ignore_the_environment(self):
        blank = {"globalExec": {"enabled": True, "options": {"job_queue": "short", "job_memory": ""}}}
        assert _preflight(_env(jobMemory="16"), blank)["status"] == "warn"

    @pytest.mark.parametrize("executor", ["local", "awsbatch", "", None])
    def test_no_check_when_tasks_do_not_go_to_a_batch_scheduler(self, executor):
        assert _preflight(_env(executorJob=executor)) is None

    def test_the_scheduler_name_is_matched_without_case(self):
        assert _preflight(_env(executorJob="SLURM"))["status"] == "warn"
