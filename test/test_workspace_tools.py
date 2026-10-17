"""
Tests for the workspace tools: run approved code in a container on a run
environment's workspace.

The HTTP layer is patched at `requests.request`, so these tests see exactly the
headers, bodies and retries the tools send.
"""

import json
import pathlib
import re
from unittest.mock import MagicMock, patch

import pytest
import requests
from mcp.server.fastmcp.exceptions import ToolError

from src.foundry_mcp import client as client_module
from src.foundry_mcp import server
from src.foundry_mcp.config import set_credentials

HOST = "https://foundry.example.org"
WS = "ws-abc123"


def _response(status, body=None):
    resp = MagicMock()
    resp.status_code = status
    if body is None:
        resp.json.side_effect = ValueError("no body")
        resp.text = ""
    else:
        resp.json.return_value = body
        resp.text = json.dumps(body)
    return resp


@pytest.fixture
def via_client():
    fake = MagicMock()
    fake.auth.hostname = HOST
    fake.auth.get_headers.return_value = {"Authorization": "Bearer placeholder"}
    set_credentials(HOST, "via_mcp_placeholder")
    with patch.object(server, "get_client", return_value=fake), patch.object(client_module.time, "sleep"):
        yield fake
    set_credentials(None, None)


@pytest.fixture
def http():
    with patch.object(client_module.requests, "request") as req:
        yield req


def _error(exc_info):
    return json.loads(str(exc_info.value))


class TestRequestJson:
    def test_merges_extra_headers_and_does_not_raise_on_http_errors(self, via_client, http):
        http.return_value = _response(409, {"reason": "x"})
        status, body = client_module.request_json(
            via_client, "POST", "/api/v1/thing", data={"a": 1}, headers={"Idempotency-Key": "k1"},
        )
        assert (status, body) == (409, {"reason": "x"})
        kwargs = http.call_args.kwargs
        assert kwargs["headers"]["Idempotency-Key"] == "k1"
        assert kwargs["headers"]["Authorization"] == "Bearer placeholder"
        assert kwargs["json"] == {"a": 1}
        assert http.call_args.args == ("POST", f"{HOST}/api/v1/thing")

    def test_retries_a_dropped_connection_only_when_asked(self, via_client, http):
        http.side_effect = [requests.ConnectionError("reset"), _response(200, {"ok": True})]
        assert client_module.request_json(via_client, "GET", "/x", retry=True) == (200, {"ok": True})
        assert http.call_count == 2

    def test_no_retry_by_default(self, via_client, http):
        http.side_effect = requests.ConnectionError("reset")
        with pytest.raises(requests.ConnectionError):
            client_module.request_json(via_client, "POST", "/x")
        assert http.call_count == 1


