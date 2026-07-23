# Task 4 Report — Compact `get_run_details` + verbose escape hatch + launch advisory

## Summary

Implemented all 5 parts of Task 4 in `foundry-mcp` (worktree
`foundry-mcp-phase0`, branch `feat/chat-scientist-phase0`):

1. Updated the existing test `TestGetRunDetails.test_hits_details_endpoint_and_returns_json`
   in `test/test_run_execution.py` to call `server.get_run_details("123", verbose=True)`.
2. Added `test/test_run_details_summary.py` with the 3 specified tests for the
   compact path (default envelope shape, next_steps advisory content, verbose
   raw-blob passthrough).
3. Added `_summarize_run_details(details) -> dict` and replaced `get_run_details`
   with the new `(run_id: str, verbose: bool = False) -> str` version in
   `src/foundry_mcp/server.py`.
4. Edited `initiate_run`'s docstring to add the plain HPC cost/time wording
   ("This LAUNCHES real HPC compute (it can take minutes to hours and consumes
   cluster time) — always confirm with the user before calling.").
5. Added the CHANGELOG entry under `## [Unreleased]` → `### Added`.

All code/text matches the brief verbatim (`_summarize_run_details`,
`get_run_details`, the docstring line, and the CHANGELOG bullet were copied
exactly as specified).

## Extra fix beyond the brief's file list (flagged, not silently done)

The brief's file list didn't mention `test/test_run_execution_live.py`, but it
is an opt-in, credential-gated live-integration test (skipped by default —
this is the "2 skipped" seen in every full-suite run in this report) that
calls `server.get_run_details(...)` at 4 call sites and reads raw keys
(`permission`, `groupId`, `mainPipeline`, `processOptions`, `inputs`) directly
off the result — i.e. it depends on the legacy raw-blob shape that is now
gated behind `verbose=True`. Since the task's global constraint states
"Backwards compatibility is critical" specifically for this exact
duplicate→update_run→initiate_run flow, and this file *is* the live
verification of that flow (see project memory:
`mcp-run-execution-tools-live-verified`), I added `verbose=True` to its 4
`get_run_details(...)` calls. This is a minimal, mechanical, same-semantics
change (no assertions altered) that prevents a latent regression for anyone
who runs it live with real credentials. It does not affect the automated
test suite's pass/fail outcome here since the test is skipped without
`VIAFOUNDRY_LIVE_*` env vars.

I did NOT touch `README.md`'s one-line tool table (`| get_run_details | Full
editable run: inputs, processOptions, permission, groupId |`), which is now
slightly stale (it describes the raw/verbose shape, not the new default). It
wasn't in the brief's file list and is documentation-only; noting it below as
a low-priority follow-up rather than expanding scope unilaterally.

## TDD evidence

### RED — before implementation

Command:
```
python -m pytest test/test_run_details_summary.py -v
```
Output (abbreviated, full run captured 3 failures):
```
FAILED test/test_run_details_summary.py::TestGetRunDetailsCompact::test_default_returns_compact_summary_envelope
  AssertionError: assert 'mainPipeline' == 'summary'
FAILED test/test_run_details_summary.py::TestGetRunDetailsCompact::test_default_next_steps_have_verbose_and_hpc_advisory
  KeyError: 'next_steps'
FAILED test/test_run_details_summary.py::TestGetRunDetailsCompact::test_verbose_returns_full_blob
  TypeError: get_run_details() got an unexpected keyword argument 'verbose'
3 failed, 64 warnings in 1.30s
```
This is exactly the expected RED per the brief: the default call still
returned the raw blob (`mainPipeline` is the raw top-level key, not
`summary`), there was no `next_steps` key, and `verbose` wasn't yet a
parameter.

Command:
```
python -m pytest test/test_run_execution.py -v -k GetRunDetails
```
Output:
```
FAILED test/test_run_execution.py::TestGetRunDetails::test_hits_details_endpoint_and_returns_json
  TypeError: get_run_details() got an unexpected keyword argument 'verbose'
