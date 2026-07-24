"""Tests for preflight_run — catching the things that waste HPC time.

The motivating case is real: live run 12194's `compare_file` points at a path
that returns "File not found" on the cluster *today*. That is the
duplicate-a-purged-run failure mode, where DESeq2 silently reports "Skipped"
after the job has already burned cluster time.
"""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server

LAUNCH_DIR = "/pi/alper.kucukural-umw/kucukura/test/aligners"
UUID = "8597b4294de7456ea44b72079e242213"


def _inp(name, value, type_="input", **extra):
    row = {"id": 1, "name": name, "type": type_, "value": value,
           "collectionId": None, "vmetaCollectionId": "", "serviceTokenId": None}
    row.update(extra)
    return row


def _details(inputs=None, launch_dir=LAUNCH_DIR):
    return {
        "name": "Mouse Aligner Tests",
        "runUUID": UUID,
        "launchDirectory": launch_dir,
        "mainPipeline": {"id": 1405, "name": "RNA-seq Pipeline", "version": "2.7.4"},
        "runEnvironment": {"selectedId": 550, "availableList": []},
        "inputs": inputs if inputs is not None else DEFAULT_INPUTS,
        "processOptions": {},
    }


DEFAULT_INPUTS = [
    _inp("reads", "rna_seq mousetest paired-2", "vmetaCollection",
         vmetaCollectionId="69743e8d41a0a599bb36f6a8"),
    _inp("genome_build", "mousetest_mm10"),
    _inp("groups_file", f"{LAUNCH_DIR}/foundryUploads/run12194/metadata.tsv"),
    _inp("compare_file", f"{LAUNCH_DIR}/foundryUploads/run12194/compare.tsv"),
]

GROUPS_TSV = ("sample_name\tgroup\n"
              "control_rep1\tcontrol\ncontrol_rep2\tcontrol\ncontrol_rep3\tcontrol\n"
              "exper_rep1\texper\nexper_rep2\texper\nexper_rep3\texper\n")

DATASET_ROWS = [{"name": n, "file1": f"/d/{n}.1.gz", "file2": f"/d/{n}.2.gz",
                 "file_layout": "pair"}
                for n in ("control_rep1", "control_rep2", "control_rep3",
                          "exper_rep1", "exper_rep2", "exper_rep3")]


def _client(details=None, dataset_rows=None, files=None, run_status="NextSuc"):
    """files maps an absolute path -> its contents, or to the sentinel MISSING."""
    files = {} if files is None else files
    details = details or _details()
    rows = DATASET_ROWS if dataset_rows is None else dataset_rows
    c = MagicMock()

    def _call(**kwargs):
        endpoint = kwargs.get("endpoint", "")
        if endpoint.endswith("/details"):
            return details
        if "/vmeta/dataset/" in endpoint and endpoint.endswith("/files/search"):
            return {"status": "success", "results": len(rows), "data": rows}
        if endpoint.endswith("/uploaded-file"):
            path = (kwargs.get("data") or {}).get("path")
            if path not in files:
                raise RuntimeError("File not found")
            return files[path]
        if "/run/list" in endpoint:
            return {"total": 1, "data": [{"id": 12194, "status": run_status}]}
        return {}

    c.call.side_effect = _call
    return c


def _preflight(**kwargs):
    client = _client(**kwargs)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.preflight_run("12194")), client


ALL_GOOD = {f"{LAUNCH_DIR}/foundryUploads/run12194/metadata.tsv": GROUPS_TSV,
            f"{LAUNCH_DIR}/foundryUploads/run12194/compare.tsv":
                "control\texper\n"}


