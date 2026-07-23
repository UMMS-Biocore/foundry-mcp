## Task 4: Compact `get_run_details` (with `verbose` escape hatch) + launch advisory

**Files:**
- Modify: `src/foundry_mcp/server.py` — add `_summarize_run_details()`; rework `get_run_details` to `(run_id, verbose=False)`; add a plain cost/time line to `initiate_run`'s docstring (X3).
- Modify: `test/test_run_execution.py` — update the one test that reads top-level `groupId` to pass `verbose=True`.
- Test: `test/test_run_details_summary.py` (new)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `envelope` (Task 1).
- Produces:
  - `_summarize_run_details(details: dict) -> dict` with keys: `pipeline{name,version,id}`, `project{name,id}`, `permission`, `groupId`, `sample_inputs[]` (`vmetaCollection` inputs, as `{name,dataset}`), `settings[]` (non-path scalar inputs, as `{name,value}`), `reference_paths[]` (names of `/`-prefixed path inputs), `process_option_groups` (int).
  - `get_run_details(run_id: str, verbose: bool = False) -> str` — `verbose=True` returns the full raw details blob (unchanged legacy behavior); default returns the compact envelope.

- [ ] **Step 1: Update the existing test to use `verbose=True`**

In `test/test_run_execution.py`, in `TestGetRunDetails.test_hits_details_endpoint_and_returns_json`, change:

```python
            result = server.get_run_details("123")
```

to:

```python
            result = server.get_run_details("123", verbose=True)
```

(The `test_returns_error_json_on_exception` test is unchanged — the error path still returns `{"error": ...}`.)

- [ ] **Step 2: Write the failing tests for the compact path**

Create `test/test_run_details_summary.py`:

```python
"""Tests for the compact get_run_details summary."""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _client(call_return):
    c = MagicMock()
    c.call.return_value = call_return
    return c


_DETAILS = {
    "mainPipeline": {"id": 1539, "name": "PMM RNASeq DE Pipeline", "version": "2.0.0"},
    "project": {"id": 2664, "name": "BC_RNAseq"},
    "permission": 15,
    "groupId": 10,
    "inputs": [
        {"name": "reads", "type": "vmetaCollection", "value": "AAV_UCP1"},
        {"name": "genome", "type": "input", "value": "/pi/x/genome.fa"},
        {"name": "run_STAR", "type": "input", "value": "no"},
    ],
    "processOptions": {"281_32": {}, "289_12": {}},
}


class TestGetRunDetailsCompact:
    def test_default_returns_compact_summary_envelope(self):
        client = _client(_DETAILS)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("123"))
        assert list(parsed.keys())[0] == "summary"
        assert parsed["data"]["pipeline"]["name"] == "PMM RNASeq DE Pipeline"
        assert parsed["data"]["sample_inputs"][0]["dataset"] == "AAV_UCP1"
        assert parsed["data"]["settings"][0]["name"] == "run_STAR"
        assert parsed["data"]["reference_paths"] == ["genome"]
        assert parsed["data"]["process_option_groups"] == 2

    def test_default_next_steps_have_verbose_and_hpc_advisory(self):
        client = _client(_DETAILS)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("123"))
        assert any("verbose=True" in s for s in parsed["next_steps"])
        assert any("HPC" in s for s in parsed["next_steps"])

    def test_verbose_returns_full_blob(self):
        client = _client(_DETAILS)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("123", verbose=True))
        assert parsed["groupId"] == 10
        assert "summary" not in parsed
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest test/test_run_details_summary.py -v`
Expected: FAIL — default call currently returns the raw blob, so `list(parsed.keys())[0] == "summary"` fails (`get_run_details` has no `verbose` param yet → `TypeError` on the `verbose=True` test).

- [ ] **Step 4: Add the summariser and rework the tool**

In `src/foundry_mcp/server.py`, replace the entire existing `get_run_details` function (lines ~749–765) with:

```python
def _summarize_run_details(details):
    """Distill a run's full details blob into a compact, plain-language summary
    a bench scientist can read without wading through processOptions."""
    pipeline = details.get("mainPipeline") or {}
    project = details.get("project") or {}
    inputs = details.get("inputs") or []
    proc_opts = details.get("processOptions") or {}

    sample_inputs, settings, reference_paths = [], [], []
    for inp in inputs:
        if not isinstance(inp, dict):
            continue
        name, value = inp.get("name"), inp.get("value")
        if inp.get("type") == "vmetaCollection":
            sample_inputs.append({"name": name, "dataset": value})
        elif isinstance(value, str) and value.startswith("/"):
            reference_paths.append(name)
        else:
            settings.append({"name": name, "value": value})

    return {
        "pipeline": {
            "name": pipeline.get("name"),
            "version": pipeline.get("version"),
            "id": pipeline.get("id"),
        },
        "project": {"name": project.get("name"), "id": project.get("id")},
        "permission": details.get("permission"),
        "groupId": details.get("groupId"),
        "sample_inputs": sample_inputs,
        "settings": settings,
        "reference_paths": reference_paths,
        "process_option_groups": len(proc_opts),
    }


@mcp.tool()
def get_run_details(run_id: str, verbose: bool = False) -> str:
    """
    Show a run's configuration. By default returns a compact, plain-language
    summary (pipeline, samples, key settings, count of process-option groups).
    Pass verbose=True to get the FULL editable inputs[] and processOptions{}
    needed to build an update_run body — do this before duplicate_run/update_run.
    get_run shows a run's status; this shows the settings that produced it.
    """
    try:
        via_client = get_client()
        details = via_client.call(
            method="GET", endpoint=f"/api/v1/run/{run_id}/details"
        )
        if verbose:
            return json.dumps(details, indent=2)

        summary_data = _summarize_run_details(details)
        pipeline = summary_data["pipeline"]
        result = envelope(
            summary=(
                f"Run {run_id} uses pipeline '{pipeline['name']}' "
                f"(v{pipeline['version']}) with {len(summary_data['settings'])} "
                f"settings and {summary_data['process_option_groups']} "
                f"process-option groups."
            ),
            data=summary_data,
            next_steps=[
                f"To edit or re-launch, call get_run_details(run_id='{run_id}', "
                f"verbose=True) for the full editable config, then update_run.",
                "Launching a run uses HPC compute — confirm with the user "
                "before initiate_run.",
            ],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting run details for {run_id}: {e}")
        return json.dumps({"error": str(e)})
```

