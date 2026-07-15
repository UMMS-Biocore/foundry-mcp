"""Opt-in live integration test. Runs only when VIAFOUNDRY_LIVE_* env vars are set.

These talk to a real ViaFoundry instance, so they are skipped by default. Set:

    VIAFOUNDRY_LIVE_HOST=https://<instance>       # or http://host:port
    VIAFOUNDRY_LIVE_TOKEN=via_mcp_...             # MUST be an MCP-scoped token
    VIAFOUNDRY_LIVE_SOURCE_RUN=<runId>            # a small/cheap known-good run

The mutating-chain test additionally needs a throwaway target project:

    VIAFOUNDRY_LIVE_TARGET_PROJECT=<projectId>

The mutating test exercises duplicate_run -> update_run only. It deliberately
does NOT call initiate_run: automated tests must never launch cluster compute.
It DOES leave an unlaunched duplicate (draft) run behind in the target project.
"""
import json
import os
import pytest

from src.viafoundry_mcp import server, config

LIVE = all(os.environ.get(k) for k in (
    "VIAFOUNDRY_LIVE_HOST", "VIAFOUNDRY_LIVE_TOKEN", "VIAFOUNDRY_LIVE_SOURCE_RUN"
))
pytestmark = pytest.mark.skipif(not LIVE, reason="live ViaFoundry creds not set")

LIVE_MUTATE = LIVE and bool(os.environ.get("VIAFOUNDRY_LIVE_TARGET_PROJECT"))

# ViaFoundry permission value for GroupShared runs; group_id is required only then.
GROUP_SHARED_PERMISSION = 15


def _is_error(result):
    """update_run/duplicate_run return a JSON string; an error is a dict with 'error'."""
    return isinstance(result, dict) and "error" in result


def test_get_run_details_roundtrip():
    config.set_credentials(
        os.environ["VIAFOUNDRY_LIVE_HOST"], os.environ["VIAFOUNDRY_LIVE_TOKEN"]
    )
    try:
        result = json.loads(
            server.get_run_details(os.environ["VIAFOUNDRY_LIVE_SOURCE_RUN"])
        )
        assert "error" not in result
        assert "permission" in result and "groupId" in result
    finally:
        config.set_credentials(None, None)


@pytest.mark.skipif(
    not LIVE_MUTATE, reason="VIAFOUNDRY_LIVE_TARGET_PROJECT not set (mutating chain)"
)
def test_duplicate_then_update_run_chain():
    """duplicate_run -> update_run against a live instance (no initiate_run).

    Verified end-to-end on staging 2026-07-15: RNA-seq run 11116 -> duplicate
    12193 in project 1841. Note: update_run (PATCH /save) triggers the backend's
    stale-input cleanup, so the duplicate's input count may DROP after the patch
    (inputs whose names are not in the pipeline's current variable set are
    removed). That is expected; we assert the run stays coherent, not that the
    input count is preserved.
    """
    config.set_credentials(
        os.environ["VIAFOUNDRY_LIVE_HOST"], os.environ["VIAFOUNDRY_LIVE_TOKEN"]
    )
    try:
        source_run = os.environ["VIAFOUNDRY_LIVE_SOURCE_RUN"]
        target_project = int(os.environ["VIAFOUNDRY_LIVE_TARGET_PROJECT"])

        # Learn the source run's shape.
        src = json.loads(server.get_run_details(source_run))
        assert "error" not in src
        pipeline_id = (src.get("mainPipeline") or {}).get("id")
        assert pipeline_id, "source run has no mainPipeline.id"

        # 1) duplicate into the target project.
        dup = json.loads(
            server.duplicate_run(source_run, target_project, int(pipeline_id))
        )
        assert not _is_error(dup), f"duplicate_run failed: {dup}"
        new_run = dup.get("duplicatedRunId")
        assert new_run, f"no duplicatedRunId in response: {dup}"

        # 2) re-inspect the duplicate and build a minimal, execution-inert patch.
        det = json.loads(server.get_run_details(str(new_run)))
        assert "error" not in det
        permission = det["permission"]
        group_id = det.get("groupId") if permission == GROUP_SHARED_PERMISSION else None
        process_options = det.get("processOptions") or {}

        # Re-affirm one existing input (upsert). Prefer the vmetaCollection input
        # (the one duplicate_run is known to sometimes drop); else any scalar with
        # a non-empty value. Never send an empty-string value.
        inputs_patch = []
        vmeta = next(
            (i for i in det.get("inputs", []) if i.get("type") == "vmetaCollection"),
            None,
        )
        if vmeta:
            inputs_patch = [{
                "id": vmeta.get("id"),
                "type": "vmetaCollection",
                "name": vmeta.get("name"),
                "vmetaCollectionId": vmeta.get("vmetaCollectionId"),
                "value": vmeta.get("value") or "NA",
            }]
        else:
            scalar = next(
                (i for i in det.get("inputs", [])
                 if i.get("type") == "input" and i.get("value")),
                None,
            )
            if scalar:
                inputs_patch = [{
                    "id": scalar.get("id"),
                    "type": "input",
                    "name": scalar.get("name"),
                    "value": scalar.get("value"),
                }]

        # 3) patch. processOptions is REPLACE-semantics server-side, so send the
        #    full dict verbatim. Success returns "" (empty body); an error is a
        #    dict with an "error" key.
        res = json.loads(server.update_run(
            str(new_run),
            inputs=inputs_patch,
            process_options=process_options,
            permission=permission,
            group_id=group_id,
        ))
        assert not _is_error(res), f"update_run failed: {res}"

        # 4) the run must still be coherent afterwards (do NOT initiate it).
        after = json.loads(server.get_run_details(str(new_run)))
        assert "error" not in after
        assert after.get("inputs"), "run lost all inputs after update_run"
        assert "permission" in after
    finally:
        config.set_credentials(None, None)