class TestWorkspaceRun:
    def test_every_transport_retry_sends_the_same_key(self, via_client, http):
        http.side_effect = [
            requests.ConnectionError("reset"),
            _response(504),
            _response(201, {"kind": "submitted", "jobId": "123", "attemptId": 4}),
        ]
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="my-key.1"))
        keys = [c.kwargs["headers"]["Idempotency-Key"] for c in http.call_args_list]
        assert keys == ["my-key.1"] * 3
        assert out["status"] == "submitted"
        assert out["submission_key"] == "my-key.1"
        assert out["scheduler_job_id"] == "123"

    def test_an_empty_key_is_minted_once_and_returned(self, via_client, http):
        http.side_effect = [
            requests.Timeout("slow"),
            _response(201, {"kind": "submitted", "jobId": "9", "attemptId": 1}),
        ]
        out = json.loads(server.workspace_run(WS, "echo hi"))
        keys = {c.kwargs["headers"]["Idempotency-Key"] for c in http.call_args_list}
        assert len(keys) == 1
        key = keys.pop()
        assert key == out["submission_key"]
        assert key.startswith("mcp-")
        assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key)

    def test_sends_only_the_job_body(self, via_client, http):
        http.return_value = _response(201, {"kind": "submitted", "jobId": "9", "attemptId": 1})
        server.workspace_run(WS, "python run.py", cpus=2, memory_mb=4096, wall_minutes=60, submission_key="k")
        call = http.call_args
        assert call.args == ("POST", f"{HOST}/api/v1/agent-workspace/{WS}/jobs")
        assert call.kwargs["json"] == {"command": "python run.py", "cpus": 2, "memoryMb": 4096, "wallMinutes": 60}

    @pytest.mark.parametrize("bad", ["has space", "a/b", "x" * 129, "semi;colon"])
    def test_a_bad_key_is_refused_before_any_request(self, via_client, http, bad):
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key=bad)
        assert _error(exc)["reason"] == "invalid_submission_key"
        http.assert_not_called()

    def test_a_command_over_32_kib_is_refused_before_any_request(self, via_client, http):
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "x" * 32769, submission_key="k")
        assert _error(exc)["reason"] == "command_too_large"
        http.assert_not_called()

    def test_a_command_of_exactly_32_kib_is_sent(self, via_client, http):
        http.return_value = _response(201, {"kind": "submitted", "jobId": "9", "attemptId": 1})
        server.workspace_run(WS, "x" * 32768, submission_key="k")
        assert http.call_count == 1

    def test_multibyte_command_is_measured_in_bytes(self, via_client, http):
        with pytest.raises(ToolError):
            server.workspace_run(WS, "é" * 16385, submission_key="k")
        http.assert_not_called()

    @pytest.mark.parametrize("bad", ["../etc", "UPPER", "", "a" * 64])
    def test_a_bad_workspace_id_is_refused_before_any_request(self, via_client, http, bad):
        with pytest.raises(ToolError) as exc:
            server.workspace_run(bad, "echo hi", submission_key="k")
        assert _error(exc)["reason"] == "invalid_workspace_id"
        http.assert_not_called()

    def test_awaiting_approval_returns_the_approval_link(self, via_client, http):
        http.return_value = _response(202, {"kind": "awaiting_approval", "requestId": 77})
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "awaiting_approval"
        assert out["submission_key"] == "k"
        assert out["approve_at"] == f"{HOST}/workspaces/{WS}/requests/77"
        assert "A person must approve this job in Foundry Connect" in out["message"]

    def test_the_link_drops_an_api_suffix_from_the_configured_host(self, via_client, http):
        set_credentials(f"{HOST}/api/", "via_mcp_placeholder")
        http.return_value = _response(202, {"kind": "awaiting_approval", "requestId": 5})
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["approve_at"] == f"{HOST}/workspaces/{WS}/requests/5"

    def test_review_stale_asks_for_a_new_review(self, via_client, http):
        http.return_value = _response(409, {"kind": "review_stale", "requestId": 8})
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "review_stale"
        assert out["approve_at"] == f"{HOST}/workspaces/{WS}/requests/8"

    def test_uncertain_tells_the_client_to_check_with_the_same_key(self, via_client, http):
        http.return_value = _response(202, {"kind": "uncertain", "reason": "dispatch_unconfirmed"})
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "uncertain"
        assert "same submission_key" in out["message"]

    def test_a_refusal_is_an_error_with_the_servers_reason(self, via_client, http):
        http.return_value = _response(409, {
            "kind": "refused", "reason": "cap_exceeded", "message": "Too many live jobs.", "remedy": "Wait for one to finish.",
        })
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "cap_exceeded"
        assert err["error"] == "Too many live jobs."
        assert err["remedy"] == "Wait for one to finish."
        assert err["submission_key"] == "k"

    def test_a_rejected_request_says_a_new_key_starts_a_new_job(self, via_client, http):
        http.return_value = _response(409, {"kind": "request_terminal", "state": "rejected"})
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "request_rejected"
        assert "new submission_key" in err["error"]

    def test_a_key_reused_with_a_different_body_is_a_clear_conflict(self, via_client, http):
        http.return_value = _response(409, {"kind": "conflict"})
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        assert _error(exc)["reason"] == "submission_key_conflict"

    def test_a_missing_workspace_is_a_not_found_error(self, via_client, http):
        http.return_value = _response(404, {"kind": "refused", "reason": "workspace_not_found"})
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        assert _error(exc)["reason"] == "workspace_not_found"

    def test_a_404_with_no_reason_says_the_feature_may_be_off(self, via_client, http):
        http.return_value = _response(404)
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "not_found_or_disabled"
        assert err["http_status"] == 404

    def test_the_tool_offers_no_mount_or_docker_host_parameter(self):
        import inspect
        params = set(inspect.signature(server.workspace_run).parameters)
        assert params == {"workspace_id", "command", "cpus", "memory_mb", "wall_minutes", "submission_key"}

    def test_the_docstring_warns_it_launches_compute_and_explains_keys(self):
        doc = " ".join(server.workspace_run.__doc__.split())
        assert "LAUNCHES" in doc and "confirm with the user" in doc
        assert "A new key starts a new job." in doc


