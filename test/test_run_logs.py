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


# ---------------------------------------------------------------------------
# Failed tasks first. On a resumed or failed run the picked log is often the
# launcher script, not the failing task. The logs endpoint returns no task work
# directory, so each failed task's .command.err tail comes from the
# "Command error:" section Nextflow writes into its own log for that task.
# ---------------------------------------------------------------------------

_TRACE_HEADER = "name\tstatus\thash\texit"


def _trace(*rows):
    return "\n".join([_TRACE_HEADER, *(
        f"{name}\t{status}\tab/123456\t{exit_code}" for name, status, exit_code in rows
    )])


def _nf_error_block(task, err_lines, work_dir="/scratch/work/ab/123456"):
    err = "\n".join(f"  {line}" for line in err_lines)
    return (
        f"ERROR ~ Error executing process > '{task}'\n\n"
        f"Caused by:\n  Process `{task}` terminated with an error exit status (1)\n\n"
        f"Command executed:\n\n  run_tool --in x\n\n"
        f"Command exit status:\n  1\n\n"
        f"Command output:\n  (empty)\n\n"
        f"Command error:\n{err}\n\n"
        f"Work dir:\n  {work_dir}\n\n"
        f"Tip: view the complete command output by changing to the process work dir\n"
    )


_LAUNCHER = "#!/bin/bash\n" + "\n".join(f"echo launcher line {i}" for i in range(40))


class TestGetRunLogFailedTasks:
    def _run(self, logs):
        client = _client(logs)
        with patch.object(server, "get_client", return_value=client):
            return json.loads(server.get_run_log("7")), client

    def test_failed_task_names_and_error_tail_come_first(self):
        trace = _trace(("ALIGN (s1)", "COMPLETED", "0"), ("COUNT (s1)", "FAILED", "1"))
        nf_log = "N E X T F L O W\n" + _nf_error_block(
            "COUNT (s1)", ["Traceback (most recent call last):", "KeyError: 'gene_id'"])
        parsed, client = self._run([
            {"name": "trace.txt", "content": trace},
            {"name": "log.txt", "content": nf_log},
            {"name": ".command.err", "content": _LAUNCHER},
        ])
        client.call.assert_called_once()  # no extra requests
        assert parsed["summary"].startswith("Failed task: COUNT (s1)")
        assert list(parsed["data"])[0] == "failed_tasks"
        task = parsed["data"]["failed_tasks"][0]
        assert task["name"] == "COUNT (s1)"
        assert task["exit"] == "1"
        assert task["work_dir"] == "/scratch/work/ab/123456"
        assert task["command_err_tail"] == (
            "Traceback (most recent call last):\nKeyError: 'gene_id'")
        # Today's log tail is still there, unchanged.
        assert parsed["data"]["log_name"] == ".command.err"
        assert parsed["data"]["log_tail"] == _LAUNCHER

    def test_error_tail_is_capped_to_the_last_lines(self):
        lines = [f"err {i}" for i in range(60)]
        trace = _trace(("COUNT", "FAILED", "1"))
        parsed, _ = self._run([
            {"name": "trace.txt", "content": trace},
            {"name": ".nextflow.log", "content": _nf_error_block("COUNT", lines)},
        ])
        tail = parsed["data"]["failed_tasks"][0]["command_err_tail"].splitlines()
        assert len(tail) == server._RUN_LOG_ERR_TAIL_LINES
        assert tail[-1] == "err 59"

    def test_caps_the_number_of_tasks_and_says_how_many_were_cut(self):
        n = server._RUN_LOG_MAX_FAILED_TASKS + 3
        trace = _trace(*[(f"T{i}", "FAILED", "1") for i in range(n)])
        parsed, _ = self._run([{"name": "trace.txt", "content": trace},
                               {"name": "log.txt", "content": _LAUNCHER}])
        data = parsed["data"]
        assert len(data["failed_tasks"]) == server._RUN_LOG_MAX_FAILED_TASKS
        assert data["failed_tasks_total"] == n
        assert data["failed_tasks_omitted"] == 3
        assert "3 more" in parsed["summary"]

    def test_task_without_an_error_block_is_reported_and_others_continue(self):
        trace = _trace(("A", "FAILED", "1"), ("B", "FAILED", "137"))
        parsed, _ = self._run([
            {"name": "trace.txt", "content": trace},
            {"name": "log.txt", "content": _nf_error_block("B", ["Killed"])},
        ])
        a, b = parsed["data"]["failed_tasks"]
        assert "command_err_tail" not in a
        assert "not found" in a["command_err_unavailable"]
        assert b["command_err_tail"] == "Killed"

    def test_uses_the_latest_error_block_for_a_retried_task(self):
        trace = _trace(("A", "FAILED", "1"))
        nf_log = _nf_error_block("A", ["first try"]) + _nf_error_block("A", ["second try"])
        parsed, _ = self._run([{"name": "trace.txt", "content": trace},
                               {"name": "log.txt", "content": nf_log}])
        assert parsed["data"]["failed_tasks"][0]["command_err_tail"] == "second try"

    def test_one_task_extraction_error_does_not_break_the_reply(self):
        trace = _trace(("A", "FAILED", "1"), ("B", "FAILED", "1"))
        logs = [{"name": "trace.txt", "content": trace},
                {"name": "log.txt", "content": _nf_error_block("B", ["bad input"])}]
        real = server._failed_task_err_tail

        def flaky(logs_, name):
            if name == "A":
                raise RuntimeError("unreadable")
            return real(logs_, name)

        with patch.object(server, "_failed_task_err_tail", side_effect=flaky):
            parsed, _ = self._run(logs)
        a, b = parsed["data"]["failed_tasks"]
        assert "unreadable" in a["command_err_unavailable"]
        assert b["command_err_tail"] == "bad input"
        assert parsed["data"]["log_name"]

    def test_no_failed_task_leaves_the_reply_as_today(self):
        trace = _trace(("A", "COMPLETED", "0"))
        logs = [{"name": "trace.txt", "content": trace},
                {"name": ".command.err", "content": "Traceback\nboom\n"}]
        parsed, _ = self._run(logs)
        assert "failed_tasks" not in parsed["data"]
        assert parsed["summary"].startswith("Showing the tail of '.command.err'")

    def test_no_trace_leaves_the_reply_as_today(self):
        parsed, _ = self._run([{"name": ".command.err", "content": "Traceback\nboom\n"}])
        assert "failed_tasks" not in parsed["data"]
        assert parsed["summary"].startswith("Showing the tail of '.command.err'")
