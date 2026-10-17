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


# What a disabled workspace route answers: disableRouteHandler sends the
# backend's ERROR_PAGE_TEMPLATE as a 404 HTML page.
_DISABLED_ROUTE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Error</title>
</head>
<body>
    <pre>Cannot POST /api/v1/agent-workspace/ws-abc123/jobs</pre>
</body>
</html>"""


def _html_response(status, text):
    resp = MagicMock()
    resp.status_code = status
    resp.json.side_effect = ValueError("not json")
    resp.text = text
    return resp


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

    def test_a_body_that_is_not_json_comes_back_as_none(self, via_client, http):
        http.return_value = _html_response(404, _DISABLED_ROUTE_PAGE)
        assert client_module.request_json(via_client, "POST", "/x") == (404, None)

    def test_no_retry_by_default(self, via_client, http):
        http.side_effect = requests.ConnectionError("reset")
        with pytest.raises(client_module.RequestFailed) as exc:
            client_module.request_json(via_client, "POST", "/x")
        assert http.call_count == 1
        assert exc.value.reason == "connection_failed"
        assert exc.value.may_have_arrived is True

    def test_a_broken_chunked_answer_is_retried_then_a_clear_error(self, via_client, http):
        http.side_effect = requests.exceptions.ChunkedEncodingError("cut short")
        with pytest.raises(client_module.RequestFailed) as exc:
            client_module.request_json(via_client, "GET", "/x", retry=True)
        assert http.call_count == 3
        assert exc.value.reason == "connection_failed"
        assert "ChunkedEncodingError" in exc.value.message
        assert isinstance(exc.value.__cause__, requests.exceptions.ChunkedEncodingError)

    @pytest.mark.parametrize("exc", [
        requests.exceptions.ContentDecodingError("bad gzip"),
        requests.exceptions.TooManyRedirects("loop"),
    ])
    def test_any_other_requests_error_is_a_request_failed_that_may_have_arrived(self, via_client, http, exc):
        http.side_effect = exc
        with pytest.raises(client_module.RequestFailed) as exc_info:
            client_module.request_json(via_client, "POST", "/x", retry=True)
        assert http.call_count == 1
        assert exc_info.value.reason == "connection_failed"
        assert exc_info.value.may_have_arrived is True
        assert type(exc).__name__ in exc_info.value.message

    def test_send_request_says_when_an_earlier_attempt_was_lost(self, via_client, http):
        http.side_effect = [requests.Timeout("slow"), _response(429)]
        answer = client_module.send_request(via_client, "POST", "/x", retry=True)
        assert (answer.status, answer.body, answer.earlier_attempt_lost) == (429, None, True)

    def test_send_request_on_the_first_attempt_lost_nothing(self, via_client, http):
        http.return_value = _response(429)
        assert client_module.send_request(via_client, "POST", "/x", retry=True).earlier_attempt_lost is False

    @pytest.mark.parametrize("hostname", ["foundry.example.org", "https://", "ftp://foundry.example.org"])
    def test_an_unusable_hostname_is_a_clear_error_and_nothing_is_sent(self, via_client, http, hostname):
        via_client.auth.hostname = hostname
        with pytest.raises(client_module.RequestFailed) as exc:
            client_module.request_json(via_client, "GET", "/x", retry=True)
        assert http.call_count == 0
        assert exc.value.reason == "invalid_hostname"
        assert exc.value.may_have_arrived is False
        assert "https://" in exc.value.message

    @pytest.mark.parametrize("exc", [
        requests.exceptions.InvalidSchema("No connection adapters were found for 'ftp://elsewhere'"),
        requests.exceptions.InvalidURL("Invalid URL 'https://': No host supplied"),
        requests.exceptions.MissingSchema("Invalid URL 'elsewhere'"),
    ])
    def test_an_address_error_while_sending_came_from_a_redirect_and_may_have_arrived(self, via_client, http, exc):
        # The configured address is fine, so requests met the bad address in a
        # redirect Location: the first request was sent.
        http.side_effect = exc
        with pytest.raises(client_module.RequestFailed) as exc_info:
            client_module.request_json(via_client, "POST", "/x", retry=True)
        assert http.call_count == 1
        assert exc_info.value.reason == "connection_failed"
        assert exc_info.value.may_have_arrived is True

    def test_an_address_error_after_a_lost_attempt_may_have_arrived(self, via_client, http):
        http.side_effect = [requests.Timeout("slow"), requests.exceptions.InvalidURL("Invalid URL")]
        with pytest.raises(client_module.RequestFailed) as exc:
            client_module.request_json(via_client, "POST", "/x", retry=True)
        assert http.call_count == 2
        assert exc.value.reason == "connection_failed"
        assert exc.value.may_have_arrived is True


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
        assert out["reason"] == "dispatch_unconfirmed"

    def test_a_success_status_with_no_outcome_is_uncertain(self, via_client, http):
        http.return_value = _response(200, {})
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "uncertain"
        assert out["http_status"] == 200
        assert "do not use a new key" in out["message"]

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

    def test_the_disabled_route_html_404_gives_the_plain_sentence(self, via_client, http):
        http.return_value = _html_response(404, _DISABLED_ROUTE_PAGE)
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "not_found_or_disabled"
        assert err["http_status"] == 404
        assert err["error"] == server._NOT_FOUND_MESSAGE
        assert "<" not in err["error"] and "Cannot POST" not in err["error"]

    @pytest.mark.parametrize("status", [502, 504])
    def test_a_gateway_page_on_the_last_attempt_is_uncertain_not_refused(self, via_client, http, status):
        page = f"<html><head><title>{status} Bad Gateway</title></head><body>nginx</body></html>"
        http.return_value = _html_response(status, page)
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "uncertain"
        assert out["submission_key"] == "k"
        assert out["http_status"] == status
        assert "same submission_key" in out["message"] and "do not use a new key" in out["message"]
        assert "not submitted" not in out["message"]
        assert "<" not in out["message"]
        assert http.call_count == 3

    def test_a_server_error_with_no_outcome_is_uncertain(self, via_client, http):
        http.return_value = _response(500, {"error": "boom"})
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "uncertain"
        assert out["http_status"] == 500

    def test_a_client_error_with_no_outcome_stays_a_refusal(self, via_client, http):
        http.return_value = _response(400, {"reason": "invalid_body", "message": "cpus is too large."})
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "invalid_body"
        assert err["submission_key"] == "k"

    def test_a_429_after_a_lost_attempt_is_uncertain_not_refused(self, via_client, http):
        http.side_effect = [requests.Timeout("slow"), _response(429)]
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "uncertain"
        assert out["submission_key"] == "k"
        assert out["http_status"] == 429
        assert "not submitted" not in out["message"]

    @pytest.mark.parametrize("last", [
        lambda: _response(408),
        lambda: _response(429),
        lambda: _html_response(400, "<html><body>Bad Request</body></html>"),
    ])
    def test_a_client_error_with_no_outcome_after_lost_attempts_is_uncertain(self, via_client, http, last):
        http.side_effect = [requests.Timeout("slow"), requests.Timeout("slow"), last()]
        out = json.loads(server.workspace_run(WS, "echo hi"))
        assert out["status"] == "uncertain"
        assert out["submission_key"].startswith("mcp-")
        assert "not submitted" not in out["message"]
        assert "<" not in out["message"]

    def test_a_typed_refusal_after_a_lost_attempt_stays_a_refusal(self, via_client, http):
        # The server deduplicates by key, so a typed outcome speaks for the whole call.
        http.side_effect = [requests.Timeout("slow"), _response(409, {"kind": "conflict"})]
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        assert _error(exc)["reason"] == "submission_key_conflict"

    @pytest.mark.parametrize("exc", [
        requests.exceptions.ContentDecodingError("bad gzip"),
        requests.exceptions.TooManyRedirects("loop"),
    ])
    def test_an_unreadable_answer_is_uncertain_and_keeps_the_minted_key(self, via_client, http, exc):
        http.side_effect = exc
        with pytest.raises(ToolError) as exc_info:
            server.workspace_run(WS, "echo hi")
        err = _error(exc_info)
        assert err["reason"] == "uncertain"
        assert err["submission_key"].startswith("mcp-")
        assert "same submission_key" in err["error"]
        assert http.call_count == 1

    @pytest.mark.parametrize("exc_type", [requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError])
    def test_every_attempt_failing_in_transport_keeps_the_minted_key(self, via_client, http, exc_type):
        http.side_effect = exc_type("read timed out")
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi")
        err = _error(exc)
        assert http.call_count == 3
        sent = {c.kwargs["headers"]["Idempotency-Key"] for c in http.call_args_list}
        assert sent == {err["submission_key"]}
        assert err["submission_key"].startswith("mcp-")
        assert err["reason"] == "uncertain"
        assert err["workspace_id"] == WS
        assert "same submission_key" in err["error"] and "do not use a new key" in err["error"]

    def test_a_bad_hostname_is_refused_with_the_key_and_not_called_uncertain(self, via_client, http):
        via_client.auth.hostname = "foundry.example.org"
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "invalid_hostname"
        assert err["submission_key"] == "k"
        assert http.call_count == 0

    @pytest.mark.parametrize("side_effect", [
        [requests.Timeout("slow"), requests.exceptions.InvalidURL("Invalid URL")],
        [requests.exceptions.InvalidSchema("No connection adapters were found")],
    ])
    def test_an_address_error_once_a_request_was_sent_is_uncertain(self, via_client, http, side_effect):
        http.side_effect = side_effect
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "uncertain"
        assert err["submission_key"] == "k"
        assert "same submission_key" in err["error"]

    @pytest.mark.parametrize("status,body", [
        (201, {"kind": "queued_v2"}),
        (202, {"kind": "queued_v2"}),
        (500, {"kind": "internal"}),
        (409, {"kind": "held_for_quota"}),
    ])
    def test_an_outcome_kind_this_client_does_not_know_is_uncertain(self, via_client, http, status, body):
        http.return_value = _response(status, body)
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "uncertain"
        assert out["submission_key"] == "k"
        assert out["http_status"] == status
        assert out["server_outcome"] == body["kind"]
        assert "not submitted" not in out["message"]

    @pytest.mark.parametrize("lost_first", [False, True])
    @pytest.mark.parametrize("status", [400, 404, 409])
    @pytest.mark.parametrize("kind", [["conflict"], {"name": "conflict"}])
    def test_a_kind_that_is_not_a_string_is_uncertain_and_keeps_the_key(self, via_client, http, kind, status, lost_first):
        answer = _response(status, {"kind": kind, "reason": "x"})
        http.side_effect = [requests.Timeout("slow"), answer] if lost_first else [answer]
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "uncertain"
        assert out["submission_key"] == "k"
        assert out["http_status"] == status
        assert out["server_outcome"] == kind
        assert "not submitted" not in out["message"]

    @pytest.mark.parametrize("status", [200, 201, 500, 503])
    def test_a_known_refusal_kind_outside_a_4xx_is_uncertain(self, via_client, http, status):
        http.return_value = _response(status, {"kind": "conflict"})
        out = json.loads(server.workspace_run(WS, "echo hi", submission_key="k"))
        assert out["status"] == "uncertain"
        assert out["http_status"] == status

    def test_a_typed_refused_after_a_lost_attempt_stays_a_refusal(self, via_client, http):
        http.side_effect = [requests.Timeout("slow"), _response(404, {"kind": "refused", "reason": "workspace_not_found"})]
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "workspace_not_found"
        assert err["submission_key"] == "k"

    @pytest.mark.parametrize("status", [401, 403])
    def test_an_auth_refusal_says_to_check_the_token(self, via_client, http, status):
        http.return_value = _response(status, {"error": "Unauthorized"})
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["http_status"] == status
        assert "token" in err["error"]
        assert err["submission_key"] == "k"

    def test_an_auth_refusal_with_a_server_message_keeps_it_and_adds_the_token_hint(self, via_client, http):
        http.return_value = _response(403, {"reason": "workspace_forbidden", "message": "Not your workspace."})
        with pytest.raises(ToolError) as exc:
            server.workspace_run(WS, "echo hi", submission_key="k")
        err = _error(exc)
        assert err["reason"] == "workspace_forbidden"
        assert err["error"] == "Not your workspace."
        assert "token" in err["remedy"]

    def test_the_tool_offers_no_mount_or_docker_host_parameter(self):
        import inspect
        params = set(inspect.signature(server.workspace_run).parameters)
        assert params == {"workspace_id", "command", "cpus", "memory_mb", "wall_minutes", "submission_key"}

    def test_the_docstring_warns_it_launches_compute_and_explains_keys(self):
        doc = " ".join(server.workspace_run.__doc__.split())
        assert "LAUNCHES" in doc and "confirm with the user" in doc
        assert "A new key starts a new job." in doc
        for status in ("submitted", "awaiting_approval", "review_stale", "uncertain"):
            assert status in doc


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


class TestTransportErrorsInTheOtherTools:
    @pytest.mark.parametrize("exc_type", [requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError])
    def test_list_and_status_turn_transport_errors_into_tool_errors(self, via_client, http, exc_type):
        http.side_effect = exc_type("down")
        for call in (lambda: server.workspace_list(), lambda: server.workspace_job_status(WS, "k")):
            with pytest.raises(ToolError) as exc:
                call()
            err = _error(exc)
            assert err["reason"] == "connection_failed"
            assert "could not be reached" in err["error"]

    def test_a_create_timeout_says_to_check_the_list_before_retrying(self, via_client, http):
        http.side_effect = requests.Timeout("read timed out")
        with pytest.raises(ToolError) as exc:
            server.workspace_create("12", "34", "s", "img")
        err = _error(exc)
        assert http.call_count == 1
        assert err["reason"] == "connection_failed"
        assert "workspace_list" in err["error"]

    def test_a_cancel_transport_error_says_to_read_the_job(self, via_client, http):
        http.side_effect = requests.ConnectionError("reset")
        with pytest.raises(ToolError) as exc:
            server.workspace_cancel(WS, "k")
        err = _error(exc)
        assert http.call_count == 1
        assert "workspace_job_status" in err["error"]

    def test_a_bad_hostname_is_a_clear_error_in_every_tool(self, via_client, http):
        via_client.auth.hostname = "foundry.example.org"
        for call in (
            lambda: server.workspace_list(),
            lambda: server.workspace_create("12", "34", "s", "img"),
            lambda: server.workspace_job_status(WS, "k"),
            lambda: server.workspace_cancel(WS, "k"),
        ):
            with pytest.raises(ToolError) as exc:
                call()
            err = _error(exc)
            assert err["reason"] == "invalid_hostname"
            assert "workspace_list" not in err["error"]

    @pytest.mark.parametrize("status", [401, 403])
    def test_an_auth_refusal_in_list_says_to_check_the_token(self, via_client, http, status):
        http.return_value = _html_response(status, "<html>Unauthorized</html>")
        with pytest.raises(ToolError) as exc:
            server.workspace_list()
        err = _error(exc)
        assert err["reason"] == ("unauthorized" if status == 401 else "forbidden")
        assert "token" in err["error"]
        assert "<" not in err["error"]


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
