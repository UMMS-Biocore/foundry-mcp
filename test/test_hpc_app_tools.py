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


NOT_GRANTED = {"error": "execution_not_granted", "message": "x"}


@pytest.mark.parametrize(
    "tool",
    [
        lambda: server.hpc_app_launch(),
        lambda: server.hpc_app_list(),
        lambda: server.hpc_app_status(LAUNCH),
        lambda: server.hpc_app_stop(LAUNCH),
    ],
    ids=["launch", "list", "status", "stop"],
)
def test_every_tool_names_the_reconnect_step_on_execution_not_granted(via_client, http, tool):
    http.side_effect = [_response(403, NOT_GRANTED)]
    with pytest.raises(ToolError) as e:
        tool()
    body = json.loads(str(e.value))
    assert body["reason"] == "execution_not_granted"
    assert "Reconnect" in body["error"] and "tick" in body["error"]


def test_launch_names_the_reconnect_step_when_the_post_is_refused(via_client, http):
    http.side_effect = [_response(200, ONE_ENV), _response(403, NOT_GRANTED)]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch()
    assert json.loads(str(e.value))["reason"] == "execution_not_granted"
    assert [c[0] for c in _calls(http)].count("POST") == 1


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


@pytest.mark.parametrize("status", [502, 503, 504, 500])
def test_launch_gateway_answer_is_unknown_not_refused(via_client, http, status):
    http.side_effect = [_response(200, ONE_ENV), _response(status)]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch()
    body = json.loads(str(e.value))
    assert body["reason"] == "outcome_unknown"
    assert "hpc_app_list" in body["error"]
    assert [c[0] for c in _calls(http)].count("POST") == 1


def test_launch_server_error_with_a_reason_is_still_refused(via_client, http):
    http.side_effect = [_response(200, ONE_ENV), _response(500, {"reason": "submit_failed", "detail": "No."})]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch()
    assert json.loads(str(e.value))["reason"] == "submit_failed"


def test_launch_generic_server_error_body_is_unknown_not_refused(via_client, http):
    http.side_effect = [_response(200, ONE_ENV), _response(500, {"error": "Internal Server Error"})]
    with pytest.raises(ToolError) as e:
        server.hpc_app_launch()
    body = json.loads(str(e.value))
    assert body["reason"] == "outcome_unknown"
    assert "hpc_app_list" in body["error"]
    assert [c[0] for c in _calls(http)].count("POST") == 1


def test_stop_generic_server_error_body_is_unknown_not_refused(via_client, http):
    http.side_effect = [_response(500, {"error": "Internal Server Error"})]
    with pytest.raises(ToolError) as e:
        server.hpc_app_stop(LAUNCH)
    body = json.loads(str(e.value))
    assert body["reason"] == "outcome_unknown" and "hpc_app_status" in body["error"]
    assert len(http.call_args_list) == 1


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


@pytest.mark.parametrize("bad", ["../x", "?x", "..", "a%2Fb", "a b", "#", "", LAUNCH.upper(), LAUNCH + "x"])
def test_status_rejects_a_path_like_id(via_client, http, bad):
    with pytest.raises(ToolError) as e:
        server.hpc_app_status(bad)
    assert json.loads(str(e.value))["reason"] == "invalid_launch_id"
    http.assert_not_called()


@pytest.mark.parametrize("state", ["stopping", "ended"])
def test_stop_reports_the_launch_as_it_is(via_client, http, state):
    http.side_effect = [_response(202, {**VIEW, "state": state})]
    out = json.loads(server.hpc_app_stop(LAUNCH))
    assert _calls(http) == [("POST", f"/api/v1/hpc-app-launches/{LAUNCH}/stop")]
    assert out["launch"]["state"] == state


def test_stop_gateway_answer_is_unknown_not_refused(via_client, http):
    http.side_effect = [_response(504)]
    with pytest.raises(ToolError) as e:
        server.hpc_app_stop(LAUNCH)
    body = json.loads(str(e.value))
    assert body["reason"] == "outcome_unknown" and "hpc_app_status" in body["error"]
    assert len(http.call_args_list) == 1