1 failed, 1 passed, 13 deselected
```
Confirms the modified existing test also genuinely failed pre-implementation
(the error-path test, unchanged, still passed).

### GREEN — after implementation

Command:
```
python -m pytest test/test_run_details_summary.py test/test_run_execution.py -v
```
Output:
```
test/test_run_details_summary.py::TestGetRunDetailsCompact::test_default_returns_compact_summary_envelope PASSED
test/test_run_details_summary.py::TestGetRunDetailsCompact::test_default_next_steps_have_verbose_and_hpc_advisory PASSED
test/test_run_details_summary.py::TestGetRunDetailsCompact::test_verbose_returns_full_blob PASSED
test/test_run_execution.py::TestGetRunDetails::test_hits_details_endpoint_and_returns_json PASSED
test/test_run_execution.py::TestGetRunDetails::test_returns_error_json_on_exception PASSED
... (13 more, all PASSED)
18 passed, 64 warnings in 0.82s
```

### Full suite

Command:
```
python -m pytest test/ -q
```
Output:
```
FAILED test/test_client.py::test_get_client_initializes_once - ValueError: In...
FAILED test/test_client.py::test_get_client_configures_auth_token - ValueErro...
FAILED test/test_client.py::test_get_client_missing_credentials - AttributeEr...
FAILED test/test_client.py::test_reset_clients_clears_cache - ValueError: Inv...
FAILED test/test_client.py::test_get_client_caches_by_credentials - ValueErro...
5 failed, 98 passed, 2 skipped, 64 warnings in 1.08s
```
This matches the brief's expectation: exactly the 5 pre-existing,
out-of-scope `test_client.py` failures and no new failures. I verified this
baseline directly by `git stash`-ing my changes and re-running
`test/test_client.py` alone against the pre-Task-4 commit (031e6ec): same 5
failures, same error messages, `5 failed, 2 passed` — confirming these
failures pre-exist my change and are unrelated to it. Changes were restored
via `git stash pop` afterward.

## Backwards-compat confirmation (explicit)

`get_run_details(run_id, verbose=True)` returns
`json.dumps(details, indent=2)` where `details` is exactly the object
returned by `via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/details")`
— unmodified, no wrapping, no field removal. This is verified by
`test_verbose_returns_full_blob`, which asserts `parsed["groupId"] == 10`
(a raw top-level key) and `"summary" not in parsed`. The default
(non-verbose) path is the only one that returns the new compact envelope.
The duplicate_run → update_run → initiate_run flow (and the live
integration test that exercises it) is preserved by calling with
`verbose=True`.

## Files changed

- `/Users/alper/ws/Archive/docker_work/viascientific/foundry-mcp-phase0/src/foundry_mcp/server.py`
  — added `_summarize_run_details()`; reworked `get_run_details(run_id, verbose=False)`;
  updated `initiate_run` docstring.
- `/Users/alper/ws/Archive/docker_work/viascientific/foundry-mcp-phase0/test/test_run_execution.py`
  — existing test now passes `verbose=True`.
- `/Users/alper/ws/Archive/docker_work/viascientific/foundry-mcp-phase0/test/test_run_details_summary.py`
  — new file, 3 tests, matches brief verbatim.