- [ ] **Step 5: Add the plain cost/time line to `initiate_run` (X3)**

In `initiate_run`'s docstring (line ~882), change the sentence `This LAUNCHES compute; confirm with the user before calling.` to:

```python
    params. Returns status, runUUID, localRunDir. This LAUNCHES real HPC compute
    (it can take minutes to hours and consumes cluster time) — always confirm
    with the user before calling.
```

- [ ] **Step 6: Run the targeted tests to verify they pass**

Run: `python -m pytest test/test_run_details_summary.py test/test_run_execution.py -v`
Expected: PASS (compact + verbose tests green; the updated existing test green).

- [ ] **Step 7: Update the CHANGELOG**

In `CHANGELOG.md`, under `## [Unreleased]` → `### Added`, append:

```markdown
- **Chat-friendly run journey (Phase 0)** — `get_run_log` surfaces execution
  logs (`.command.err`/Nextflow tail) so failed runs are diagnosable from chat;
  `get_run` now returns a plain-language `status_display` + `next_steps`;
  `get_run_details` returns a compact summary by default (`verbose=True` for the
  full editable config); all use a shared `{summary, next_steps, data}` envelope.
```

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest test/ -q`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/foundry_mcp/server.py test/test_run_details_summary.py test/test_run_execution.py CHANGELOG.md
git commit -m "feat(mcp): compact get_run_details with verbose escape hatch + launch advisory"
```

---

## Self-Review

**1. Spec coverage (Phase 0 items from the design doc):**
- M1 (`get_run_log` / diagnosis) → Task 2. ✅ (Diagnosis is the prioritized-log tail + next_steps; deeper LLM diagnosis is Phase 4, as the spec states.)
- M2 (human status) → Task 3. ✅
- R2 (reshape `get_run_details`) → Task 4. ✅ (compact by default, `verbose=True` escape hatch preserves the update_run flow.)
- X4 (response envelope) → Task 1 helper, applied in Tasks 2 & 4. ✅
- X2 (description overhaul) → folded into Tasks 2/3/4 docstrings (get_run_log, get_run, get_run_details, initiate_run). ✅
- X3 (launch guardrail) → Task 4 (initiate_run docstring cost/time line + the "confirm before initiate_run" next_step in get_run_details). ✅
- Backend log-endpoint dependency → resolved: `/v1/run/{id}/logs` already exists (`allowGuestAccess: true`), no backend change. ✅

**2. Placeholder scan:** No TBD/TODO/"handle errors"/"similar to". Every code and test step shows full content. ✅

**3. Type consistency:**
- `envelope(summary, data, next_steps)` / `tail_text(text, max_lines, max_chars)` — defined Task 1, called identically in Tasks 2 & 4. ✅
- `_pick_diagnostic_log` returns `(name, content)`; `get_run_log` consumes both. ✅
- `get_run_log(run_id, attempt_id=None)` → `params={"attemptId": attempt_id}` matches the backend query schema key `attemptId`. ✅
- `_summarize_run_details` output keys match every assertion in `test_run_details_summary.py` (`pipeline.name`, `sample_inputs[].dataset`, `settings[].name`, `reference_paths`, `process_option_groups`). ✅
- `_human_run_status` labels match `test_run_status.py` (`Failed`/`Completed`/`Running`/`Connecting`). ✅

**Note on the one behavior change:** `get_run_details` default output shape changes (compact envelope vs raw blob). This is intentional and called out; the only consumer that needs the raw blob (update_run body building) is directed to `verbose=True`, and the existing test is updated in Task 4 Step 1.

---

## After the plan

These four commits land in the `foundry-mcp` submodule. To take effect in a running stack, the parent repo's `./mcp` submodule pointer must be bumped and the MCP container rebuilt/redeployed (see [[local-install-deploy-mcp-run-tools]] for local; a submodule-pointer bump + redeploy for staging/prod). That deploy step is out of scope for this plan.
