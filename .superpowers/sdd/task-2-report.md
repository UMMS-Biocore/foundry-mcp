# Task 2 Report: `get_run_log` MCP tool

## Summary

Implemented the `get_run_log` MCP tool exactly per the task brief, following TDD.
It hits `GET /api/v1/run/{run_id}/logs`, picks the most diagnostically useful
non-empty log (via a new `_LOG_PRIORITY` list + `_pick_diagnostic_log()` helper),
tails it with `tail_text()`, and wraps the result in `envelope()`.

## What was implemented

1. **`src/foundry_mcp/server.py`**
   - Updated the `.utils` import (line 37) to also pull in `envelope, tail_text`:
     ```python
     from .utils import serialize_response, MCP_TOKEN_PREFIX, remove_none, envelope, tail_text
     ```
   - Added, immediately after `get_run` and before the `# Run Execution Tools`
     banner (previously at line 744, now shifted by +87 lines):
     - Module-level `_LOG_PRIORITY` list: `.command.err`, `err.log`,
       `.command.log`, `log.txt`, `.nextflow.log`, `serverlog.txt`.
     - `_pick_diagnostic_log(logs)` — builds a `name -> content` map from the
       `{"name","content"}` dicts, walks `_LOG_PRIORITY` for the first non-empty
       match, falls back to the first non-empty log in the list, else `(None, None)`.
     - `@mcp.tool() def get_run_log(run_id: str, attempt_id: int = None) -> str` —
       calls `get_client()`, builds `params = {"attemptId": attempt_id} if attempt_id else None`,
       calls `via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/logs", params=params)`,
       unwraps a `{"logs": [...]}` dict shape if present, coerces non-list results
       to `[]`, picks the diagnostic log, and returns either a "no logs yet"
       envelope or a tail-of-log envelope with `log_name`, `log_tail`,
       `available_logs`, and two `next_steps` (point to `get_run_details`, and
       to `initiate_run(run_type='resumerun')`). Exceptions are logged and
       returned as `json.dumps({"error": str(e)})`.
   - Code added verbatim from the brief — no deviations.

2. **`test/test_run_logs.py`** (new) — copied verbatim from the brief's Step 2,
   5 tests covering: endpoint/params call shape, `attemptId` query passthrough,
   unwrapping a `{"logs": [...]}` dict response, the "no logs available yet"
   path, and exception → `{"error": ...}`.

## TDD evidence

**RED** — `python -m pytest test/test_run_logs.py -v` (before implementation):

```
FAILED test/test_run_logs.py::TestGetRunLog::test_hits_logs_endpoint_and_tails_command_err
FAILED test/test_run_logs.py::TestGetRunLog::test_passes_attempt_id_as_query
FAILED test/test_run_logs.py::TestGetRunLog::test_unwraps_dict_logs_key
FAILED test/test_run_logs.py::TestGetRunLog::test_reports_when_no_logs_available
FAILED test/test_run_logs.py::TestGetRunLog::test_returns_error_json_on_exception
======================== 5 failed, 64 warnings in 1.39s ========================
```
Each failure was `AttributeError: module 'src.foundry_mcp.server' has no attribute 'get_run_log'`
— exactly as predicted by the brief (Step 3), confirming the tests actually
exercise the not-yet-existent tool.

**GREEN** — `python -m pytest test/test_run_logs.py -v` (after implementation):

```
test/test_run_logs.py::TestGetRunLog::test_hits_logs_endpoint_and_tails_command_err PASSED [ 20%]
test/test_run_logs.py::TestGetRunLog::test_passes_attempt_id_as_query PASSED [ 40%]
test/test_run_logs.py::TestGetRunLog::test_unwraps_dict_logs_key PASSED [ 60%]
test/test_run_logs.py::TestGetRunLog::test_reports_when_no_logs_available PASSED [ 80%]
test/test_run_logs.py::TestGetRunLog::test_returns_error_json_on_exception PASSED [100%]
======================== 5 passed, 64 warnings in 1.32s ========================
```

**Full suite** — `python -m pytest test/ -q`:

```
FAILED test/test_client.py::test_get_client_initializes_once - ValueError: In...
FAILED test/test_client.py::test_get_client_configures_auth_token - ValueErro...
FAILED test/test_client.py::test_get_client_missing_credentials - AttributeEr...
FAILED test/test_client.py::test_reset_clients_clears_cache - ValueError: Inv...
FAILED test/test_client.py::test_get_client_caches_by_credentials - ValueErro...
5 failed, 91 passed, 2 skipped, 64 warnings in 1.21s
```

Baseline (measured before touching any files, for comparison): `5 failed, 86 passed, 2 skipped`.
Delta: exactly +5 passed (the new tests), same 5 pre-existing `test_client.py`
failures (credential/env-mocking issue predating this branch, out of scope per
the task instructions), no new failures, no change in skip count.

## Files changed

- `/Users/alper/ws/Archive/docker_work/viascientific/foundry-mcp-phase0/src/foundry_mcp/server.py`
  (modified: import line + new `_LOG_PRIORITY`, `_pick_diagnostic_log`, `get_run_log`)
- `/Users/alper/ws/Archive/docker_work/viascientific/foundry-mcp-phase0/test/test_run_logs.py` (new)

## Commit

`240ddeb3e1376babf8e088bdf92f183c7eae78cf` — "feat(mcp): add get_run_log tool to
surface failure logs in chat" (2 files changed, 147 insertions(+), 1 deletion(-)).
Same author identity (`Alper Kucukural <alper@viascientific.com>`) as Task 1's
commit `6e442ba` — this is the worktree's existing git config, unchanged by me.

## Self-review findings

- Diff matches the brief's Step 1 and Step 4 code blocks verbatim (checked with
  `git diff` after implementing, before committing) — no deviations introduced.
- Insertion point verified before editing: `get_run` ends at line 741, the
  `# Run Execution Tools` banner was at line 744 — matches the brief's "around
  line 744" pointer.
- `envelope`/`tail_text` in `src/foundry_mcp/utils.py` already exist from Task 1
  with the exact signatures the brief assumes (`envelope(summary, data=None,
  next_steps=None)`, `tail_text(text, max_lines=200, max_chars=12000)`) — no
  adjustment needed.
- `attempt_id: int = None` and the truthiness check `if attempt_id` mean an
  explicit `attempt_id=0` would be treated as "no attempt filter" (params=None
  instead of `{"attemptId": 0}`). This matches the brief's exact code and the
  existing codebase convention (`get_run`'s `run_name: str = None` uses the same
  style); attempt IDs are not expected to be `0` in practice, so this is a
  non-issue but worth flagging as an inherited edge case, not something I added.
- `_pick_diagnostic_log`'s `by_name` dict comprehension would let a duplicate
  log `name` in the input list silently overwrite an earlier entry with the
  same name (last-wins). The brief's code does this intentionally; duplicate
  names aren't expected from the real `/logs` endpoint.
- No linter/formatter config beyond `pyproject.toml`'s pytest section was found
  in the repo, so no additional lint step was run beyond pytest.

## Concerns

None. Implementation is a verbatim application of the brief; TDD evidence
confirms tests failed for the expected reason pre-implementation and passed
post-implementation; full-suite run shows zero regressions against the
pre-existing (out-of-scope) `test_client.py` failures.
