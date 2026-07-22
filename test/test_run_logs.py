"""Tests for get_run_log."""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _client(call_return):
    c = MagicMock()
    c.call.return_value = call_return
    return c


class TestGetRunLog:
    def test_hits_logs_endpoint_and_tails_command_err(self):
        logs = [
            {"name": "log.txt", "content": "started\nstep1 ok\n"},
            {"name": ".command.err", "content": "Traceback\nSTAR: genome index not found\n"},
        ]
        client = _client(logs)
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_log("12219")
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/run/12219/logs", params=None
        )
        parsed = json.loads(result)
        assert parsed["data"]["log_name"] == ".command.err"
        assert "genome index not found" in parsed["data"]["log_tail"]
        assert parsed["summary"]
        assert parsed["next_steps"]

    def test_passes_attempt_id_as_query(self):
        client = _client([{"name": "err.log", "content": "boom"}])
        with patch.object(server, "get_client", return_value=client):
            server.get_run_log("12219", attempt_id=3)
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/run/12219/logs", params={"attemptId": 3}
        )

    def test_unwraps_dict_logs_key(self):
        client = _client({"logs": [{"name": "err.log", "content": "oops"}]})
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_log("1")
        assert json.loads(result)["data"]["log_name"] == "err.log"

    def test_reports_when_no_logs_available(self):
        client = _client([{"name": "log.txt", "content": ""}])
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_log("1")
        parsed = json.loads(result)
        assert parsed["data"]["logs"] == []
        assert "synced" in parsed["summary"]

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_log("1")
        assert json.loads(result) == {"error": "boom"}

    def test_command_out_outranks_nextflow_log(self):
        logs = [
            {"name": ".nextflow.log", "content": "generic nextflow chatter"},
            {"name": ".command.out", "content": "Killed\nOOM"},
        ]
        client = _client(logs)
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_log("1")
        assert json.loads(result)["data"]["log_name"] == ".command.out"

    def test_fallback_skips_html_and_config_and_nf_artifacts(self):
        # No priority-listed log has content; only non-log artifacts + a
        # genuine (unlisted) log file have content. The fallback must skip
        # the html/nf/config artifacts and surface the real log.
        logs = [
            {"name": "report.html", "content": "<html>big nextflow report</html>"},
            {"name": "timeline.html", "content": "<html>timeline</html>"},
            {"name": "nextflow.nf", "content": "process foo { ... }"},
            {"name": "nextflow.config", "content": "params { ... }"},
            {"name": "some_other.log", "content": "real diagnostic content here"},
        ]
        client = _client(logs)
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_log("1")
        assert json.loads(result)["data"]["log_name"] == "some_other.log"

    def test_available_logs_excludes_nameless_entries(self):
        logs = [
            {"name": "err.log", "content": "boom"},
            {"name": "", "content": "some anonymous blob"},
            {"content": "no name key at all"},
        ]
        client = _client(logs)
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_log("1")
        available = json.loads(result)["data"]["available_logs"]
        assert available == ["err.log"]

    def test_relaunch_next_step_has_confirm_cue_and_run_id(self):
        client = _client([{"name": "err.log", "content": "boom"}])
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_log("12219")
        next_steps = json.loads(result)["next_steps"]
        relaunch_step = next(s for s in next_steps if "resumerun" in s)
        assert "confirming with the user" in relaunch_step
        assert "run_id='12219'" in relaunch_step
