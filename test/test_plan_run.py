"""Tests for plan_run — reducing a pipeline's inputs to the decisions a bench
scientist actually has to make.

The fixtures mirror live run 12194 (RNA-seq Pipeline on staging), which has
**70 inputs and 61 process-option groups**. That is the friction this tool
exists to remove.
"""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _inp(name, value, type_="input", **extra):
    row = {"id": 1, "name": name, "type": type_, "value": value,
           "collectionId": None, "vmetaCollectionId": "", "serviceTokenId": None}
    row.update(extra)
    return row


REFERENCE_PATHS = [
    _inp("genome", "/share/data/umw_biocore/genome_data/mm10/genome.fa"),
    _inp("bowtie2_index", "/share/data/umw_biocore/genome_data/mm10/bowtie2"),
    _inp("star_index", "/share/data/umw_biocore/genome_data/mm10/star"),
    _inp("genome_source", "https://web.dolphinnext.com/dnext_data/genome.fa"),
    _inp("gtf_source", "https://web.dolphinnext.com/dnext_data/genes.gtf"),
    _inp("pdfbox_path", "/usr/local/bin/dolphin-tools/pdfbox-app.jar"),
]

STEP_TOGGLES = (
    [_inp(f"run_{n}", "yes") for n in
     ("FastQC", "STAR", "RSEM", "HISAT2", "Kallisto", "Salmon",
      "DESeq2_after_RSEM")]
    + [_inp(f"run_{n}", "no") for n in
       ("Adapter_Removal", "UMIextract", "Trimmer", "Quality_Filtering",
        "RSeQC", "BigWig_Conversion", "IGV_TDF_Conversion",
        "gsea_DESeq2_RSEM", "gsea_DESeq2_Salmon")]
)

DECISIONS = [
    _inp("reads", "rna_seq mousetest paired-2", "vmetaCollection",
         vmetaCollectionId="69743e8d41a0a599bb36f6a8"),
    _inp("mate", "pair"),
    _inp("genome_build", "mousetest_mm10"),
    _inp("groups_file", "/pi/kucukura/foundryUploads/run12194/metadata.tsv"),
    _inp("compare_file", "/pi/kucukura/foundryUploads/run12194/compare.tsv"),
    _inp("gtf_type", "ncbi"),
    _inp("replace_geneID_with_geneName", "yes"),
    _inp("add_sequences_to_reference", "no"),
]

DETAILS = {
    "name": "Mouse Aligner Tests",
    "mainPipeline": {"id": 1405, "name": "RNA-seq Pipeline", "version": "2.7.4"},
    "project": {"id": 1841, "name": "Test project"},
    "permission": 2, "groupId": 7,
    "inputs": REFERENCE_PATHS + STEP_TOGGLES + DECISIONS,
    "processOptions": {f"proc_{i}": {"cpu": 1} for i in range(61)},
}


def _client(details=DETAILS, example_run=True):
    c = MagicMock()
    runs = {"total": 1, "take": 1, "skip": 0, "data": [
        {"id": 12194, "name": "Mouse Aligner Tests", "status": "Completed",
         "dateCreated": "2026-07-15T20:47:14.000Z"}]} if example_run else {
        "total": 0, "take": 1, "skip": 0, "data": []}

    def _call(**kwargs):
        return runs if kwargs.get("endpoint") == "/api/v1/run/list" else details

    c.call.side_effect = _call
    return c


def _plan(details=DETAILS, example_run=True, **kwargs):
    client = _client(details, example_run)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.plan_run(1405, **kwargs)), client


