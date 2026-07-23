## Task 2: `get_run_log` tool — surface failure logs in chat

**Files:**
- Modify: `src/foundry_mcp/server.py` — update the `.utils` import; add `_LOG_PRIORITY` + `_pick_diagnostic_log()`; add the `get_run_log` tool in the "Run Management Tools" section (after `get_run`, before the "Run Execution Tools" banner at line ~744).
- Test: `test/test_run_logs.py` (new)

**Interfaces:**
- Consumes: `envelope`, `tail_text` from Task 1; `get_client()` and `via_client.call(...)`.
- Produces:
  - `_pick_diagnostic_log(logs: list) -> tuple` — given a list of `{"name","content"}` dicts, returns `(name, content)` of the most useful non-empty log by `_LOG_PRIORITY`, else the first non-empty, else `(None, None)`.
  - `get_run_log(run_id: str, attempt_id: int = None) -> str` MCP tool.

- [ ] **Step 1: Update the utils import in `server.py`**

Change the existing import line (currently line ~37):

```python
from .utils import serialize_response, MCP_TOKEN_PREFIX, remove_none
```

to:

```python
from .utils import serialize_response, MCP_TOKEN_PREFIX, remove_none, envelope, tail_text
```

- [ ] **Step 2: Write the failing tests**

Create `test/test_run_logs.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest test/test_run_logs.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'get_run_log'`.

- [ ] **Step 4: Implement the helper + tool**

In `src/foundry_mcp/server.py`, immediately after the `get_run` tool (before the `# Run Execution Tools` banner around line 744), add:

```python
# Log file names, ordered most→least useful for diagnosing a failed run.
_LOG_PRIORITY = [
    ".command.err", "err.log", ".command.log", "log.txt",
    ".nextflow.log", "serverlog.txt",
]


def _pick_diagnostic_log(logs):
    """From a list of {"name","content"} dicts, return (name, content) of the
    most useful non-empty log for diagnosis, or (None, None) if all are empty."""
    by_name = {
        entry.get("name"): (entry.get("content") or "")
        for entry in logs
        if isinstance(entry, dict)
    }
    for name in _LOG_PRIORITY:
        if by_name.get(name, "").strip():
            return name, by_name[name]
    for name, content in by_name.items():
        if content.strip():
            return name, content
    return None, None


@mcp.tool()
def get_run_log(run_id: str, attempt_id: int = None) -> str:
    """
    Show why a run is in its current state by returning its execution log.
    Use this whenever a run's status is Failed (Error/NextErr) or the user asks
    "why did it fail / what happened". Returns a plain-language summary plus the
    tail of the most relevant log (.command.err / Nextflow). Pair with get_run
    (status) and get_run_details (the settings that produced it).
    """
    try:
        via_client = get_client()
        params = {"attemptId": attempt_id} if attempt_id else None
        logger.info(
            f"Fetching logs for run {run_id}"
            + (f" attempt {attempt_id}" if attempt_id else "")
        )
        logs = via_client.call(
            method="GET", endpoint=f"/api/v1/run/{run_id}/logs", params=params
        )
        if isinstance(logs, dict) and "logs" in logs:
            logs = logs["logs"]
        if not isinstance(logs, list):
            logs = []

        name, content = _pick_diagnostic_log(logs)
        if not name:
            result = envelope(
                summary=(
                    f"No log output is available yet for run {run_id}. If it is "
                    f"still starting or running on a cluster, logs may not have "
                    f"synced — try again shortly."
                ),
                data={"logs": []},
                next_steps=[f"Check status with get_run(run_id='{run_id}')."],
            )
            return json.dumps(result, indent=2)

        result = envelope(
            summary=(
                f"Showing the tail of '{name}' for run {run_id} (the most "
                f"relevant log). Read the last lines for the error or the "
                f"completion message."
            ),
            data={
                "log_name": name,
                "log_tail": tail_text(content),
                "available_logs": [
                    entry.get("name") for entry in logs if isinstance(entry, dict)
                ],
            },
            next_steps=[
                f"If it failed, get_run_details(run_id='{run_id}') shows the "
                f"inputs/params that caused it.",
                "Fix the cause, then re-launch with "
                "initiate_run(run_type='resumerun') to reuse completed steps.",
            ],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error fetching logs for run {run_id}: {e}")
        return json.dumps({"error": str(e)})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest test/test_run_logs.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest test/ -q`
Expected: all green (previous 15 + Task 1 + Task 2 tests).

- [ ] **Step 7: Commit**

```bash
git add src/foundry_mcp/server.py test/test_run_logs.py
git commit -m "feat(mcp): add get_run_log tool to surface failure logs in chat"
```

---

