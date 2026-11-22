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
        "runEnvironment": {"selectedId": 550, "availableList": [{"id": 550}]},
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


def _client(details=None, dataset_rows=None, files=None, run_status="Completed"):
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

    def test_a_pipeline_without_a_genome_build_input_gets_no_genome_warning(self):
        """No definitions name a genome build and the run saves none, so the
        warning would never apply."""
        inputs = [i for i in DEFAULT_INPUTS if i["name"] != "genome_build"]
        parsed, _ = _preflight(details=_details(inputs), files=ALL_GOOD)
        assert not any(c["id"] == "genome" and c["status"] != "pass"
                       for c in parsed["data"]["checks"])

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


class TestPreflightLaunchGates:
    """A run created from scratch has no environment or directory until update_run sets them."""

    @staticmethod
    def _check(parsed, check_id):
        return next(c for c in parsed["data"]["checks"] if c["id"] == check_id)

    def test_missing_environment_and_directory_fail_by_name(self):
        details = _details(launch_dir=None)
        details["runEnvironment"] = {"selectedId": None, "availableList": []}
        parsed, _ = _preflight(details=details, files=ALL_GOOD)
        assert self._check(parsed, "run_environment")["status"] == "fail"
        assert self._check(parsed, "execution_directory")["status"] == "fail"
        assert parsed["data"]["ok"] is False

    def test_set_environment_and_directory_pass(self):
        parsed, _ = _preflight(files=ALL_GOOD)
        assert self._check(parsed, "run_environment")["status"] == "pass"
        assert self._check(parsed, "execution_directory")["status"] == "pass"

    def test_an_environment_you_cannot_launch_on_fails(self):
        details = _details()
        details["runEnvironment"] = {"selectedId": 550, "availableList": [{"id": 777}]}
        parsed, _ = _preflight(details=details, files=ALL_GOOD)
        check = self._check(parsed, "run_environment")
        assert check["status"] == "fail"
        assert "not one you can launch on" in check["detail"]

    def test_no_usable_environment_is_named(self):
        details = _details()
        details["runEnvironment"] = {"selectedId": None, "availableList": []}
        parsed, _ = _preflight(details=details, files=ALL_GOOD)
        assert "no run environment you can launch on" in self._check(parsed, "run_environment")["detail"]


def _with_definitions(client, parameter_list):
    """Serve GET /run/{id}/parameter-list on top of a _client mock."""
    base = client.call.side_effect

    def _call(**kwargs):
        if kwargs.get("endpoint", "").endswith("/parameter-list"):
            return parameter_list
        return base(**kwargs)

    client.call.side_effect = _call
    return client


def _preflight_with(parameter_list, **kwargs):
    client = _with_definitions(_client(**kwargs), parameter_list)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.preflight_run("12194"))


RNASEQ_PARAMS = {"pipelineType": "via", "inputs": {
    "pipe": {
        "schema": [{"varName": "params.run_STAR", "type": "dropdown", "defaultVal": "yes"}],
        "inputNodes": [
            {"varName": "params.reads", "qualifier": "set", "optional": True},
            {"varName": "params.bam_files", "qualifier": "set", "optional": True},
            {"varName": "params.mate", "qualifier": "val"},
            {"varName": "params.genome_build", "qualifier": "val"},
        ],
    },
    "processes": {},
}}