# ---------------------------------------------------------------- app_execute

import base64  # noqa: E402

from mcp.server.fastmcp import Image  # noqa: E402

OPS = f"/api/v1/hpc-app-launches/{LAUNCH}/operations"
OP_ID = "0123456789abcdef0123456789abcdef"
SHA = "a" * 64


def _ended(state="succeeded", **extra):
    body = {
        "opId": OP_ID, "op": "execute", "state": state, "reason": None, "truncated": False,
        "outputBytes": 2, "events": [{"t": "stdout", "text": "hi"}], "result": None,
        "replayed": False, "outputRetained": True, "sessionRestarted": False,
    }
    body.update(extra)
    return body


RUNNING = {"opId": OP_ID, "state": "running"}


def _text(out):
    return json.loads(out if isinstance(out, str) else out[0])


@pytest.mark.parametrize(
    "kwargs, op, args",
    [
        ({"op": "execute", "code": "print(1)"}, "execute", {"code": "print(1)"}),
        ({"op": "cells"}, "cells", {}),
        ({"op": "create_cell", "code": "x = 1", "after": "Hbol", "run": False}, "create_cell",
         {"code": "x = 1", "after": "Hbol", "run": False}),
        ({"op": "create_cell", "code": "x = 1"}, "create_cell", {"code": "x = 1", "run": True}),
        ({"op": "edit_cell", "cell_id": "Hbol", "code": "x = 2", "expected_code_sha256": SHA}, "edit_cell",
         {"cellId": "Hbol", "code": "x = 2", "expectedCodeSha256": SHA, "run": True}),
        ({"op": "run_cell", "cell_id": "Hbol"}, "run_cell", {"cellId": "Hbol"}),
        ({"op": "delete_cell", "cell_id": "Hbol", "expected_code_sha256": SHA}, "delete_cell",
         {"cellId": "Hbol", "expectedCodeSha256": SHA}),
        ({"op": "status"}, "status", {}),
        ({"op": "interrupt"}, "interrupt", {}),
    ],
)
def test_app_execute_request_shape_per_op(via_client, http, kwargs, op, args):
    http.side_effect = [_response(200, _ended())]
    out = _text(server.app_execute(LAUNCH, op_id=OP_ID, notebook="n.py", timeout_seconds=120, **kwargs))
    assert _calls(http) == [("POST", OPS)]
    assert _sent_body(http.call_args) == {
        "opId": OP_ID, "op": op, "notebook": "n.py", "args": args, "waitSeconds": 25, "timeoutSeconds": 120,
    }
    assert out["op_id"] == OP_ID and out["state"] == "succeeded"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"op": "nope"},
        {"op": "execute"},
        {"op": "create_cell"},
        {"op": "edit_cell", "cell_id": "Hbol", "code": "x"},
        {"op": "edit_cell", "code": "x", "expected_code_sha256": SHA},
        {"op": "run_cell"},
        {"op": "delete_cell", "cell_id": "Hbol"},
        {"op": "execute", "code": "1", "op_id": "short"},
        {"op": "execute", "code": "1", "notebook": "../x.py"},
        {"op": "execute", "code": "1", "timeout_seconds": 0},
        {"op": "execute", "code": "1", "timeout_seconds": 3601},
        {"op": "execute", "code": "1", "wait_seconds": -1},
        {"op": "execute", "code": "1", "wait_seconds": 51},
    ],
)
def test_app_execute_rejects_a_bad_call_before_any_request(via_client, http, kwargs):
    with pytest.raises(ToolError) as e:
        server.app_execute(LAUNCH, **kwargs)
    assert json.loads(str(e.value))["reason"] == "invalid_argument"
    http.assert_not_called()


