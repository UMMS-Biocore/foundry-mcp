"""Tests for the status check fields and the one shot refresh of a stale status.

The backend polls a run's status every FETCH_LOGS_INTERVAL (15 minutes by
default), so `get_run` could say Running after the log already said Success.
`GET /v1/run/{id}/status` now also returns `lastStatusCheckAt`,
`nextflowExitStatus` and `firstErrorLine`, and `POST /v1/run/{id}/sync`
(authorization: authenticated) forces a resync. When the evidence says an
active run has finished, the MCP calls /sync once and reports the new status.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
FRESH = _iso(NOW - timedelta(minutes=5))
STALE = _iso(NOW - timedelta(hours=2))


def _client(list_status="Running", status=None, synced_status="Completed",
            after_sync=None, sync_error=None):
    """A client for get_run and watch_run.

    status: the first GET /status answer. after_sync: the GET /status answer
    once /sync was called.
    """
    c = MagicMock()
    state = {"synced": False}
    status = status if status is not None else {"runStatus": list_status}

    def _call(**kwargs):
        endpoint = kwargs.get("endpoint", "")
        method = kwargs.get("method")
        if endpoint.endswith("/run/list"):
            return {"data": [{"id": 7, "name": "rnaseq",
                              "status": synced_status if state["synced"] else list_status}]}
        if endpoint.endswith("/sync") and method == "POST":
            if sync_error:
                raise sync_error
            state["synced"] = True
            return {"runStatus": synced_status, "logsSynced": True,
                    "reportsSynced": True}
        if endpoint.endswith("/status"):
            if state["synced"] and after_sync is not None:
                return after_sync
            return status
        if endpoint.endswith("/logs"):
            return []
        return {}

    c.call.side_effect = _call
    return c


def _sync_calls(client):
    return [c for c in client.call.call_args_list
            if c.kwargs.get("endpoint", "").endswith("/sync")]


def _get_run(client):
    with patch.object(server, "get_client", return_value=client), \
            patch.object(server, "_utcnow", return_value=NOW):
        return json.loads(server.get_run(run_id="7"))


def _watch(client):
    with patch.object(server, "get_client", return_value=client), \
            patch.object(server, "_utcnow", return_value=NOW):
        return json.loads(server.watch_run("7"))


class TestStatusCheckFields:
    def test_get_run_surfaces_the_check_fields(self):
        client = _client(list_status="Failed", synced_status="Failed", status={
            "runStatus": "Failed", "lastStatusCheckAt": FRESH,
            "nextflowExitStatus": 1, "firstErrorLine": "ERROR ~ boom"})
        parsed = _get_run(client)
        assert parsed["status_check"] == {
            "last_checked_at": FRESH, "nextflow_exit_status": 1,
            "first_error_line": "ERROR ~ boom"}

    def test_nulls_are_omitted(self):
        client = _client(list_status="Running", status={
            "runStatus": "Running", "lastStatusCheckAt": FRESH,
            "nextflowExitStatus": None, "firstErrorLine": None})
        parsed = _get_run(client)
        assert parsed["status_check"] == {"last_checked_at": FRESH}

    def test_no_fields_from_an_older_backend_adds_nothing(self):
        client = _client(list_status="Running",
                         status={"runStatus": "Running", "enableTerminate": True})
        parsed = _get_run(client)
        assert "status_check" not in parsed
        assert _sync_calls(client) == []

    def test_a_status_read_failure_does_not_break_get_run(self):
        client = _client(list_status="Completed")
        inner = client.call.side_effect

        def _call(**kwargs):
            if kwargs.get("endpoint", "").endswith("/status"):
                raise RuntimeError("gateway")
            return inner(**kwargs)

        client.call.side_effect = _call
        parsed = _get_run(client)
        assert parsed["status_display"] == "Completed"
        assert "status_check" not in parsed


class TestStaleRunningRefresh:
    def test_exit_status_set_while_running_forces_one_sync(self):
        client = _client(
            list_status="Running",
            status={"runStatus": "Running", "lastStatusCheckAt": FRESH,
                    "nextflowExitStatus": 0},
            after_sync={"runStatus": "Completed", "lastStatusCheckAt": _iso(NOW),
                        "nextflowExitStatus": 0})
        parsed = _get_run(client)
        assert len(_sync_calls(client)) == 1
        assert parsed["run"]["status"] == "Completed"
        assert parsed["status_display"] == "Completed"
        assert parsed["status_refreshed"] is True
        assert "refreshed" in parsed["summary"]
        assert parsed["status_check"]["last_checked_at"] == _iso(NOW)

    def test_a_stale_last_check_forces_one_sync(self):
        client = _client(
            list_status="Running",
            status={"runStatus": "Running", "lastStatusCheckAt": STALE},
            after_sync={"runStatus": "Completed", "lastStatusCheckAt": _iso(NOW)})
        parsed = _get_run(client)
        assert len(_sync_calls(client)) == 1
        assert parsed["status_display"] == "Completed"

    def test_a_fresh_check_without_exit_status_does_not_sync(self):
        client = _client(list_status="Running", status={
            "runStatus": "Running", "lastStatusCheckAt": FRESH})
        parsed = _get_run(client)
        assert _sync_calls(client) == []
        assert parsed["status_display"] == "Running"
        assert "status_refreshed" not in parsed

    def test_a_finished_run_is_never_synced(self):
        client = _client(list_status="Completed", synced_status="Completed",
                         status={"runStatus": "Completed",
                                 "lastStatusCheckAt": STALE,
                                 "nextflowExitStatus": 0})
        _get_run(client)
        assert _sync_calls(client) == []

    def test_legacy_running_status_is_active_too(self):
        client = _client(
            list_status="NextRun", synced_status="NextSuc",
            status={"runStatus": "NextRun", "nextflowExitStatus": 0},
            after_sync={"runStatus": "NextSuc", "nextflowExitStatus": 0})
        parsed = _get_run(client)
        assert len(_sync_calls(client)) == 1
        assert parsed["status_display"] == "Completed"

    def test_still_running_after_sync_is_reported_not_retried(self):
        client = _client(
            list_status="Running", synced_status="Running",
            status={"runStatus": "Running", "lastStatusCheckAt": STALE},
            after_sync={"runStatus": "Running", "lastStatusCheckAt": STALE})
        parsed = _get_run(client)
        assert len(_sync_calls(client)) == 1
        assert parsed["status_display"] == "Running"
        assert parsed["status_refreshed"] is True

    def test_a_failed_sync_keeps_the_old_status_and_says_so(self):
        client = _client(
            list_status="Running",
            status={"runStatus": "Running", "nextflowExitStatus": 0},
            sync_error=RuntimeError("403 forbidden"))
        parsed = _get_run(client)
        assert len(_sync_calls(client)) == 1
        assert parsed["status_display"] == "Running"
        assert "status_refreshed" not in parsed
        assert "403" in parsed["status_refresh_error"]

    def test_unparseable_check_time_does_not_sync(self):
        client = _client(list_status="Running", status={
            "runStatus": "Running", "lastStatusCheckAt": "not a date"})
        _get_run(client)
        assert _sync_calls(client) == []


class TestWatchRunRefresh:
    def test_watch_run_refreshes_a_finished_run_and_stops_check_again(self):
        client = _client(
            list_status="Running",
            status={"runStatus": "Running", "nextflowExitStatus": 0},
            after_sync={"runStatus": "Completed", "nextflowExitStatus": 0,
                        "lastStatusCheckAt": _iso(NOW)})
        parsed = _watch(client)
        assert len(_sync_calls(client)) == 1
        assert parsed["data"]["raw_status"] == "Completed"
        assert parsed["data"]["check_again"] is False
        assert parsed["data"]["status_refreshed"] is True
        assert parsed["data"]["status_check"]["nextflow_exit_status"] == 0
        assert "refreshed" in parsed["summary"]

    def test_watch_run_without_evidence_does_not_sync(self):
        client = _client(list_status="Running", status={"runStatus": "Running"})
        parsed = _watch(client)
        assert _sync_calls(client) == []
        assert parsed["data"]["check_again"] is True
        assert "status_check" not in parsed["data"]
