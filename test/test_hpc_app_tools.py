"""
Tests for the cluster app tools: launch, list, check and stop an app.

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

ONE_ENV = {
    "environments": [{"profileClusterId": 7, "name": "Cluster", "dataRoots": [{"name": "scratch", "mode": "rw"}]}],
    "apps": [{"appId": 11, "name": "Marimo notebook", "kind": "marimo"}, {"appId": 12, "name": "Shiny", "kind": "shiny"}],
}
TWO_ENV = {
    "environments": ONE_ENV["environments"] + [{"profileClusterId": 8, "name": "Other", "dataRoots": []}],
    "apps": ONE_ENV["apps"],
}
VIEW = {
    "launchId": LAUNCH,
    "state": "requested",
    "mounts": [{"name": "scratch", "containerPath": "/mnt/scratch", "mode": "ro"}],
}


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


def test_launch_uses_the_only_environment_and_posts_the_body(via_client, http):
    http.side_effect = [_response(200, ONE_ENV), _response(201, VIEW)]
    out = json.loads(server.hpc_app_launch(data_roots=["scratch"], cpus=2, memory_mb=2048, wall_minutes=30))
    assert _calls(http) == [
        ("GET", "/api/v1/hpc-app-launches/options"),
        ("POST", "/api/v1/hpc-app-launches"),
    ]
    assert _sent_body(http.call_args_list[1]) == {
        "cpus": 2,
        "memoryMb": 2048,
        "wallMinutes": 30,
        "dataRoots": [{"name": "scratch"}],
        "appId": 11,
        "profileClusterId": 7,
    }
    assert out["launch"]["launchId"] == LAUNCH
    assert out["mounts"] == VIEW["mounts"]
    assert out["open_in_foundry_connect"] == f"{HOST}/apps/cluster-launches"
    assert "_auth" not in json.dumps(out)


def test_launch_sends_an_empty_data_roots_list_when_none_asked(via_client, http):
    http.side_effect = [_response(200, ONE_ENV), _response(201, VIEW)]
    server.hpc_app_launch()
    body = _sent_body(http.call_args_list[1])
    assert body["dataRoots"] == [] and "mounts" not in body
    assert (body["cpus"], body["memoryMb"], body["wallMinutes"]) == (1, 4096, 60)


def test_launch_passes_data_root_objects_through(via_client, http):
    http.side_effect = [_response(200, ONE_ENV), _response(201, VIEW)]
    root = {"name": "scratch", "mode": "rw", "subpath": "a/b"}
    server.hpc_app_launch(data_roots=[root])
    assert _sent_body(http.call_args_list[1])["dataRoots"] == [root]


def test_launch_with_several_environments_returns_the_options_and_sends_nothing(via_client, http):
    http.side_effect = [_response(200, TWO_ENV)]
    out = json.loads(server.hpc_app_launch())
    assert out["needs_choice"] == "run_environment_id"
    assert [e["profileClusterId"] for e in out["environments"]] == [7, 8]
    assert _calls(http) == [("GET", "/api/v1/hpc-app-launches/options")]


def test_launch_with_a_chosen_environment(via_client, http):
    http.side_effect = [_response(200, TWO_ENV), _response(201, VIEW)]
    server.hpc_app_launch(run_environment_id=8)
    assert _sent_body(http.call_args_list[1])["profileClusterId"] == 8


def test_launch_refuses_an_environment_that_is_not_listed(via_client, http):
    http.side_effect = [_response(200, ONE_ENV)]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch(run_environment_id=99)
    assert json.loads(str(e.value))["reason"] == "run_environment_unavailable"
    assert len(http.call_args_list) == 1


def test_launch_with_no_environment(via_client, http):
    http.side_effect = [_response(200, {"environments": [], "apps": ONE_ENV["apps"]})]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch()
    assert json.loads(str(e.value))["reason"] == "no_run_environment"


def test_launch_names_the_reconnect_step_on_execution_not_granted(via_client, http):
    http.side_effect = [_response(403, {"error": "execution_not_granted", "message": "x"})]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch()
    body = json.loads(str(e.value))
    assert body["reason"] == "execution_not_granted"
    assert "Reconnect" in body["error"] and "tick" in body["error"]


def test_launch_keeps_the_servers_refusal_reason_and_detail(via_client, http):
    http.side_effect = [_response(200, ONE_ENV), _response(400, {"reason": "data_root_unknown", "detail": "No data root named nope."})]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch(data_roots=["nope"])
    body = json.loads(str(e.value))
    assert body["reason"] == "data_root_unknown" and body["error"] == "No data root named nope."


def test_launch_is_not_retried_and_says_to_check_the_list(via_client, http):
    http.side_effect = [_response(200, ONE_ENV), requests.ConnectionError("down")]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch()
    assert "hpc_app_list" in json.loads(str(e.value))["error"]
    assert [c[0] for c in _calls(http)].count("POST") == 1


def test_launch_rejects_a_bad_number_before_any_request(via_client, http):
    with pytest.raises(ToolError):
        server.hpc_app_launch(cpus=0)
    http.assert_not_called()


def test_list_gets_the_launches(via_client, http):
    http.side_effect = [_response(200, [VIEW])]
    out = json.loads(server.hpc_app_list())
    assert _calls(http) == [("GET", "/api/v1/hpc-app-launches")]
    assert out["launches"] == [VIEW]


def test_status_gets_one_launch(via_client, http):
    http.side_effect = [_response(200, VIEW)]
    out = json.loads(server.hpc_app_status(LAUNCH))
    assert _calls(http) == [("GET", f"/api/v1/hpc-app-launches/{LAUNCH}")]
    assert out["launch"]["state"] == "requested"


def test_status_of_an_unknown_launch(via_client, http):
    http.side_effect = [_response(404, {"reason": "launch_not_found"})]
    with pytest.raises(ToolError) as e:
        server.hpc_app_status(LAUNCH)
    assert json.loads(str(e.value))["reason"] == "launch_not_found"


def test_status_rejects_a_path_like_id(via_client, http):
    with pytest.raises(ToolError):
        server.hpc_app_status("../x")
    http.assert_not_called()


@pytest.mark.parametrize("state", ["stopping", "ended"])
def test_stop_reports_the_launch_as_it_is(via_client, http, state):
    http.side_effect = [_response(202, {**VIEW, "state": state})]
    out = json.loads(server.hpc_app_stop(LAUNCH))
    assert _calls(http) == [("POST", f"/api/v1/hpc-app-launches/{LAUNCH}/stop")]
    assert out["launch"]["state"] == state


def test_stop_names_the_reconnect_step_on_execution_not_granted(via_client, http):
    http.side_effect = [_response(403, {"error": "execution_not_granted", "message": "x"})]
    with pytest.raises(ToolError) as e:
        server.hpc_app_stop(LAUNCH)
    assert json.loads(str(e.value))["reason"] == "execution_not_granted"
