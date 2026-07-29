"""Tests for get_run human status enrichment."""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _search_client(runs):
    c = MagicMock()
    c.call.return_value = {"data": runs}
    return c


class TestGetRunHumanStatus:
    def test_failed_run_gets_failed_display_and_log_next_step(self):
        client = _search_client([{"id": 12219, "name": "UCP1_AAV", "status": "NextErr"}])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run(run_id="12219"))
        assert parsed["status_display"] == "Failed"
        assert "get_run_log" in parsed["next_steps"][0]

    def test_completed_run_points_to_reports(self):
        client = _search_client([{"id": 1, "name": "x", "status": "NextSuc"}])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run(run_id="1"))
        assert parsed["status_display"] == "Completed"
        assert "include_reports" in parsed["next_steps"][0]

    def test_running_run_display(self):
        client = _search_client([{"id": 1, "name": "x", "status": "NextRun"}])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run(run_id="1"))
        assert parsed["status_display"] == "Running"

    def test_unknown_status_falls_back_to_connecting(self):
        client = _search_client([{"id": 1, "name": "x", "status": "Manual"}])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run(run_id="1"))
        assert parsed["status_display"] == "Connecting"

    def test_fuzzy_match_does_not_get_status_display(self):
        client = _search_client(
            [{"id": 1, "name": "not-quite-it", "status": "NextSuc"}]
        )
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run(run_name="nope"))
        assert parsed["match_type"] == "fuzzy"
        assert "status_display" not in parsed

    def test_no_match_does_not_get_status_display(self):
        client = _search_client([])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run(run_name="nope"))
        assert parsed["match_type"] == "none"
        assert "status_display" not in parsed


class TestDualVocabulary:
    """Backend 2.20.0 renames the run status wire values. MCP releases on its own
    cadence and can be pointed at an older backend, so it must read both."""

    def test_display_map_covers_both_vocabularies(self):
        for legacy, canonical in [
            ("NextSuc", "Completed"),
            ("NextErr", "Failed"),
            ("NextRun", "Running"),
            ("init", "Initializing"),
            ("Waiting", "Submitted"),
            ("Error", "LaunchFailed"),
            ("Aborted", "Unreachable"),
        ]:
            assert legacy in server._RUN_STATUS_DISPLAY
            assert canonical in server._RUN_STATUS_DISPLAY
            assert (
                server._RUN_STATUS_DISPLAY[legacy]
                == server._RUN_STATUS_DISPLAY[canonical]
            )

    def test_terminal_statuses_cover_both_vocabularies(self):
        for value in (
            "NextSuc", "NextErr", "Error", "Terminated",
            "Completed", "Failed", "LaunchFailed",
        ):
            assert value in server._TERMINAL_RUN_STATUSES

    def test_canonical_completed_run_points_to_reports(self):
        client = _search_client([{"id": 1, "name": "x", "status": "Completed"}])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run(run_id="1"))
        assert parsed["status_display"] == "Completed"

    def test_canonical_failed_run_points_to_log(self):
        client = _search_client([{"id": 1, "name": "x", "status": "Failed"}])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run(run_id="1"))
        assert parsed["status_display"] == "Failed"
        assert "get_run_log" in parsed["next_steps"][0]