class TestPreflightRun:
    def test_a_healthy_run_passes_cleanly(self):
        parsed, _ = _preflight(files=ALL_GOOD)
        assert parsed["data"]["ok"] is True
        assert parsed["data"]["failures"] == 0
        assert "ready" in parsed["summary"].lower()
        assert all(c["status"] in ("pass", "warn")
                   for c in parsed["data"]["checks"])

    def test_catches_the_dead_compare_file(self):
        """The live 12194 case: metadata.tsv exists, compare.tsv does not."""
        files = {f"{LAUNCH_DIR}/foundryUploads/run12194/metadata.tsv": GROUPS_TSV}
        parsed, _ = _preflight(files=files)
        assert parsed["data"]["ok"] is False
        dead = [c for c in parsed["data"]["checks"] if c["status"] == "fail"]
        assert len(dead) == 1
        assert dead[0]["input"] == "compare_file"
        assert "compare.tsv" in dead[0]["detail"]
        assert dead[0]["fix"]

    def test_reads_paths_with_the_runs_own_cluster_id_and_uuid(self):
        _, client = _preflight(files=ALL_GOOD)
        reads = [c for c in client.call.call_args_list
                 if c.kwargs.get("endpoint", "").endswith("/uploaded-file")]
        assert reads
        body = reads[0].kwargs["data"]
        assert body["profileClusterId"] == 550
        assert body["runUUID"] == UUID

    def test_a_never_launched_run_warns_rather_than_failing_on_its_own_uploads(self):
        """A sheet written for a run that has not launched yet legitimately does
        not exist on the cluster. Failing there would cry wolf on every new run."""
        parsed, _ = _preflight(files={}, run_status="init")
        statuses = {c["input"]: c["status"] for c in parsed["data"]["checks"]
                    if c.get("input") in ("groups_file", "compare_file")}
        assert set(statuses.values()) == {"warn"}
        assert parsed["data"]["ok"] is True

    def test_a_missing_path_outside_the_run_uploads_always_fails(self):
        """A reference or shared path is not "not launched yet" — it is wrong."""
        inputs = DEFAULT_INPUTS[:2] + [
            _inp("groups_file", "/share/data/somebody_elses/metadata.tsv")]
        parsed, _ = _preflight(details=_details(inputs), files={},
                               run_status="init")
        fails = [c for c in parsed["data"]["checks"] if c["status"] == "fail"]
        assert [c["input"] for c in fails] == ["groups_file"]

    def test_flags_samples_in_the_sheet_that_are_not_in_the_dataset(self):
        files = dict(ALL_GOOD)
        files[f"{LAUNCH_DIR}/foundryUploads/run12194/metadata.tsv"] = (
            GROUPS_TSV + "ghost_rep1\texper\n")
        parsed, _ = _preflight(files=files)
        check = next(c for c in parsed["data"]["checks"]
                     if c["id"] == "sample_names_match")
        assert check["status"] == "fail"
        assert "ghost_rep1" in check["detail"]

    def test_flags_dataset_samples_missing_from_the_sheet_too(self):
        """Both directions matter: a sample with no group is silently dropped
        from the differential test."""
        files = dict(ALL_GOOD)
        files[f"{LAUNCH_DIR}/foundryUploads/run12194/metadata.tsv"] = (
            "sample_name\tgroup\ncontrol_rep1\tcontrol\nexper_rep1\texper\n")
        parsed, _ = _preflight(files=files)
        check = next(c for c in parsed["data"]["checks"]
                     if c["id"] == "sample_names_match")
        assert check["status"] in ("warn", "fail")
        assert "control_rep2" in check["detail"]

    def test_flags_an_empty_sample_dataset(self):
        parsed, _ = _preflight(dataset_rows=[], files=ALL_GOOD)
        check = next(c for c in parsed["data"]["checks"] if c["id"] == "samples")
        assert check["status"] == "fail"
        assert "no files" in check["detail"].lower()

    def test_flags_a_group_with_no_replicates(self):
        files = dict(ALL_GOOD)
        files[f"{LAUNCH_DIR}/foundryUploads/run12194/metadata.tsv"] = (
            "sample_name\tgroup\ncontrol_rep1\tcontrol\ncontrol_rep2\tcontrol\n"
            "control_rep3\tcontrol\nexper_rep1\texper\nexper_rep2\tsolo\n"
            "exper_rep3\texper\n")
        parsed, _ = _preflight(files=files)
        check = next(c for c in parsed["data"]["checks"] if c["id"] == "replicates")
        assert check["status"] == "warn"
        assert "solo" in check["detail"]

    def test_flags_empty_and_placeholder_input_values(self):
        inputs = DEFAULT_INPUTS + [_inp("mate", ""), _inp("bed", "NO_FILE")]
        parsed, _ = _preflight(details=_details(inputs), files=ALL_GOOD)
        check = next(c for c in parsed["data"]["checks"] if c["id"] == "empty_inputs")
        assert check["status"] == "fail"
        assert "mate" in check["detail"] and "bed" in check["detail"]

    def test_flags_a_missing_genome_build(self):
        inputs = [i for i in DEFAULT_INPUTS if i["name"] != "genome_build"]
        parsed, _ = _preflight(details=_details(inputs), files=ALL_GOOD)
        check = next(c for c in parsed["data"]["checks"] if c["id"] == "genome")
        assert check["status"] == "warn"

    def test_every_check_reports_a_fix_when_it_is_not_passing(self):
        parsed, _ = _preflight(files={})
        for check in parsed["data"]["checks"]:
            if check["status"] != "pass":
                assert check["fix"], f"{check['id']} has no fix"

    def test_next_steps_gate_on_the_result(self):
        parsed, _ = _preflight(files=ALL_GOOD)
        assert "initiate_run" in " ".join(parsed["next_steps"])
        blocked, _ = _preflight(files={})
        joined = " ".join(blocked["next_steps"])
        assert "before" in joined.lower() or "fix" in joined.lower()

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.preflight_run("12194")
        assert json.loads(result) == {"error": "boom"}
