## Task 3: Human-readable run status on `get_run`

**Files:**
- Modify: `src/foundry_mcp/server.py` — add `_RUN_STATUS_DISPLAY` + `_human_run_status()` near the run tools; enrich `get_run` result before its final return; add a `get_run_log` chaining hint to `get_run`'s docstring.
- Test: `test/test_run_status.py` (new)

**Interfaces:**
- Consumes: nothing new (plain dict mutation on the existing `result`).
- Produces:
  - `_human_run_status(status: str) -> str` — maps a raw `RunStatus` string to a bench-friendly label: `Failed`, `Completed`, `Running`, `Initializing`, `Terminated`, `Not submitted`, or `Connecting`.
  - `get_run` output gains `status_display` (str) and, for resolved single runs, `summary`/`next_steps`.

- [ ] **Step 1: Write the failing tests**

Create `test/test_run_status.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test/test_run_status.py -v`
Expected: FAIL with `KeyError: 'status_display'`.

- [ ] **Step 3: Add the status helper**

In `src/foundry_mcp/server.py`, just above the `get_run` tool (line ~651), add:

```python
# Bench-friendly labels for raw RunStatus values (mirrors the frontend's
# getRunStatusDisplayText, but says "Failed" instead of "Error" for clarity).
_RUN_STATUS_DISPLAY = {
    "NextErr": "Failed",
    "Error": "Failed",
    "NextSuc": "Completed",
    "NextRun": "Running",
    "init": "Initializing",
    "Waiting": "Initializing",
    "Terminated": "Terminated",
    "NotSubmitted": "Not submitted",
    "Aborted": "Connecting",
}


def _human_run_status(status):
    """Map a raw RunStatus string to a plain-language label for scientists."""
    return _RUN_STATUS_DISPLAY.get(status, "Connecting")
```

- [ ] **Step 4: Enrich the `get_run` result**

In `get_run`, replace the final `return json.dumps(result, indent=2)` (currently line ~738, the one after the `include_reports` block) with:

```python
        run_obj = result.get("run")
        if isinstance(run_obj, dict):
            display = _human_run_status(run_obj.get("status"))
            result["status_display"] = display
            name = run_obj.get("name")
            if display == "Failed":
                result["summary"] = (
                    f"Run '{name}' ({run_id}) failed. Fetch the log to see why."
                )
                result["next_steps"] = [
                    f"get_run_log(run_id='{run_id}') to see the error."
                ]
            elif display == "Running":
                result["summary"] = f"Run '{name}' ({run_id}) is still running."
                result["next_steps"] = [
                    f"Check again later, or get_run_log(run_id='{run_id}') "
                    f"to watch progress."
                ]
            elif display == "Completed":
                result["summary"] = (
                    f"Run '{name}' ({run_id}) completed successfully."
                )
                result["next_steps"] = [
                    f"get_run(run_id='{run_id}', include_reports=True) to see "
                    f"the result files."
                ]
            else:
                result["summary"] = f"Run '{name}' ({run_id}) status: {display}."

        return json.dumps(result, indent=2)
```

(Only the `id`/`exact` branches set `result["run"]`; the early-returning `fuzzy`/`none` branches are untouched.)

- [ ] **Step 5: Polish the `get_run` docstring (X2)**

In `get_run`'s docstring, add a final sentence so the model chains to logs on failure. Change the last docstring line to:

```python
    The returned run ID can be used with report tools (e.g., fetch_report, list_files, download_file).
    If the run failed, call get_run_log(run_id) to see the error.
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest test/test_run_status.py test/test_run_execution.py -v`
Expected: PASS (new status tests green; existing run-execution tests unaffected).

- [ ] **Step 7: Commit**

```bash
git add src/foundry_mcp/server.py test/test_run_status.py
git commit -m "feat(mcp): add human-readable status + next steps to get_run"
```

---

