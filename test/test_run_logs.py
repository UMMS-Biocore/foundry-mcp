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


class TestPicksSubstantiveLogOnClusterRuns:
    """Real LSF cluster runs (verified against staging runs 12193/12194) return
    NO .command.* files at all, and `err.log` is a fixed 42-byte job-starter
    stub. The composite `log.txt` and the Nextflow debug log carry the actual
    failure, so a stub must never outrank them."""

    # verbatim from staging run 12193
    _ERR_LOG_STUB = "JOB_STARTER: slots=1 (LSB_DJOB_NUMPROC=1)\n"

    def _cluster_logs(self):
        return [
            {"name": "trace.txt", "content": "task\tstatus\n" * 40},
            {"name": "timeline.html", "content": "<html>" + "x" * 5000},
            {"name": "report.html", "content": "<html>" + "y" * 9000},
            {"name": "log.txt", "content": "Started\n" * 50 + "ERROR: DESeq2 step failed\n##Exit status: 1\n"},
            {"name": "err.log", "content": self._ERR_LOG_STUB},
            {"name": ".nextflow.log", "content": "DEBUG nextflow\n" * 200},
            {"name": "nextflow.nf", "content": "process foo {}\n" * 100},
        ]

    def test_prefers_composite_log_over_42_byte_err_stub(self):
        client = _client(self._cluster_logs())
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_log("12193"))
        assert parsed["data"]["log_name"] == "log.txt"
        assert "DESeq2 step failed" in parsed["data"]["log_tail"]

    def test_still_prefers_command_err_when_it_actually_exists(self):
        logs = self._cluster_logs() + [
            {"name": ".command.err", "content": "STAR: genome index not found\n" * 10}
        ]
        client = _client(logs)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_log("1"))
        assert parsed["data"]["log_name"] == ".command.err"

    def test_falls_back_to_stub_only_when_nothing_substantive_exists(self):
        client = _client([{"name": "err.log", "content": self._ERR_LOG_STUB}])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_log("1"))
        # better a stub than "no logs available"
        assert parsed["data"]["log_name"] == "err.log"
        assert "JOB_STARTER" in parsed["data"]["log_tail"]

    def test_never_returns_html_or_pipeline_source_as_the_log(self):
        client = _client([
            {"name": "report.html", "content": "<html>" + "z" * 20000},
            {"name": "nextflow.nf", "content": "process x {}\n" * 500},
            {"name": "err.log", "content": self._ERR_LOG_STUB},
        ])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_log("1"))
        assert parsed["data"]["log_name"] == "err.log"