class TestPlanRun:
    def test_reduces_seventy_inputs_to_at_most_eight_decisions(self):
        parsed, _ = _plan()
        assert len(DETAILS["inputs"]) == 70 - 40  # fixture is a faithful subset
        assert len(parsed["data"]["decisions"]) <= 8

    def test_hides_reference_paths_and_index_locations(self):
        parsed, _ = _plan()
        shown = {d["input"] for d in parsed["data"]["decisions"] if "input" in d}
        for hidden in ("genome", "bowtie2_index", "star_index", "genome_source",
                       "gtf_source", "pdfbox_path"):
            assert hidden not in shown

    def test_never_hides_the_users_own_design_files(self):
        """groups_file/compare_file are absolute paths too, but they encode the
        experiment — hiding them with the reference paths would hide the single
        most important thing a scientist has to get right."""
        parsed, _ = _plan()
        shown = {d["input"] for d in parsed["data"]["decisions"] if "input" in d}
        assert "groups_file" in shown
        assert "compare_file" in shown

    def test_samples_come_first(self):
        parsed, _ = _plan()
        first = parsed["data"]["decisions"][0]
        assert first["input"] == "reads"
        assert first["kind"] == "samples"
        assert first["vmetaCollectionId"] == "69743e8d41a0a599bb36f6a8"

    def test_collapses_the_run_toggles_into_a_single_decision(self):
        """16 run_* switches are one decision ("which steps"), not 16."""
        parsed, _ = _plan()
        steps = [d for d in parsed["data"]["decisions"] if d["kind"] == "steps"]
        assert len(steps) == 1
        assert "FastQC" in steps[0]["enabled"]
        assert "STAR" in steps[0]["enabled"]
        assert "Trimmer" not in steps[0]["enabled"]
        assert steps[0]["disabled_count"] == 9
        assert steps[0]["allowed"] == ["yes", "no"]

    def test_yes_no_settings_advertise_their_allowed_values(self):
        parsed, _ = _plan()
        toggle = next(d for d in parsed["data"]["decisions"]
                      if d.get("input") == "replace_geneID_with_geneName")
        assert toggle["allowed"] == ["yes", "no"]
        assert toggle["current"] == "yes"

    def test_free_text_settings_do_not_invent_allowed_values(self):
        """Run details carry no schema — only name/type/value. Claiming to know
        the allowed set would be a fabrication."""
        parsed, _ = _plan()
        build = next(d for d in parsed["data"]["decisions"]
                     if d.get("input") == "genome_build")
        assert build["allowed"] is None
        assert build["current"] == "mousetest_mm10"

    def test_labels_are_plain_language_not_variable_names(self):
        parsed, _ = _plan()
        labels = {d.get("input"): d["label"] for d in parsed["data"]["decisions"]}
        assert labels["reads"] != "reads"
        assert "sample" in labels["reads"].lower()
        assert "condition" in labels["groups_file"].lower()
        assert "paired" in labels["mate"].lower()

    def test_what_it_withheld_is_stated_not_silently_dropped(self):
        parsed, _ = _plan()
        data = parsed["data"]
        assert data["process_option_groups"] == 61
        assert data["hidden_reference_paths"] == 6
        assert data["further_settings_not_shown"] >= 1
        assert "61" in parsed["summary"]

    def test_reports_the_run_it_planned_from(self):
        parsed, _ = _plan()
        assert parsed["data"]["based_on_run"]["id"] == 12194
        assert parsed["data"]["pipeline"]["name"] == "RNA-seq Pipeline"

    def test_next_steps_spell_out_the_launch_sequence_and_the_cost(self):
        parsed, _ = _plan()
        joined = " ".join(parsed["next_steps"])
        for step in ("duplicate_run", "update_run", "initiate_run"):
            assert step in joined
        assert "HPC" in joined or "compute" in joined

    def test_planning_from_an_explicit_run_skips_the_lookup(self):
        parsed, client = _plan(run_id="12193")
        endpoints = [c.kwargs.get("endpoint") for c in client.call.call_args_list]
        assert "/api/v1/run/list" not in endpoints
        assert "/api/v1/run/12193/details" in endpoints

    def test_no_example_run_is_reported_honestly(self):
        parsed, _ = _plan(example_run=False)
        assert "error" not in parsed
        assert parsed["data"]["decisions"] == []
        assert "no successful run" in parsed["summary"].lower()
        assert "list_runs" in " ".join(parsed["next_steps"])

    def test_handles_the_dict_shaped_inputs_of_external_pipelines(self):
        """nf-core/Nextflow pipelines return inputs as {name: {...}}, not a
        list — the shape that silently emptied the Phase 0 run summary."""
        details = dict(DETAILS, inputs={i["name"]: i for i in DECISIONS})
        parsed, _ = _plan(details)
        assert parsed["data"]["decisions"][0]["input"] == "reads"

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.plan_run(1405)
        assert json.loads(result) == {"error": "boom"}