- `/Users/alper/ws/Archive/docker_work/viascientific/foundry-mcp-phase0/test/test_run_execution_live.py`
  — (beyond brief's file list, justified above) added `verbose=True` at its 4
  `get_run_details(...)` call sites so the live duplicate/update chain keeps
  reading the raw blob.
- `/Users/alper/ws/Archive/docker_work/viascientific/foundry-mcp-phase0/CHANGELOG.md`
  — added the Phase 0 "Chat-friendly run journey" bullet under
  `## [Unreleased]` → `### Added`.

## Self-review findings

- Code matches the brief's exact snippets for `_summarize_run_details`,
  `get_run_details`, the `initiate_run` docstring, and the CHANGELOG entry —
  no deviation.
- `envelope()` key order (`summary` → `next_steps` → `data`) is inherited
  from the Task 1 helper (`src/foundry_mcp/utils.py`); `get_run_details`'s
  default path uses it unmodified, so key order is correct by construction.
  Verified directly: `test_default_returns_compact_summary_envelope` asserts
  `list(parsed.keys())[0] == "summary"`.
- Checked for other in-repo callers of `get_run_details(...)` via
  `grep -rn "get_run_details(" src/ test/`: found the docstring
  cross-references in `get_run_log`/`get_run_details` (text only, not
  affected), the 5 test-file call sites, and the 4 live-test call sites
  (fixed, see above). No other production code path calls it internally.
- `test/test_integration.py` only checks that `'get_run_details'` is present
  in a list of expected tool names — unaffected by the signature/behavior
  change.
- `_summarize_run_details` defensively uses `details.get(...)  or {}` for
  `mainPipeline`/`project`/`processOptions`, so it degrades gracefully (all
  `None`/empty) if the backend ever omits a key — no crash risk introduced.
- No placeholders, TODOs, or "similar to X" left in the diff.

## Concerns

- `README.md`'s tool-summary table row for `get_run_details` is now stale
  (describes the raw/verbose shape as the default). Not in the brief's file
  list; left untouched. Low priority — worth a follow-up doc pass whenever
  README is next touched.
- The 4-call-site fix to `test/test_run_execution_live.py` is technically
  outside the brief's stated file list, done under the explicit "backwards
  compatibility is critical" constraint to avoid a latent regression in a
  test that verifies exactly the flow that constraint protects. Flagging
  for visibility in case the plan owner wants it reverted/handled
  differently, though I judge it correct and low-risk.
- Per-task deploy note (unchanged from the brief's "After the plan" section):
  none of this takes effect in a running stack until the parent repo's
  `./mcp` submodule pointer is bumped and the MCP container is
  rebuilt/redeployed — out of scope for this task.

---

# Task 4 — Final Code Review Fixes (2026-07-22)

## Summary

Applied all 10 fixes from the consolidated final code review on top of
commit `367d9b2` (feat(mcp): compact get_run_details with verbose escape
hatch + launch advisory). Split into 3 logical commits via `git add -p`
hunk-level staging (so each commit contains exactly the code + tests for
its concern, not whole-file dumps):

- `31e4754` — FIX 1 (dict-shaped external-pipeline inputs bug) + its 3 new
  tests in `test/test_run_details_summary.py`.
- `6734e25` — FIX 2/3/4/5 (get_run_log log-picker hardening + relaunch
  confirm cue) + 5 new pinning tests in `test/test_run_logs.py`.
- `611fbdd` — FIX 6/7/8/9/10 (docstrings, README, get_run comment, and the
  2 new tests in `test/test_run_status.py` + 1 in `test/test_integration.py`).

## Per-fix detail

### FIX 1 — `_summarize_run_details` silently reported "0 settings" for external pipelines

**File/lines:** `src/foundry_mcp/server.py:899-928` (new `_iter_run_inputs`
generator at ~899-911; `_summarize_run_details` loop rewritten at ~921-928).

**Why:** `inputs` from `/api/v1/run/{id}/details` is a **list** of
`{name,value,type}` for ViaFoundry-native pipelines but a **dict** of
`{name: {value,type}}` for external (nf-core/Nextflow) pipelines. The old
loop did `for inp in inputs: if not isinstance(inp, dict): continue` —
iterating a dict yields its *keys* (plain strings), so every iteration hit
the `continue` and the function returned empty `settings`/`sample_inputs`
with no error, no warning — confidently wrong output handed to the
scientist.

**Fix:** Added `_iter_run_inputs(inputs)`, a generator that normalizes both
shapes to `(name, value, type)` triples — dict branch unpacks
`{value, type}` sub-dicts (or treats the value as a bare scalar if it isn't
a dict with a `value` key), list branch is the original behavior unchanged.
`_summarize_run_details` now iterates this generator instead of the raw
`inputs`, with the same downstream classification
(`vmetaCollection` → `sample_inputs`, `"/"`-prefixed string →
`reference_paths`, else → `settings`) untouched.

**Tests added** (`test/test_run_details_summary.py`, new
`_EXTERNAL_DETAILS` fixture + `TestGetRunDetailsExternalPipelineDictInputs`
class, 3 tests): dict-shaped settings populate non-empty; a `"/"`-prefixed
dict value still lands in `reference_paths`; a `vmetaCollection`-typed dict
entry still lands in `sample_inputs`. All 3 existing list-shape tests in
the same file pass unchanged (verified — see Verification below).

### FIX 2 — `get_run_log` relaunch next_step lacked confirm cue + run_id

**File/lines:** `src/foundry_mcp/server.py:880-885` (the second
`next_steps` entry in `get_run_log`).

**Why/what:** Changed
`"Fix the cause, then re-launch with initiate_run(run_type='resumerun')..."`
to
`f"Fix the cause, then — after confirming with the user — re-launch with initiate_run(run_id='{run_id}', run_type='resumerun') to reuse completed steps."`
— exact text from the brief. This is the highest-momentum moment for an
unconfirmed relaunch (read right after seeing the error), and every other
launch-adjacent surface (`get_run_details`, `initiate_run` itself) already
carries this guardrail.

**Test:** `test_relaunch_next_step_has_confirm_cue_and_run_id` in
`test/test_run_logs.py` asserts both `"confirming with the user"` and
`"run_id='12219'"` appear in the relaunch next_step.

### FIX 3 — `_pick_diagnostic_log` could return a nameless log; `available_logs` could include unnamed entries

**File/lines:** `src/foundry_mcp/server.py:821-826` (fallback loop
condition), `:875-878` (`available_logs` comprehension).

**What:** Fallback loop condition changed from `if content.strip():` to
`if name and content.strip():` (also skips the non-log-suffix check on the
same line, see FIX 4 below — combined into one loop body). `available_logs`
comprehension changed from
`[entry.get("name") for entry in logs if isinstance(entry, dict)]` to
`[entry.get("name") for entry in logs if isinstance(entry, dict) and entry.get("name")]`.

**Tests:** `test_available_logs_excludes_nameless_entries` (a log with
`"name": ""` and one with no `name` key at all are both excluded from
`available_logs`, leaving only `["err.log"]`).

### FIX 4 — fallback log picker could select non-log artifacts (report.html, trace.txt, nextflow.nf/.config)

**File/lines:** `src/foundry_mcp/server.py:804-807` (new
`_NON_LOG_SUFFIXES` constant), `:821-823` (skip check in the fallback loop
only — the priority loop is untouched).

**What:** Added `_NON_LOG_SUFFIXES = (".html", ".nf", ".config")` and a
`continue` in the fallback loop when `name.endswith(_NON_LOG_SUFFIXES)`.
Scoped to the fallback loop only, per the brief — the priority loop
(`_LOG_PRIORITY` names) is unaffected since none of those names carry these
suffixes.

**Test:** `test_fallback_skips_html_and_config_and_nf_artifacts` — logs
containing only `report.html`, `timeline.html`, `nextflow.nf`,
`nextflow.config` (all with real content) plus one genuine `some_other.log`
— asserts the picked log is `some_other.log`, not one of the artifacts.
(Note: I deliberately left `trace.txt` out of this test's fixture — a
`.txt` suffix is not in `_NON_LOG_SUFFIXES` per the brief's exact list, and
`log.txt` is a legitimate *priority* log name, so filtering `.txt`
generically would be a scope-creep change the brief didn't ask for and
would risk excluding real logs.)

### FIX 5 — add `.command.out` to `_LOG_PRIORITY`

**File/lines:** `src/foundry_mcp/server.py:799-802`. Inserted immediately
after `.command.err` as specified.

**Test:** `test_command_out_outranks_nextflow_log` — a log set with both
`.nextflow.log` and `.command.out` populated asserts `.command.out` wins
(it's earlier in `_LOG_PRIORITY`).

### FIX 6 — docstrings under-specifying the contract

**File/lines:**
- `duplicate_run` docstring, `src/foundry_mcp/server.py:1013-1021`: now
  reads "...come from get_run_details(run_id, verbose=True) on the source
  run (its `project.id` and `mainPipeline.id`)." — corrected `projectId` →
  `project.id` (the actual nested payload key; there is no top-level
  `projectId` field, confirmed by the `_summarize_run_details`/`_DETAILS`
  test fixture which has `"project": {"id": ..., "name": ...}`).
- `update_run` docstring, `src/foundry_mcp/server.py:1073-1078`: "REQUIRED
  (echo it from get_run_details(run_id, verbose=True))" — same cross-
  reference fix.

No test added for a docstring-only change; verified by direct read.

### FIX 7 — README stale tool count/table

**File/lines:** `README.md`:
- `## Available Tools (47 Total)` → `(48 Total)` (line 89). Verified via
  `grep -c "@mcp.tool()" src/foundry_mcp/server.py` → `48`. Cross-checked
  against the per-section counts (8+8+10+3+3+6+4+3+3 = 48 — the sections
  were internally consistent already, only the two touched by this task's
  new tool needed bumping).
- `### 🏃 Run Management (7 tools)` → `(8 tools)` (line 108).
- Added a `get_run_log` row: `"Execution logs for a run (diagnose
  failures); \`attempt_id\` optional"` (matches the brief's suggested
  wording).
- Reworded the `get_run_details` row from "Full editable run: inputs,
  processOptions, permission, groupId" to "Compact run config;
  `verbose=True` returns the full editable inputs/processOptions".

### FIX 8 — pin the untouched `get_run` branches with tests

**File/lines:** `test/test_run_status.py`, 2 new tests appended to
`TestGetRunHumanStatus`:
- `test_fuzzy_match_does_not_get_status_display` — `server.get_run(run_name="nope")`
  against a client returning one non-exact-match run; asserts
  `match_type == "fuzzy"` and `"status_display" not in parsed`.
- `test_no_match_does_not_get_status_display` — same call against an empty
  `{"data": []}` client response; asserts `match_type == "none"` and
  `"status_display" not in parsed`.

### FIX 9 — registration smoke test

**File/lines:** `test/test_integration.py:53` — added `'get_run_log'` to
`expected_run_tools` in `test_run_tools_registered`.

### FIX 10 — clarifying comment at the `get_run` enrichment block

**File/lines:** `src/foundry_mcp/server.py:759-762`, immediately before
`run_obj = result.get("run")`:
```python
# NOTE: summary/next_steps are merged FLAT into `result` here (not
# nested in an envelope like get_run_log/get_run_details) so that
# existing consumers of result["run"] keep working unchanged. Don't
# "fix" this to use envelope() without also updating those consumers.
```

## Constraints verified

- Tool contract unchanged: every tool still does
  `json.dumps(result, indent=2)` / `json.dumps({"error": str(e)})` on
  exception — no signature or return-type changes to any `@mcp.tool()`
  function beyond what the brief specified (docstrings + the already-
  planned `get_run_log` internals).
- `verbose=True` still returns the raw blob unchanged —
  `test_verbose_returns_full_blob` (pre-existing, untouched) still passes.
- `envelope()` key order (`summary` → `next_steps` → `data`) untouched —
  `envelope()` itself (`src/foundry_mcp/utils.py`) was not modified by any
  of these fixes.
- The 5 pre-existing `test/test_client.py` failures are present,
  unchanged, both before and after this task's commits (see Verification
  below) — confirmed out of scope, ignored per instructions.

## Verification (actual command output)

Baseline (pre-fix, at `367d9b2`), for reference:
```
$ python -m pytest test/ -q
5 failed, 98 passed, 2 skipped, 64 warnings in 4.01s
```
(the 5 failures are the same `test/test_client.py` tests listed below)

Targeted run, post-fix:
```
$ python -m pytest test/test_run_details_summary.py test/test_run_logs.py test/test_run_status.py test/test_run_execution.py test/test_integration.py -v
...
======================= 50 passed, 64 warnings in 1.47s ========================
```
All 50 tests passed — 9 of them new this pass (3 added to
test_run_details_summary.py for FIX 1, 5 added to test_run_logs.py for
FIX 2/3/4/5, 2 added to test_run_status.py for FIX 8, 0 added but 1 line
changed in test_integration.py for FIX 9 — its existing tests still count
toward the total). test_run_execution.py is unchanged and included as a
regression check. See the raw `-v` output above for the full per-test list.

Full suite, post-fix:
```
$ python -m pytest test/ -q
FAILED test/test_client.py::test_get_client_initializes_once - ValueError: In...
FAILED test/test_client.py::test_get_client_configures_auth_token - ValueErro...
FAILED test/test_client.py::test_get_client_missing_credentials - AttributeEr...
FAILED test/test_client.py::test_reset_clients_clears_cache - ValueError: Inv...
FAILED test/test_client.py::test_get_client_caches_by_credentials - ValueErro...
5 failed, 107 passed, 2 skipped, 64 warnings in 1.19s
```
Same 5 pre-existing/out-of-scope failures, +9 net new passing tests
(98 → 107), 0 new failures.

## Commits

```
611fbdd docs(mcp): fix stale run-tool docstrings/README and pin get_run's early-return contract
6734e25 fix(mcp): harden get_run_log's diagnostic log picker and relaunch guardrail
31e4754 fix(mcp): normalize external-pipeline dict-shaped inputs in get_run_details summary
```
All 3 are new commits on top of `367d9b2`; no existing commits were
amended or rebased.

## Concerns / follow-ups

None blocking. Two minor notes carried forward for visibility:
- The pre-existing 5 `test_client.py` failures (out of scope per this
  task's instructions) look environment-related (credential validation
  raising instead of the expected mock path) — not touched, not
  investigated further, per explicit instruction to ignore them.
- `test/test_run_execution_live.py` (credential-gated, skipped by default)
  was not touched in this pass — it already uses
  `get_run_details(..., verbose=True)` throughout per the prior Task 4
  report, so it's unaffected by FIX 1's `_iter_run_inputs` change (that
  only touches the non-verbose summary path).