def test_app_execute_rejects_a_path_like_launch_id(via_client, http):
    with pytest.raises(ToolError) as e:
        server.app_execute("../x", op="cells")
    assert json.loads(str(e.value))["reason"] == "invalid_launch_id"
    http.assert_not_called()


def test_app_execute_generates_an_op_id_and_returns_it(via_client, http):
    http.side_effect = [_response(200, _ended())]
    out = _text(server.app_execute(LAUNCH, op="cells"))
    sent = _sent_body(http.call_args)["opId"]
    assert len(sent) == 32 and out["op_id"] == sent


def test_app_execute_retry_after_a_lost_answer_reuses_the_op_id(via_client, http):
    http.side_effect = [requests.ConnectionError("lost"), _response(200, _ended())]
    out = _text(server.app_execute(LAUNCH, op="execute", code="1"))
    assert _calls(http) == [("POST", OPS), ("POST", OPS)]
    first, second = (_sent_body(c) for c in http.call_args_list)
    assert first == second and first["opId"] == out["op_id"]


def test_app_execute_202_then_polls_until_done(via_client, http):
    http.side_effect = [_response(202, RUNNING), _response(200, _ended())]
    out = _text(server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID))
    assert _calls(http) == [("POST", OPS), ("GET", f"{OPS}/{OP_ID}")]
    assert http.call_args_list[1].kwargs["params"] == {"waitSeconds": 20}
    assert out["state"] == "succeeded"


def test_app_execute_polls_with_the_remainder_of_the_50_second_cap(via_client, http):
    http.side_effect = [_response(202, RUNNING)] + [_response(202, RUNNING)] * 3
    server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID, wait_seconds=50)
    assert _sent_body(http.call_args_list[0])["waitSeconds"] == 25
    waits = [c.kwargs["params"]["waitSeconds"] for c in http.call_args_list[1:]]
    assert waits == [25]


def test_app_execute_running_after_the_wait_names_the_follow_up(via_client, http):
    http.side_effect = [_response(202, RUNNING), _response(202, RUNNING)]
    out = _text(server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID))
    assert len(http.call_args_list) == 2
    assert out["state"] == "running" and out["op_id"] == OP_ID
    assert "app_operation_status" in out["next_step"] and OP_ID in out["next_step"]


def test_app_execute_with_a_short_wait_does_not_poll(via_client, http):
    http.side_effect = [_response(202, RUNNING)]
    out = _text(server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID, wait_seconds=10))
    assert _calls(http) == [("POST", OPS)]
    assert _sent_body(http.call_args)["waitSeconds"] == 10
    assert out["state"] == "running"


@pytest.mark.parametrize("status, body", [(504, None), (500, {"error": "Internal Server Error"})])
def test_app_execute_lost_poll_answer_is_not_a_refusal(via_client, http, status, body):
    http.side_effect = [_response(202, RUNNING)] + [_response(status, body)] * 3
    with pytest.raises(ToolError) as e:
        server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID)
    answer = json.loads(str(e.value))
    assert answer["reason"] == "operation_status_unavailable" and answer["op_id"] == OP_ID
    assert "app_operation_status" in answer["error"] and "Do not start" in answer["error"]


def test_app_execute_poll_connection_failure_names_the_follow_up(via_client, http):
    http.side_effect = [_response(202, RUNNING)] + [requests.ConnectionError("lost")] * 3
    with pytest.raises(ToolError) as e:
        server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID)
    answer = json.loads(str(e.value))
    assert answer["op_id"] == OP_ID and "app_operation_status" in answer["error"]


def test_app_execute_outcome_unknown_says_not_to_rerun(via_client, http):
    http.side_effect = [_response(200, _ended("outcome_unknown", reason="app_proxy_lost", events=[]))]
    out = _text(server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID))
    assert out["state"] == "outcome_unknown"
    assert "not" in out["next_step"].lower() and "cells" in out["next_step"] and "ask" in out["next_step"].lower()


