"""Tests for watch_run, and for get_run_log's handling of purged logs.

Grounding, live on staging 2026-07-24:
- `GET /v1/run/{id}/status` returns only `{runStatus, enableTerminate}` — a flat
  label with no per-process detail. Per-task progress lives in `trace.txt`,
  which the logs endpoint returns: a TSV with name/status/exit/duration columns.
- **Every failed run sampled (8 of 8) had a 4-character `log.txt` and nothing
  else.** Their run directories are purged. get_run_log nevertheless said logs
  "may not have synced — try again shortly", which for a run that failed a month
  ago is actively misleading.
"""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server

TRACE_HEADER = ("name\tstatus\thash\ttask_id\tnative_id\texit\tattempt\tsubmit\t"
                "start\tcomplete\tduration\trealtime")


def _task(name, status, exit_code="0", duration="11.7s"):
    return (f"{name}\t{status}\tab/123456\t1\t1645\t{exit_code}\t1\t"
            f"2026-07-15 17:28:21\t2026-07-15 17:28:28\t2026-07-15 17:28:33\t"
            f"{duration}\t8.0s")


def _trace(*tasks):
    return "\n".join([TRACE_HEADER, *tasks])


RUNNING_TRACE = _trace(
    _task("Adapter_Trimmer_Quality_Module_FastQC (1)", "COMPLETED"),
    _task("Adapter_Trimmer_Quality_Module_FastQC (2)", "COMPLETED"),
    _task("STAR_Module_Map_STAR (1)", "COMPLETED"),
    _task("DE_module_RSEM", "RUNNING", exit_code="-"),
)

FAILED_TRACE = _trace(
    _task("Adapter_Trimmer_Quality_Module_FastQC (1)", "COMPLETED"),
    _task("STAR_Module_Map_STAR (1)", "CACHED"),
    _task("DE_module_RSEM", "FAILED", exit_code="1"),
)


def _client(status="NextRun", logs=None):
    c = MagicMock()

    def _call(**kwargs):
        endpoint = kwargs.get("endpoint", "")
        if endpoint.endswith("/status"):
            return {"runStatus": status, "enableTerminate": True}
        if endpoint.endswith("/logs"):
            return logs if logs is not None else []
        return {}

    c.call.side_effect = _call
    return c


def _watch(**kwargs):
    client = _client(**kwargs)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.watch_run("12194")), client


class TestWatchRun:
    def test_reports_task_progress_not_just_a_status_label(self):
        """The status endpoint only gives a flat label. Progress comes from the
        trace, and it is what a scientist actually asks for."""
        parsed, _ = _watch(logs=[{"name": "trace.txt", "content": RUNNING_TRACE}])
        p = parsed["data"]["progress"]
        assert p["total_tasks"] == 4
        assert p["finished"] == 3
        assert p["running"] == 1

    def test_names_what_is_running_right_now(self):
        parsed, _ = _watch(logs=[{"name": "trace.txt", "content": RUNNING_TRACE}])
        assert "DE_module_RSEM" in parsed["data"]["progress"]["running_now"]

    def test_counts_a_cached_task_as_finished(self):
        """CACHED means Nextflow reused a previous result — it is done, and
        counting it as outstanding would understate progress badly."""
        parsed, _ = _watch(status="NextErr",
                           logs=[{"name": "trace.txt", "content": FAILED_TRACE}])
        assert parsed["data"]["progress"]["finished"] == 2

    def test_identifies_the_failing_step_with_its_exit_code(self):
        parsed, _ = _watch(status="NextErr",
                           logs=[{"name": "trace.txt", "content": FAILED_TRACE}])
        failed = parsed["data"]["progress"]["failed_tasks"]
        assert failed[0]["name"] == "DE_module_RSEM"
        assert failed[0]["exit"] == "1"
        assert "DE_module_RSEM" in parsed["summary"]

    def test_uses_a_plain_language_status(self):
        parsed, _ = _watch(status="NextRun",
                           logs=[{"name": "trace.txt", "content": RUNNING_TRACE}])
        assert parsed["data"]["status"] == "Running"

    def test_tells_the_caller_to_check_back_while_running(self):
        parsed, _ = _watch(logs=[{"name": "trace.txt", "content": RUNNING_TRACE}])
        assert parsed["data"]["check_again"] is True
        assert "watch_run" in " ".join(parsed["next_steps"])

    def test_does_not_ask_the_caller_to_check_back_once_finished(self):
        parsed, _ = _watch(status="NextSuc",
                           logs=[{"name": "trace.txt", "content": RUNNING_TRACE}])
        assert parsed["data"]["check_again"] is False
        assert "summarize_results" in " ".join(parsed["next_steps"])

    def test_works_when_no_trace_exists_yet(self):
        """A run that has just started has no trace — report the status rather
        than erroring."""
        parsed, _ = _watch(status="init", logs=[])
        assert parsed["data"]["progress"] is None
        assert parsed["data"]["status"] == "Initializing"
        assert "error" not in parsed

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            assert json.loads(server.watch_run("1")) == {"error": "boom"}


class TestPurgedLogs:
    """get_run_log must not tell someone to wait for logs that will never come."""

    def _log(self, run_id, status, logs):
        client = _client(status=status, logs=logs)
        with patch.object(server, "get_client", return_value=client):
            return json.loads(server.get_run_log(run_id))

    def test_a_finished_run_with_no_logs_says_they_are_gone(self):
        """Live: 8 of 8 failed runs on staging have a 4-char log.txt and nothing
        else — the run directory was purged. Saying "try again shortly" about a
        run that failed a month ago is actively misleading."""
        parsed = self._log("12180", "NextErr", [{"name": "log.txt", "content": "\n\n\n"}])
        text = parsed["summary"].lower()
        assert "no longer" in text or "purged" in text or "cleaned" in text
        assert "try again" not in text
        assert "shortly" not in text

    def test_it_offers_what_can_still_be_done(self):
        parsed = self._log("12180", "NextErr", [{"name": "log.txt", "content": ""}])
        joined = " ".join(parsed["next_steps"])
        assert "get_run_details" in joined or "duplicate_run" in joined

    def test_a_still_running_run_is_still_told_to_wait(self):
        """The original message is right for this case — keep it."""
        parsed = self._log("12345", "NextRun", [])
        assert "shortly" in parsed["summary"].lower()

    def test_a_run_that_never_started_is_not_described_as_purged(self):
        parsed = self._log("12345", "init", [])
        assert "purged" not in parsed["summary"].lower()