class TestWorkspaceJobStatus:
    def test_reads_the_job_by_its_key(self, via_client, http):
        http.return_value = _response(200, {
            "requestId": None, "attemptId": 3, "submissionKey": "k", "command": "echo hi",
            "cpus": 1, "memoryMb": 1024, "wallMinutes": 30, "status": "running",
            "schedulerJobId": "55", "createdAt": "2026-10-17T00:00:00.000Z",
        })
        out = json.loads(server.workspace_job_status(WS, "k"))
        assert http.call_args.args == ("GET", f"{HOST}/api/v1/agent-workspace/{WS}/jobs/k")
        assert out["job"]["status"] == "running"
        assert "approve_at" not in out

    def test_an_awaiting_job_carries_the_approval_link(self, via_client, http):
        http.return_value = _response(200, {
            "requestId": 12, "attemptId": None, "submissionKey": "k", "command": "echo hi",
            "cpus": 1, "memoryMb": 1024, "wallMinutes": 30, "status": "awaiting_approval",
            "schedulerJobId": None, "createdAt": "2026-10-17T00:00:00.000Z",
        })
        out = json.loads(server.workspace_job_status(WS, "k"))
        assert out["approve_at"] == f"{HOST}/workspaces/{WS}/requests/12"

    def test_an_unknown_key_is_a_not_found_error(self, via_client, http):
        http.return_value = _response(404, {"reason": "job_not_found", "message": "No job in a workspace of yours has this submission key."})
        with pytest.raises(ToolError) as exc:
            server.workspace_job_status(WS, "k")
        assert _error(exc)["reason"] == "job_not_found"

    def test_a_bad_key_is_refused_before_any_request(self, via_client, http):
        with pytest.raises(ToolError):
            server.workspace_job_status(WS, "../x")
        http.assert_not_called()


class TestWorkspaceCancel:
    def test_an_accepted_cancel(self, via_client, http):
        http.return_value = _response(202, {"kind": "accepted"})
        out = json.loads(server.workspace_cancel(WS, "k"))
        assert http.call_args.args == ("POST", f"{HOST}/api/v1/agent-workspace/{WS}/jobs/k/cancel")
        assert out["status"] == "cancel_requested"

    def test_job_not_cancellable_is_a_readable_error(self, via_client, http):
        http.return_value = _response(409, {
            "reason": "job_not_cancellable",
            "message": "This job is finished, or has no scheduler job of its own to cancel.",
            "status": "completed",
        })
        with pytest.raises(ToolError) as exc:
            server.workspace_cancel(WS, "k")
        err = _error(exc)
        assert err["reason"] == "job_not_cancellable"
        assert err["job_status"] == "completed"
        assert "completed" in err["error"]

    def test_an_awaiting_job_points_to_rejecting_it(self, via_client, http):
        http.return_value = _response(409, {"reason": "job_not_cancellable", "message": "m", "status": "awaiting_approval"})
        with pytest.raises(ToolError) as exc:
            server.workspace_cancel(WS, "k")
        assert "reject" in _error(exc)["error"]

    def test_an_unreachable_environment_says_try_again(self, via_client, http):
        http.return_value = _response(503, {"reason": "environment_unreachable", "message": "The run environment could not be reached to cancel the job. Try again later."})
        with pytest.raises(ToolError) as exc:
            server.workspace_cancel(WS, "k")
        assert _error(exc)["reason"] == "environment_unreachable"


class TestWorkspaceListAndCreate:
    def test_list_passes_the_environment_filter(self, via_client, http):
        http.return_value = _response(200, {"workspaces": [{"workspaceId": WS}]})
        out = json.loads(server.workspace_list(run_environment_id="12"))
        assert http.call_args.args == ("GET", f"{HOST}/api/v1/agent-workspace")
        assert http.call_args.kwargs["params"] == {"profileClusterId": 12}
        assert out["workspaces"] == [{"workspaceId": WS}]

    def test_list_without_a_filter(self, via_client, http):
        http.return_value = _response(200, {"workspaces": []})
        server.workspace_list()
        assert http.call_args.kwargs["params"] is None

    def test_create_posts_the_workspace_and_is_not_retried(self, via_client, http):
        http.return_value = _response(201, {"workspaceId": WS, "state": "ready"})
        out = json.loads(server.workspace_create("12", "34", "my space", "python:3.12"))
        assert http.call_args.kwargs["json"] == {"profileClusterId": 12, "projectId": 34, "name": "my space", "image": "python:3.12"}
        assert out["workspace"]["workspaceId"] == WS

    def test_create_refusal_keeps_the_failed_workspace_id(self, via_client, http):
        http.return_value = _response(409, {"reason": "provisioning_failed", "message": "The agent could not make the folder.", "workspaceId": WS})
        with pytest.raises(ToolError) as exc:
            server.workspace_create("12", "34", "s", "img")
        err = _error(exc)
        assert err["reason"] == "provisioning_failed"
        assert err["workspace_id"] == WS

    def test_create_refuses_a_non_numeric_environment_before_any_request(self, via_client, http):
        with pytest.raises(ToolError):
            server.workspace_create("abc", "34", "s", "img")
        http.assert_not_called()


def test_no_tool_can_approve_or_reject_a_job_request():
    # Approval stays a person's step in a signed in browser session; the server
    # refuses a token, and no tool here even tries.
    source = pathlib.Path(server.__file__).read_text()
    assert not re.search(r"requests/[^\"'\s]*/(approve|reject)", source)
    assert not any(name.startswith("workspace_") and ("approve" in name or "reject" in name) for name in dir(server))