def test_app_execute_conflict_says_to_show_the_persons_cell(via_client, http):
    http.side_effect = [_response(200, _ended("conflict", reason="conflict"))]
    out = _text(server.app_execute(LAUNCH, op="edit_cell", cell_id="Hbol", code="x", expected_code_sha256=SHA))
    assert "ask" in out["next_step"].lower() and "cell" in out["next_step"]


def test_app_execute_gateway_answer_on_the_post_is_unknown_with_the_op_id(via_client, http):
    http.side_effect = [_response(504, None)] * 3  # the transport retries a gateway answer
    with pytest.raises(ToolError) as e:
        server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID)
    body = json.loads(str(e.value))
    assert body["reason"] == "outcome_unknown" and body["op_id"] == OP_ID
    assert "app_operation_status" in body["error"]


PNG_BYTES = b"\x89PNG\r\n\x1a\n-bytes"
JPEG_BYTES = b"\xff\xd8\xff\xe0-bytes"


def test_app_execute_image_event_becomes_image_content(via_client, http):
    png = base64.b64encode(PNG_BYTES).decode()
    events = [{"t": "stdout", "text": "x"}, {"t": "output", "mimetype": "image/png", "data": png}]
    http.side_effect = [_response(200, _ended(events=events))]
    out = server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID)
    assert isinstance(out, list) and len(out) == 2 and isinstance(out[1], Image)
    assert out[1].data == PNG_BYTES and out[1]._mime_type == "image/png"
    assert png not in out[0]


def test_app_execute_data_url_image_event_becomes_image_content(via_client, http):
    url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
    events = [{"t": "output", "mimetype": "image/png", "data": url}]
    http.side_effect = [_response(200, _ended(events=events))]
    out = server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID)
    assert isinstance(out, list) and out[1].data == PNG_BYTES and out[1]._mime_type == "image/png"
    assert url not in out[0]


def test_app_execute_cell_thumbnail_format_comes_from_the_bytes(via_client, http):
    thumb = base64.b64encode(JPEG_BYTES).decode()
    cells = {"cells": [{"id": "Hbol", "code": "x", "output": {"type": "image/png", "bytes": 5, "thumbnail": thumb}}]}
    http.side_effect = [_response(200, _ended(op="cells", result=cells, events=[]))]
    out = server.app_execute(LAUNCH, op="cells", op_id=OP_ID)
    assert isinstance(out[1], Image) and out[1].data == JPEG_BYTES
    assert out[1]._mime_type == "image/jpeg"
    assert thumb not in out[0]


def test_app_execute_bytes_that_are_no_known_image_stay_text(via_client, http):
    thumb = base64.b64encode(b"not an image").decode()
    cells = {"cells": [{"id": "Hbol", "code": "x", "output": {"type": "image/png", "thumbnail": thumb}}]}
    http.side_effect = [_response(200, _ended(op="cells", result=cells, events=[]))]
    out = server.app_execute(LAUNCH, op="cells", op_id=OP_ID)
    assert isinstance(out, str)


def test_app_execute_bad_image_data_stays_text(via_client, http):
    events = [{"t": "image", "mimetype": "image/png", "data": "!!not base64!!"}]
    http.side_effect = [_response(200, _ended(events=events))]
    out = server.app_execute(LAUNCH, op="execute", code="1", op_id=OP_ID)
    assert isinstance(out, str)


def test_app_execute_relays_the_origin_refusal(via_client, http):
    http.side_effect = [_response(409, {"reason": "app_origin_not_isolated", "detail": "Ask your administrator."})]
    with pytest.raises(ToolError) as e:
        server.app_execute(LAUNCH, op="cells")
    body = json.loads(str(e.value))
    assert body["reason"] == "app_origin_not_isolated" and body["error"] == "Ask your administrator."
    assert body["http_status"] == 409 and body["op_id"]


