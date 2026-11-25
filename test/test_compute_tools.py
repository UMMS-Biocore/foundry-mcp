"""
Tests for the compute session tools: Workbench cloud VMs, shared server app
instances and the compute_open link.

The HTTP layer is patched at `requests.request`, so these tests see exactly the
method, path and body each tool sends.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from mcp.server.fastmcp.exceptions import ToolError

from src.foundry_mcp import client as client_module
from src.foundry_mcp import server
from src.foundry_mcp.config import set_credentials

HOST = "https://foundry.example.org"
LAUNCH = "a2b3c4d5e6f7g2h3i4j5k6l7m2"

NOT_GRANTED = {
    "error": "compute_control_not_granted",
    "message": "This assistant connection may not start, stop, change or delete compute sessions.",
}
FORBIDDEN = {"message": "You are not authorized to access this resource or it does not exist."}


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
    fake.auth.get_headers.return_value = {}
    set_credentials(HOST, "placeholder")
    with patch.object(server, "get_client", return_value=fake), patch.object(client_module.time, "sleep"):
        yield fake
    set_credentials(None, None)


@pytest.fixture
def http():
    with patch.object(client_module.requests, "request") as req:
        yield req


def _calls(http):
    return [(c.args[0], c.args[1].replace(HOST, "")) for c in http.call_args_list]


def _sent_body(call):
    return call.kwargs.get("json")


def _tool_error(excinfo):
    return json.loads(str(excinfo.value))


# ---------------------------------------------------------------- compute_open


def test_compute_open_builds_the_page_link_and_calls_nothing(via_client, http):
    out = json.loads(server.compute_open("workbench:42"))
    assert out["open_in_foundry_connect"] == f"{HOST}/compute/open/workbench:42"
    assert out["kind"] == "workbench"
    assert http.call_count == 0


def test_compute_open_accepts_every_kind(via_client, http):
    for session_id, kind in (
        ("shinyproxy-app:7", "shinyproxy-app"),
        (f"cluster-app:{LAUNCH}", "cluster-app"),
    ):
        out = json.loads(server.compute_open(session_id))
        assert out["kind"] == kind
        assert out["open_in_foundry_connect"] == f"{HOST}/compute/open/{session_id}"
    assert http.call_count == 0


def test_compute_open_strips_the_api_suffix_from_the_address(http):
    set_credentials(f"{HOST}/api", "placeholder")
    try:
        out = json.loads(server.compute_open("workbench:1"))
    finally:
        set_credentials(None, None)
    assert out["open_in_foundry_connect"] == f"{HOST}/compute/open/workbench:1"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "workbench:0",
        "workbench:01",
        "workbench:abc",
        "workbench:12345678901",
        "cluster-app:short",
        "cluster-app:A2B3C4D5E6F7G2H3I4J5K6L7M2",
        "other:1",
        "workbench:1/../../admin",
        "42",
    ],
)
def test_compute_open_refuses_anything_that_is_not_a_session_id(via_client, http, bad):
    with pytest.raises(ToolError) as excinfo:
        server.compute_open(bad)
    assert _tool_error(excinfo)["reason"] == "invalid_session_id"
    assert http.call_count == 0


# ---------------------------------------------------------------- workbench list

INSTANCES = {
    "skip": 0,
    "take": 50,
    "total": 2,
    "data": [
        {
            "id": 42,
            "workbench": "RStudio",
            "name": "analysis",
            "accessUrl": "https://vm.example.org/42",
            "status": "RUNNING",
            "owner": {"id": 3, "username": "someone"},
            "updatedAt": "2026-11-24T10:00:00.000Z",
            "uptimeSeconds": 3600,
            "dateCreated": "2026-11-24T09:00:00.000Z",
            "vmLastStateChangedAt": "2026-11-24T09:05:00.000Z",
        },
        {
            "id": 43,
            "workbench": "Jupyter",
            "name": "",
            "accessUrl": "https://vm.example.org/43",
            "status": "SUSPENDED",
            "owner": {"id": 3, "username": "someone"},
            "updatedAt": "2026-11-24T10:00:00.000Z",
            "uptimeSeconds": 0,
            "dateCreated": "2026-11-23T09:00:00.000Z",
            "vmLastStateChangedAt": "2026-11-23T19:00:00.000Z",
        },
    ],
    "metadata": {"totals": {"accessible": 2, "running": 1, "stopped": 1, "terminated": 0, "totalCost": "1.20"}, "owners": []},
}


def test_workbench_list_reads_the_instance_list_and_maps_states(via_client, http):
    http.return_value = _response(200, INSTANCES)
    out = json.loads(server.workbench_list())
    assert _calls(http) == [("GET", "/api/v1/workbench/instance/list")]
    assert http.call_args.kwargs["params"] == {"take": 50, "skip": 0}
    first, second = out["sessions"]
    assert first["kind"] == "workbench"
    assert first["session_id"] == "workbench:42"
    assert first["instance_id"] == 42
    assert first["state"] == "running"
    assert first["status"] == "RUNNING"
    assert first["actions"] == ["open", "extend", "edit", "stop", "terminate"]
    assert second["state"] == "stopped"
    assert second["actions"] == ["edit", "start", "terminate"]
    assert out["total"] == 2


def test_workbench_list_never_returns_the_access_url(via_client, http):
    http.return_value = _response(200, INSTANCES)
    text = server.workbench_list()
    assert "accessUrl" not in text
    assert "vm.example.org" not in text


@pytest.mark.parametrize(
    "status,state,actions",
    [
        ("PROVISIONING", "starting", []),
        ("STAGING", "starting", []),
        ("RESUMING", "starting", []),
        ("REPAIRING", "starting", []),
        ("PENDING_STOP", "stopping", []),
        ("STOPPING", "stopping", []),
        ("SUSPENDING", "stopping", []),
        ("TERMINATING", "stopping", []),
        ("TERMINATED", "stopped", ["edit", "start", "terminate"]),
        ("DELETED", "ended", ["delete"]),
        ("FAILED", "ended", ["delete"]),
        ("SOMETHING_NEW", "unknown", []),
    ],
)
def test_workbench_states_follow_the_compute_vocabulary(via_client, http, status, state, actions):
    body = {**INSTANCES, "data": [{**INSTANCES["data"][0], "status": status}]}
    http.return_value = _response(200, body)
    session = json.loads(server.workbench_list())["sessions"][0]
    assert session["state"] == state
    assert session["actions"] == actions


def test_workbench_list_bounds_the_page(via_client, http):
    http.return_value = _response(200, INSTANCES)
    server.workbench_list(take=500, skip=-3)
    assert http.call_args.kwargs["params"] == {"take": 200, "skip": 0}


def test_workbench_list_refusal_keeps_the_server_message(via_client, http):
    http.return_value = _response(404, {"message": "Workbench is not enabled."})
    with pytest.raises(ToolError) as excinfo:
        server.workbench_list()
    err = _tool_error(excinfo)
    assert err["error"] == "Workbench is not enabled."
    assert err["http_status"] == 404


APPLICATIONS = {
    "skip": 0,
    "take": 100,
    "total": 1,
    "data": [
        {
            "id": 5,
            "name": "RStudio",
            "description": "R in the browser",
            "image": "example/rstudio:1",
            "port": 8787,
            "env": "",
            "command": "",
            "template": {"id": 1, "title": "RStudio", "description": ""},
            "owner": {"id": 3, "username": "someone"},
            "own": True,
            "groupId": None,
            "perms": 3,
            "dateCreated": "2026-11-01T00:00:00.000Z",
            "dateModified": "2026-11-01T00:00:00.000Z",
        }
    ],
}


def test_workbench_applications_reads_mine_and_shared_once_each(via_client, http):
    shared = {**APPLICATIONS, "data": [APPLICATIONS["data"][0], {**APPLICATIONS["data"][0], "id": 6, "name": "Jupyter", "own": False}]}
    http.side_effect = [_response(200, APPLICATIONS), _response(200, shared)]
    out = json.loads(server.workbench_applications())
    assert _calls(http) == [
        ("GET", "/api/v1/workbench/application/list"),
        ("GET", "/api/v1/workbench/application/list"),
    ]
    assert [c.kwargs["params"]["type"] for c in http.call_args_list] == ["1", "2"]
    assert [a["application_id"] for a in out["applications"]] == [5, 6]
    assert out["instance_types"][0] == "n2-highmem-2"


# ---------------------------------------------------------------- workbench writes


def test_workbench_launch_posts_the_form_without_a_login(via_client, http):
    http.return_value = _response(200, {"id": 44, "name": "analysis"})
    out = json.loads(server.workbench_launch(application_id=5, instance_type="n2-standard-4", disk_size_gb=50, name="analysis"))
    assert _calls(http) == [("POST", "/api/v1/workbench/instance/create/5")]
    body = _sent_body(http.call_args)
    assert body == {"name": "analysis", "instanceType": "n2-standard-4", "diskSize": 50, "password": ""}
    assert out["session_id"] == "workbench:44"
    assert out["open_in_foundry_connect"] == f"{HOST}/compute/open/workbench:44"
    assert out["kind"] == "workbench"


def test_workbench_launch_is_never_retried(via_client, http):
    http.side_effect = [_response(502, None), _response(200, {"id": 44})]
    with pytest.raises(ToolError) as excinfo:
        server.workbench_launch(application_id=5, instance_type="n2-standard-4", disk_size_gb=50)
    assert _tool_error(excinfo)["reason"] == "outcome_unknown"
    assert http.call_count == 1


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"application_id": 0, "instance_type": "n2-standard-4", "disk_size_gb": 50}, "invalid_argument"),
        ({"application_id": 5, "instance_type": "huge", "disk_size_gb": 50}, "invalid_argument"),
        ({"application_id": 5, "instance_type": "n2-standard-4", "disk_size_gb": 0}, "invalid_argument"),
        ({"application_id": 5, "instance_type": "n2-standard-4", "disk_size_gb": True}, "invalid_argument"),
    ],
)
def test_workbench_launch_validates_before_sending(via_client, http, kwargs, reason):
    with pytest.raises(ToolError) as excinfo:
        server.workbench_launch(**kwargs)
    assert _tool_error(excinfo)["reason"] == reason
    assert http.call_count == 0


@pytest.mark.parametrize(
    "tool,method,path",
    [
        ("workbench_stop", "GET", "/api/v1/workbench/instance/stop/42"),
        ("workbench_start", "GET", "/api/v1/workbench/instance/start/42"),
    ],
)
def test_workbench_stop_and_start_call_their_routes(via_client, http, tool, method, path):
    http.return_value = _response(200, 1)
    out = json.loads(getattr(server, tool)(42))
    assert _calls(http) == [(method, path)]
    assert out["session_id"] == "workbench:42"
    assert out["kind"] == "workbench"


def test_workbench_extend_sets_the_timeout_in_seconds(via_client, http):
    http.return_value = _response(200, 1)
    out = json.loads(server.workbench_extend(42, minutes=90))
    assert _calls(http) == [("POST", "/api/v1/workbench/instance/42/set-timeout")]
    assert _sent_body(http.call_args) == {"totalSeconds": 5400}
    assert out["session_id"] == "workbench:42"


@pytest.mark.parametrize("minutes", [0, -5, 14401, True])
def test_workbench_extend_stays_within_ten_days(via_client, http, minutes):
    with pytest.raises(ToolError) as excinfo:
        server.workbench_extend(42, minutes=minutes)
    assert _tool_error(excinfo)["reason"] == "invalid_argument"
    assert http.call_count == 0


def test_workbench_edit_sends_only_what_changes(via_client, http):
    http.return_value = _response(200, {"success": True, "message": "Workbench instance updated successfully"})
    server.workbench_edit(42, instance_type="n2-highmem-4", disk_size_gb=200)
    assert _calls(http) == [("PATCH", "/api/v1/workbench/instance/42")]
    assert _sent_body(http.call_args) == {"instanceType": "n2-highmem-4", "diskSize": 200}


def test_workbench_edit_needs_a_change(via_client, http):
    with pytest.raises(ToolError) as excinfo:
        server.workbench_edit(42)
    assert _tool_error(excinfo)["reason"] == "invalid_argument"
    assert http.call_count == 0


def test_workbench_edit_keeps_the_servers_bad_request_message(via_client, http):
    http.return_value = _response(400, {"message": "Instance must be in a stopped state to change its type or disk size."})
    with pytest.raises(ToolError) as excinfo:
        server.workbench_edit(42, disk_size_gb=200)
    err = _tool_error(excinfo)
    assert err["error"].startswith("Instance must be in a stopped state")
    assert err["http_status"] == 400


def test_workbench_terminate_needs_confirmation(via_client, http):
    with pytest.raises(ToolError) as excinfo:
        server.workbench_terminate(42)
    assert _tool_error(excinfo)["reason"] == "confirmation_required"
    assert http.call_count == 0


def test_workbench_terminate_calls_the_route(via_client, http):
    http.return_value = _response(204, None)
    out = json.loads(server.workbench_terminate(42, confirm_terminate=True))
    assert _calls(http) == [("GET", "/api/v1/workbench/instance/terminate/42")]
    assert out["session_id"] == "workbench:42"


def test_workbench_delete_needs_confirmation(via_client, http):
    with pytest.raises(ToolError) as excinfo:
        server.workbench_delete(42, confirm_delete="yes")
    assert _tool_error(excinfo)["reason"] == "confirmation_required"
    assert http.call_count == 0


def test_workbench_delete_calls_the_route(via_client, http):
    http.return_value = _response(204, None)
    out = json.loads(server.workbench_delete(42, confirm_delete=True))
    assert _calls(http) == [("DELETE", "/api/v1/workbench/instance/42")]
    assert out["session_id"] == "workbench:42"


@pytest.mark.parametrize("bad", [0, -1, "abc", "4 2", True, None])
def test_workbench_ids_are_positive_whole_numbers(via_client, http, bad):
    with pytest.raises(ToolError) as excinfo:
        server.workbench_stop(bad)
    assert _tool_error(excinfo)["reason"] == "invalid_argument"
    assert http.call_count == 0


def test_workbench_id_given_as_session_id_is_accepted(via_client, http):
    http.return_value = _response(200, 1)
    server.workbench_stop("workbench:42")
    assert _calls(http) == [("GET", "/api/v1/workbench/instance/stop/42")]


def test_workbench_not_found_keeps_the_server_message(via_client, http):
    http.return_value = _response(403, FORBIDDEN)
    with pytest.raises(ToolError) as excinfo:
        server.workbench_start(42)
    err = _tool_error(excinfo)
    assert err["error"] == FORBIDDEN["message"]
    assert err["http_status"] == 403


@pytest.mark.parametrize(
    "call",
    [
        lambda: server.workbench_launch(application_id=5, instance_type="n2-standard-4", disk_size_gb=50),
        lambda: server.workbench_stop(42),
        lambda: server.workbench_start(42),
        lambda: server.workbench_extend(42, minutes=60),
        lambda: server.workbench_edit(42, name="x"),
        lambda: server.workbench_terminate(42, confirm_terminate=True),
        lambda: server.workbench_delete(42, confirm_delete=True),
        lambda: server.app_terminate(7, confirm_terminate=True),
    ],
)
def test_missing_compute_permission_says_how_to_grant_it(via_client, http, call):
    http.return_value = _response(403, NOT_GRANTED)
    with pytest.raises(ToolError) as excinfo:
        call()
    err = _tool_error(excinfo)
    assert err["reason"] == "compute_control_not_granted"
    assert "Manage my compute sessions" in err["error"]
    assert "Reconnect" in err["error"]
    assert err["http_status"] == 403
    assert http.call_count == 1


def test_a_lost_answer_on_stop_is_reported_as_unknown(via_client, http):
    http.side_effect = requests.ConnectionError("reset")
    with pytest.raises(ToolError) as excinfo:
        server.workbench_stop(42)
    err = _tool_error(excinfo)
    assert "workbench_list" in err["error"]


# ---------------------------------------------------------------- app_terminate


def test_app_terminate_needs_confirmation(via_client, http):
    with pytest.raises(ToolError) as excinfo:
        server.app_terminate(7)
    assert _tool_error(excinfo)["reason"] == "confirmation_required"
    assert http.call_count == 0


def test_app_terminate_calls_the_route(via_client, http):
    http.return_value = _response(204, None)
    out = json.loads(server.app_terminate(7, confirm_terminate=True))
    assert _calls(http) == [("GET", "/api/v1/app/terminate/7")]
    assert out["session_id"] == "shinyproxy-app:7"
    assert out["kind"] == "shinyproxy-app"


def test_app_terminate_accepts_a_session_id(via_client, http):
    http.return_value = _response(204, None)
    server.app_terminate("shinyproxy-app:7", confirm_terminate=True)
    assert _calls(http) == [("GET", "/api/v1/app/terminate/7")]


def test_app_terminate_refuses_another_kinds_session_id(via_client, http):
    with pytest.raises(ToolError) as excinfo:
        server.app_terminate("workbench:7", confirm_terminate=True)
    assert _tool_error(excinfo)["reason"] == "invalid_argument"
    assert http.call_count == 0


def test_app_terminate_reports_an_instance_that_is_not_yours(via_client, http):
    http.return_value = _response(200, {"status": "Warning", "message": "App not found but its okay. We pretend that it is terminated."})
    with pytest.raises(ToolError) as excinfo:
        server.app_terminate(7, confirm_terminate=True)
    err = _tool_error(excinfo)
    assert err["reason"] == "not_found"


def test_app_terminate_reports_a_shared_server_failure(via_client, http):
    http.return_value = _response(200, {"status": "fail", "message": "proxy did not answer"})
    with pytest.raises(ToolError) as excinfo:
        server.app_terminate(7, confirm_terminate=True)
    err = _tool_error(excinfo)
    assert err["reason"] == "terminate_failed"
    assert err["error"] == "proxy did not answer"