class TestPreflightAgainstDefinitions:
    """#30 (c) and (d): judge the saved inputs against what the pipeline takes."""

    def _check(self, parsed, check_id):
        return next(c for c in parsed["data"]["checks"] if c["id"] == check_id)

    def test_no_saved_inputs_fails_naming_required_ones_without_a_default(self):
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=[]), files=ALL_GOOD)
        check = self._check(parsed, "empty_inputs")
        assert check["status"] == "fail"
        assert "mate" in check["detail"] and "genome_build" in check["detail"]
        assert "run_STAR" not in check["detail"]  # it has a default
        assert parsed["data"]["ok"] is False

    def test_required_inputs_with_values_pass(self):
        inputs = [_inp("mate", "single"), _inp("genome_build", "hg38"),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        assert self._check(parsed, "empty_inputs")["status"] == "pass"
        assert self._check(parsed, "data_inputs")["status"] == "pass"

    def test_no_data_input_fails_listing_the_candidates(self):
        inputs = [_inp("mate", "single"), _inp("genome_build", "hg38")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        check = self._check(parsed, "data_inputs")
        assert check["status"] == "fail"
        assert "reads" in check["detail"] and "bam_files" in check["detail"]
        assert check["fix"]

    def test_either_data_input_is_enough(self):
        inputs = [_inp("mate", "single"), _inp("genome_build", "hg38"), _inp("bam_files", "/d/bams")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        assert self._check(parsed, "data_inputs")["status"] == "pass"

    def test_external_pipeline_sections_count_as_saved(self):
        params = {"pipelineType": "nfcore", "inputs": {"main": {"schema": [
            {"varName": "params.input", "type": "input", "file": True},
            {"varName": "params.outdir", "type": "input"},
        ]}}}
        details = _details(inputs={"main": {"input": "/d/sheet.csv", "outdir": "/d/out"}})
        parsed = _preflight_with(params, details=details, files=ALL_GOOD)
        assert self._check(parsed, "empty_inputs")["status"] == "pass"
        assert self._check(parsed, "data_inputs")["status"] == "pass"

    def test_unreadable_definitions_skip_the_definition_checks(self):
        client = _client(details=_details(inputs=[]), files=ALL_GOOD)
        base = client.call.side_effect

        def _call(**kwargs):
            if kwargs.get("endpoint", "").endswith("/parameter-list"):
                raise RuntimeError("boom")
            return base(**kwargs)

        client.call.side_effect = _call
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.preflight_run("12194"))
        assert "error" not in parsed
        assert not any(c["id"] == "data_inputs" for c in parsed["data"]["checks"])


NO_GENOME_PARAMS = {"pipelineType": "via", "inputs": {
    "pipe": {
        "schema": [],
        "inputNodes": [
            {"varName": "params.reads", "qualifier": "set"},
            {"varName": "params.mate", "qualifier": "val"},
        ],
    },
    "processes": {},
}}


class TestPreflightGenomeBuild:
    """Warn about a missing genome build only when the pipeline takes one."""

    @staticmethod
    def _genome(parsed):
        return next((c for c in parsed["data"]["checks"] if c["id"] == "genome"), None)

    @staticmethod
    def _unreadable(details):
        client = _client(details=details, files=ALL_GOOD)
        base = client.call.side_effect

        def _call(**kwargs):
            if kwargs.get("endpoint", "").endswith("/parameter-list"):
                raise RuntimeError("boom")
            return base(**kwargs)

        client.call.side_effect = _call
        with patch.object(server, "get_client", return_value=client):
            return json.loads(server.preflight_run("12194"))

    def test_pipeline_without_a_genome_build_input_gets_no_warning(self):
        inputs = [_inp("mate", "pair"),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(NO_GENOME_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        check = self._genome(parsed)
        assert check is None or check["status"] == "pass"

    def test_defined_genome_build_left_unset_warns_by_name(self):
        inputs = [_inp("mate", "pair"),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        check = self._genome(parsed)
        assert check["status"] == "warn"
        assert "genome_build" in check["detail"]
        assert check["fix"]

    def test_defined_genome_build_left_empty_warns(self):
        inputs = [_inp("mate", "pair"), _inp("genome_build", ""),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        assert self._genome(parsed)["status"] == "warn"

    def test_defined_genome_build_with_a_value_passes(self):
        inputs = [_inp("mate", "pair"), _inp("genome_build", "hg38"),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        check = self._genome(parsed)
        assert check["status"] == "pass"
        assert "hg38" in check["detail"]

    def test_defined_genome_build_with_a_default_passes_when_unset(self):
        params = json.loads(json.dumps(RNASEQ_PARAMS))
        params["inputs"]["pipe"]["inputNodes"][3]["defaultVal"] = "mm10"
        inputs = [_inp("mate", "pair"),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(params, details=_details(inputs=inputs), files=ALL_GOOD)
        check = self._genome(parsed)
        assert check["status"] == "pass"
        assert "mm10" in check["detail"]

    def test_external_pipeline_section_value_counts(self):
        params = {"pipelineType": "nfcore", "inputs": {"main": {"schema": [
            {"varName": "params.input", "type": "input", "file": True},
            {"varName": "params.genome_build", "type": "input"},
        ]}}}
        details = _details(inputs={"main": {"input": "/d/sheet.csv", "genome_build": "GRCh38"}})
        parsed = _preflight_with(params, details=details, files=ALL_GOOD)
        assert self._genome(parsed)["status"] == "pass"

    def test_unreadable_definitions_do_not_warn_when_no_build_is_saved(self):
        inputs = [i for i in DEFAULT_INPUTS if i["name"] != "genome_build"]
        parsed = self._unreadable(_details(inputs))
        check = self._genome(parsed)
        assert check is None or check["status"] == "pass"

    def test_unreadable_definitions_still_judge_a_saved_build(self):
        assert self._genome(self._unreadable(_details()))["status"] == "pass"
        inputs = [i for i in DEFAULT_INPUTS if i["name"] != "genome_build"] + [_inp("genome_build", "")]
        assert self._genome(self._unreadable(_details(inputs)))["status"] == "warn"


class TestPreflightLayoutMatchesMate:
    """Staging run 12450: a single end dataset on a run whose `mate` input
    stayed `pair` failed in Quality_Filtering ("No such variable: file1")."""

    @staticmethod
    def _check(parsed):
        return next((c for c in parsed["data"]["checks"] if c["id"] == "layout"), None)

    @staticmethod
    def _rows(layouts):
        return [{"name": f"s{i}", "geo_id": f"SRR00000{i}", "file1": "", "file2": "",
                 "file_layout": lay} for i, lay in enumerate(layouts)]

    def test_single_dataset_on_a_pair_run_fails(self):
        inputs = DEFAULT_INPUTS + [_inp("mate", "pair")]
        parsed, _ = _preflight(details=_details(inputs), dataset_rows=self._rows(["single", "single"]),
                               files=ALL_GOOD)
        check = self._check(parsed)
        assert check["status"] == "fail"
        assert "single" in check["detail"] and "pair" in check["detail"]
        assert "mate" in check["fix"]
        assert parsed["data"]["ok"] is False

    def test_matching_layout_passes(self):
        inputs = DEFAULT_INPUTS + [_inp("mate", "pair")]
        parsed, _ = _preflight(details=_details(inputs), files=ALL_GOOD)
        assert self._check(parsed)["status"] == "pass"

    def test_mixed_dataset_fails(self):
        inputs = DEFAULT_INPUTS + [_inp("mate", "single")]
        parsed, _ = _preflight(details=_details(inputs), dataset_rows=self._rows(["single", "pair"]),
                               files=ALL_GOOD)
        check = self._check(parsed)
        assert check["status"] == "fail"
        assert "mixes" in check["detail"]

    def test_layout_is_derived_from_files_when_not_stored(self):
        rows = [{"name": "a", "file1": "/d/a.fq.gz", "file2": ""}]
        inputs = DEFAULT_INPUTS + [_inp("mate", "pair")]
        parsed, _ = _preflight(details=_details(inputs), dataset_rows=rows, files=ALL_GOOD)
        assert self._check(parsed)["status"] == "fail"

    def test_no_mate_input_means_no_check(self):
        parsed, _ = _preflight(files=ALL_GOOD)
        assert self._check(parsed) is None


class TestPreflightLayoutProcessOptions:
    """Staging run 12451: `mate` was single but Kallisto's process option
    single_or_paired_end_reads was still `pair`, and the check passed. Process
    options are stored on the run as {"<process>_<gnum>": {option: value}}."""

    @staticmethod
    def _check(parsed):
        return next((c for c in parsed["data"]["checks"] if c["id"] == "layout"), None)

    @staticmethod
    def _single_rows():
        return [{"name": "s1", "geo_id": "SRR000001", "file1": "", "file2": "", "file_layout": "single"}]

    def _run(self, mate, options, rows=None):
        inputs = DEFAULT_INPUTS + ([_inp("mate", mate)] if mate else [])
        details = _details(inputs)
        details["processOptions"] = options
        parsed, _ = _preflight(details=details, dataset_rows=rows or self._single_rows(), files=ALL_GOOD)
        return self._check(parsed)

    def test_kallisto_option_left_at_pair_fails_by_name(self):
        check = self._run("single", {"248_36": {"single_or_paired_end_reads": "pair", "fragment_length": "200"}})
        assert check["status"] == "fail"
        assert "248_36" in check["detail"] and "single_or_paired_end_reads" in check["detail"]
        assert "'pair'" in check["detail"]
        assert "single_or_paired_end_reads" in check["fix"]

    def test_matching_option_passes(self):
        check = self._run("single", {"248_36": {"single_or_paired_end_reads": "single"}})
        assert check["status"] == "pass"

    def test_empty_option_is_not_a_mismatch(self):
        check = self._run("single", {"257_19": {"single_or_paired_end_reads": ""}})
        assert check["status"] == "pass"

    def test_option_alone_is_checked_without_mate(self):
        check = self._run(None, {"248_36": {"single_or_paired_end_reads": "pair"}})
        assert check["status"] == "fail"

    def test_paired_spellings_are_normalized(self):
        rows = [{"name": "p", "file1": "/d/p.1.gz", "file2": "/d/p.2.gz", "file_layout": "pair"}]
        check = self._run("pair", {"9_1": {"read_layout": "paired-end"}}, rows=rows)
        assert check["status"] == "pass"

    def test_unrelated_options_are_ignored(self):
        check = self._run("single", {"177": {"multiqc_parameters": "-e general_stats"}, "pipe": {}})
        assert check["status"] == "pass"

    def test_mate_and_option_both_wrong_are_both_named(self):
        check = self._run("pair", {"248_36": {"single_or_paired_end_reads": "pair"}})
        assert "mate" in check["detail"] and "single_or_paired_end_reads" in check["detail"]


class TestPreflightNotUsedInputs:
    """update_run refuses an empty value and tells agents to send "NA" for an
    input the run does not use. Preflight must accept that, not fail on it."""

    @staticmethod
    def _check(parsed, check_id):
        return next((c for c in parsed["data"]["checks"] if c["id"] == check_id), None)

    def test_na_on_an_optional_input_passes_with_a_note(self):
        inputs = DEFAULT_INPUTS + [_inp("bed", "NA")]
        parsed, _ = _preflight(details=_details(inputs), files=ALL_GOOD)
        assert self._check(parsed, "empty_inputs")["status"] == "pass"
        note = self._check(parsed, "unused_inputs")
        assert note["status"] == "warn"
        assert "bed" in note["detail"]
        assert note["fix"]
        assert parsed["data"]["ok"] is True

    def test_na_is_matched_after_trimming(self):
        inputs = DEFAULT_INPUTS + [_inp("bed", " NA ")]
        parsed, _ = _preflight(details=_details(inputs), files=ALL_GOOD)
        assert self._check(parsed, "empty_inputs")["status"] == "pass"
        assert "bed" in self._check(parsed, "unused_inputs")["detail"]

    def test_no_na_input_means_no_note(self):
        parsed, _ = _preflight(files=ALL_GOOD)
        assert self._check(parsed, "unused_inputs") is None

    def test_real_blanks_still_fail_and_na_is_not_named_as_blank(self):
        inputs = DEFAULT_INPUTS + [_inp("mate", ""), _inp("bed", "NA")]
        parsed, _ = _preflight(details=_details(inputs), files=ALL_GOOD)
        check = self._check(parsed, "empty_inputs")
        assert check["status"] == "fail"
        assert "mate" in check["detail"]
        assert "bed" not in check["detail"]
        assert "bed" in self._check(parsed, "unused_inputs")["detail"]

    def test_other_placeholders_still_fail(self):
        for placeholder in ("NO_FILE", "None", "null"):
            inputs = DEFAULT_INPUTS + [_inp("bed", placeholder)]
            parsed, _ = _preflight(details=_details(inputs), files=ALL_GOOD)
            check = self._check(parsed, "empty_inputs")
            assert check["status"] == "fail", placeholder
            assert "bed" in check["detail"]

    def test_na_on_a_required_input_without_a_default_fails_by_name(self):
        inputs = [_inp("mate", "NA"), _inp("genome_build", "hg38"),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        check = self._check(parsed, "empty_inputs")
        assert check["status"] == "fail"
        assert "mate" in check["detail"] and "NA" in check["detail"]
        assert "not set" not in check["detail"]  # named once, as NA, not also as unset
        assert parsed["data"]["ok"] is False

    def test_na_on_a_required_input_with_a_default_is_only_noted(self):
        inputs = [_inp("mate", "pair"), _inp("genome_build", "hg38"), _inp("run_STAR", "NA"),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        assert self._check(parsed, "empty_inputs")["status"] == "pass"
        assert "run_STAR" in self._check(parsed, "unused_inputs")["detail"]
        assert parsed["data"]["ok"] is True

    def test_na_on_an_optional_data_input_does_not_count_as_data(self):
        inputs = [_inp("mate", "single"), _inp("genome_build", "hg38"), _inp("bam_files", "NA"),
                  _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8")]
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(inputs=inputs), files=ALL_GOOD)
        assert self._check(parsed, "empty_inputs")["status"] == "pass"
        assert self._check(parsed, "data_inputs")["detail"] == "Data input set: reads."
        assert "bam_files" in self._check(parsed, "unused_inputs")["detail"]


# A Foundry pipeline whose processes carry per process forms. Form fields are
# the schema items whose varName is not `params.*`; the run saves their values
# in processOptions under the same key (`pipe` for the pipeline level form).
FORMS_PARAMS = {"pipelineType": "via", "inputs": {
    "pipe": {
        "schema": [{"varName": "params.run_STAR", "type": "dropdown", "defaultVal": "yes"},
                   {"varName": "pipeline_note", "type": "input"}],
        "inputNodes": [{"varName": "params.reads", "qualifier": "set", "optional": True}],
    },
    "processes": {
        "13_46": {"schema": [{"varName": "aligner_Parameters", "type": "input"},
                             {"varName": "description", "type": "input"}]},
        "4": {"schema": [{"varName": "test_name", "type": "input"}]},
        "7": {"schema": [{"varName": "params.min_len", "type": "input", "defaultVal": "9"}]},
    },
}}


class TestPreflightProcessForms:
    """A run created through the API had blank per process forms that only a
    human noticed in the UI. Preflight warns about forms with nothing in them."""

    @staticmethod
    def _check(parsed):
        return next((c for c in parsed["data"]["checks"] if c["id"] == "process_forms"), None)

    def _run(self, options, params=FORMS_PARAMS, drop_options=False):
        details = _details()
        if drop_options:
            details.pop("processOptions")
        else:
            details["processOptions"] = options
        return _preflight_with(params, details=details, files=ALL_GOOD)

    def test_filled_forms_pass(self):
        check = self._check(self._run({
            "pipe": {"pipeline_note": "x"},
            "13_46": {"aligner_Parameters": ["-k 1"], "description": ["rRNA"]},
            "4": {"test_name": "t1"},
        }))
        assert check["status"] == "pass"

    def test_an_entirely_empty_form_warns_by_name(self):
        parsed = self._run({
            "pipe": {"pipeline_note": "x"},
            "13_46": {"aligner_Parameters": [], "description": ["", " "]},
            "4": {"test_name": "t1"},
        })
        check = self._check(parsed)
        assert check["status"] == "warn"
        assert "13_46" in check["detail"]
        assert "'4'" not in check["detail"] and "pipe" not in check["forms"]
        assert check["forms"] == ["13_46"]
        assert check["fix"]
        assert parsed["data"]["ok"] is True  # a warning, not a failure

    def test_a_form_missing_from_process_options_warns(self):
        check = self._check(self._run({"pipe": {"pipeline_note": "x"},
                                       "13_46": {"aligner_Parameters": "-k 1"}}))
        assert check["status"] == "warn"
        assert check["forms"] == ["4"]

    def test_no_process_options_at_all_warns_naming_every_form(self):
        for kwargs in ({"options": {}}, {"options": None, "drop_options": True}):
            check = self._check(self._run(**kwargs))
            assert check["status"] == "warn"
            assert sorted(check["forms"]) == ["13_46", "4", "pipe"]
            assert "no process options" in check["detail"].lower()

    def test_a_false_checkbox_or_zero_is_a_value(self):
        check = self._check(self._run({"pipe": {"pipeline_note": False},
                                       "13_46": {"aligner_Parameters": 0},
                                       "4": {"test_name": "t"}}))
        assert check["status"] == "pass"

    def test_a_process_without_form_fields_is_not_a_form(self):
        """Process 7 has only params.* items, so `{}` there is not an empty form."""
        check = self._check(self._run({"pipe": {"pipeline_note": "x"},
                                       "13_46": {"description": "d"},
                                       "4": {"test_name": "t"}, "7": {}}))
        assert check["status"] == "pass"

    def test_pipeline_without_forms_has_no_check(self):
        parsed = _preflight_with(RNASEQ_PARAMS, details=_details(), files=ALL_GOOD)
        assert self._check(parsed) is None

    def test_unreadable_definitions_still_judge_saved_forms_with_fields(self):
        details = _details()
        details["processOptions"] = {"13_46": {"aligner_Parameters": ""}, "281_32": {},
                                     "4": {"test_name": "t"}}
        parsed, _ = _preflight(details=details, files=ALL_GOOD)
        check = self._check(parsed)
        assert check["status"] == "warn"
        assert check["forms"] == ["13_46"]  # `{}` cannot be judged without definitions


def _path_params(default=None, type_="val", name="star_index"):
    item = {"varName": f"params.{name}", "qualifier": type_}
    if default is not None:
        item["defVal"] = default
    return {"pipelineType": "via", "inputs": {
        "pipe": {"schema": [], "inputNodes": [
            {"varName": "params.reads", "qualifier": "set", "optional": True},
            {"varName": "params.genome_build", "qualifier": "val"},
            item,
        ]},
        "processes": {},
    }}


class TestPreflightRequiredPathInputs:
    """A run passed preflight and then failed because its reference path inputs
    were never filled. A required path input must hold a real path."""

    @staticmethod
    def _check(parsed):
        return next(c for c in parsed["data"]["checks"] if c["id"] == "empty_inputs")

    def _run(self, params, extra_inputs):
        inputs = DEFAULT_INPUTS + extra_inputs
        return self._check(_preflight_with(params, details=_details(inputs), files=ALL_GOOD))

    def test_whitespace_value_already_fails(self):
        check = self._run(_path_params(), [_inp("star_index", "   ")])
        assert check["status"] == "fail" and "star_index" in check["detail"]

    def test_never_saved_with_no_default_already_fails(self):
        check = self._run(_path_params(), [])
        assert check["status"] == "fail" and "star_index" in check["detail"]

    def test_placeholder_value_already_fails(self):
        check = self._run(_path_params(type_="file", name="gtf"), [_inp("gtf", "NO_FILE")])
        assert check["status"] == "fail" and "gtf" in check["detail"]

    def test_never_saved_with_a_placeholder_default_fails(self):
        check = self._run(_path_params(default="NO_FILE"), [])
        assert check["status"] == "fail" and "star_index" in check["detail"]

    def test_file_typed_input_with_a_placeholder_default_fails(self):
        check = self._run(_path_params(default="NO_FILE", type_="file", name="annotation"), [])
        assert check["status"] == "fail" and "annotation" in check["detail"]

    def test_na_on_a_path_with_a_placeholder_default_fails(self):
        check = self._run(_path_params(default="NO_FILE"), [_inp("star_index", "NA")])
        assert check["status"] == "fail" and "star_index" in check["detail"]

    def test_a_real_default_still_passes(self):
        check = self._run(_path_params(default="/ref/mm10/star"), [])
        assert check["status"] == "pass"

    def test_a_non_path_input_with_a_placeholder_default_is_unchanged(self):
        check = self._run(_path_params(default="NA", name="extra_args"), [])
        assert check["status"] == "pass"


GB = 1024 ** 3


def _sized_preflight(estimate=None, error=None, input_bytes=None, **kwargs):
    """Preflight with the size estimate route answering `estimate`, or raising
    `error`. Returns the parsed result and the body the route received."""
    client = _client(files=ALL_GOOD, **kwargs)
    inner = client.call.side_effect
    seen = {}

    def _call(**kw):
        if kw.get("endpoint", "").endswith("/size-estimate"):
            seen["body"] = kw.get("data")
            seen["method"] = kw.get("method")
            if error is not None:
                raise error
            return estimate
        return inner(**kw)

    client.call.side_effect = _call
    with patch.object(server, "get_client", return_value=client):
        args = {} if input_bytes is None else {"input_bytes": input_bytes}
        return json.loads(server.preflight_run("12194", **args)), seen


HISTORY = {
    "basis": "history", "runsUsed": 5, "minRuns": 3,
    "inputBytes": 10 * GB, "inputSource": "request", "inputNote": None,
    "fallbackFactor": None, "bytesPerInputByte": 3.0,
    "estimatedWorkBytes": 25 * GB, "estimatedReportBytes": 5 * GB,
    "estimatedBytes": 40 * GB,
    "freeSpace": {"checked": True, "availableBytes": 500 * GB, "checkedPath": "/x"},
    "warn": False,
    "message": "This run is expected to need about 40 GB, based on 5 past runs.",
}

ROUGH = {
    "basis": "no_history_rough", "runsUsed": 1, "minRuns": 3,
    "inputBytes": 2 * GB, "inputSource": "datasets",
    "inputNote": "Not counted in the input size: 1 dataset could not be read.",
    "fallbackFactor": 6, "bytesPerInputByte": None,
    "estimatedWorkBytes": None, "estimatedReportBytes": None,
    "estimatedBytes": 12 * GB,
    "freeSpace": {"checked": False, "reason": "no execution directory"},
    "warn": False,
    "message": "No history: a rough figure, 6 times the input size: about 12 GB.",
}


class TestPreflightSizeEstimate:
    def setup_method(self):
        server._ACCESSION_BYTES.clear()

    def test_history_answer_is_shown_with_its_parts(self):
        parsed, seen = _sized_preflight(HISTORY, input_bytes=10 * GB)
        assert seen["method"] == "POST"
        assert seen["body"] == {"inputBytes": 10 * GB}
        size = parsed["data"]["size_estimate"]
        assert size["available"] is True
        assert size["basis"] == "history"
        assert size["runsUsed"] == 5
        assert size["message"] == HISTORY["message"]
        assert size["estimatedBytes"] == 40 * GB
        assert size["estimated_size"] == "40.0 GB"
        assert size["estimated_parts"] == {
            "inputs": "10.0 GB", "work": "25.0 GB", "report": "5.0 GB"}
        assert size["free_space"] == "500.0 GB"
        assert size["warn"] is False
        assert parsed["data"]["ok"] is True
        assert "WARNING" not in parsed["summary"]

    def test_no_input_bytes_sends_an_empty_body(self):
        _, seen = _sized_preflight(HISTORY)
        assert seen["body"] == {}

    def test_a_dataset_created_from_accessions_supplies_its_raw_total(self):
        server._remember_accession_bytes("69743e8d41a0a599bb36f6a8", 7 * GB)
        _, seen = _sized_preflight(HISTORY)
        assert seen["body"] == {"inputBytes": 7 * GB}

    def test_rough_fallback_keeps_its_note_and_factor(self):
        parsed, _ = _sized_preflight(ROUGH)
        size = parsed["data"]["size_estimate"]
        assert size["basis"] == "no_history_rough"
        assert size["fallbackFactor"] == 6
        assert size["inputNote"] == ROUGH["inputNote"]
        assert size["estimated_size"] == "12.0 GB"
        assert "estimated_parts" not in size
        assert size["freeSpace"] == {"checked": False, "reason": "no execution directory"}
        assert "free_space" not in size
        assert parsed["data"]["ok"] is True

    def test_warn_true_is_a_warning_and_never_a_failure(self):
        warn = dict(HISTORY, warn=True,
                    freeSpace={"checked": True, "availableBytes": 20 * GB, "checkedPath": "/x"})
        parsed, _ = _sized_preflight(warn)
        assert parsed["data"]["size_estimate"]["warn"] is True
        assert parsed["data"]["ok"] is True
        assert parsed["data"]["failures"] == 0
        assert all(c["status"] != "fail" for c in parsed["data"]["checks"])
        assert "WARNING" in parsed["summary"]
        assert "size_estimate" in parsed["next_steps"][0]

    def test_a_route_error_degrades_to_a_note(self):
        parsed, _ = _sized_preflight(error=RuntimeError("500 Server Error"))
        size = parsed["data"]["size_estimate"]
        assert size == {"available": False, "warn": False, "note": size["note"]}
        assert "could not be read" in size["note"]
        assert parsed["data"]["ok"] is True
        assert "error" not in parsed

    def test_an_older_server_without_the_route_degrades_to_a_note(self):
        import requests
        response = MagicMock(status_code=404)
        parsed, _ = _sized_preflight(
            error=requests.HTTPError("Not Found", response=response))
        size = parsed["data"]["size_estimate"]
        assert size["available"] is False
        assert "does not offer" in size["note"]
        assert parsed["data"]["ok"] is True