def test_app_execute_names_the_reconnect_step_when_not_granted(via_client, http):
    http.side_effect = [_response(403, NOT_GRANTED)]
    with pytest.raises(ToolError) as e:
        server.app_execute(LAUNCH, op="cells")
    assert json.loads(str(e.value))["reason"] == "execution_not_granted"


def test_app_operation_status_gets_with_the_wait(via_client, http):
    http.side_effect = [_response(200, _ended())]
    out = _text(server.app_operation_status(LAUNCH, OP_ID, wait_seconds=10))
    assert _calls(http) == [("GET", f"{OPS}/{OP_ID}")]
    assert http.call_args.kwargs["params"] == {"waitSeconds": 10}
    assert out["state"] == "succeeded"


def test_app_operation_status_running_and_unknown_op(via_client, http):
    http.side_effect = [_response(202, RUNNING), _response(404, {"reason": "operation_not_found"})]
    assert _text(server.app_operation_status(LAUNCH, OP_ID))["state"] == "running"
    with pytest.raises(ToolError) as e:
        server.app_operation_status(LAUNCH, OP_ID)
    assert json.loads(str(e.value))["reason"] == "operation_not_found"


def test_app_operation_status_gateway_answer_is_not_a_refusal(via_client, http):
    http.side_effect = [_response(502, None)] * 3
    with pytest.raises(ToolError) as e:
        server.app_operation_status(LAUNCH, OP_ID)
    answer = json.loads(str(e.value))
    assert answer["reason"] == "operation_status_unavailable" and OP_ID in answer["error"]


@pytest.mark.parametrize("bad_op, wait", [("short", 25), (OP_ID, 51), (OP_ID, -1)])
def test_app_operation_status_rejects_bad_arguments(via_client, http, bad_op, wait):
    with pytest.raises(ToolError):
        server.app_operation_status(LAUNCH, bad_op, wait_seconds=wait)
    http.assert_not_called()


def test_the_reconnect_step_names_the_consent_box_word_for_word(via_client, http):
    http.side_effect = [_response(403, NOT_GRANTED)]
    with pytest.raises(ToolError) as e:
        server.hpc_app_list()
    assert '"Run code in my cluster notebooks"' in json.loads(str(e.value))["error"]


def test_app_execute_conflict_says_nothing_was_written(via_client, http):
    http.side_effect = [_response(200, _ended("conflict", reason="conflict"))]
    out = _text(server.app_execute(LAUNCH, op="edit_cell", cell_id="Hbol", code="x", expected_code_sha256=SHA))
    assert "Nothing of yours was written" in out["next_step"]


def test_app_execute_conflict_after_write_says_the_write_was_applied(via_client, http):
    http.side_effect = [_response(200, _ended("conflict", reason="conflict_after_write"))]
    out = _text(server.app_execute(LAUNCH, op="edit_cell", cell_id="Hbol", code="x", expected_code_sha256=SHA))
    assert "write was applied" in out["next_step"] and "Do not write it again" in out["next_step"]
    assert "Nothing of yours was written" not in out["next_step"]


def test_app_execute_landed_change_whose_run_did_not_finish_says_not_to_resend(via_client, http):
    result = {"cellId": "Hbol", "codeSha256": SHA, "run": None, "applied": True, "runState": "cancelled", "runReason": "stopped"}
    http.side_effect = [_response(200, _ended("succeeded", op="create_cell", result=result))]
    out = _text(server.app_execute(LAUNCH, op="create_cell", code="x", run=True))
    assert out["state"] == "succeeded" and out["result"]["applied"] is True
    assert "Do not create or edit the cell again" in out["next_step"] and "run_cell" in out["next_step"]


def test_app_execute_plain_success_has_no_next_step(via_client, http):
    http.side_effect = [_response(200, _ended("succeeded", op="create_cell", result={"cellId": "Hbol", "codeSha256": SHA}))]
    out = _text(server.app_execute(LAUNCH, op="create_cell", code="x", run=False))
    assert "next_step" not in out
