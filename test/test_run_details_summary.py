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


_EXTERNAL_DETAILS = {
    "mainPipeline": {"id": 42, "name": "nf-core/rnaseq", "version": "3.14.0"},
    "project": {"id": 2664, "name": "BC_RNAseq"},
    "permission": 3,
    "groupId": None,
    # External (nf-core/Nextflow) pipelines: backend returns a dict keyed by
    # input name, not the Foundry Connect list-of-{name,value,type}.
    "inputs": {
        "genome": {"value": "/pi/x/genome.fa", "type": "input"},
        "aligner": {"value": "star_salmon", "type": "input"},
        "reads": {"value": "AAV_UCP1", "type": "vmetaCollection"},
    },
    "processOptions": {"1_1": {}},
}


class TestGetRunDetailsExternalPipelineDictInputs:
    def test_dict_shaped_inputs_populate_settings_not_empty(self):
        client = _client(_EXTERNAL_DETAILS)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("456"))
        assert parsed["data"]["settings"], "settings must not be silently empty"
        names = {s["name"] for s in parsed["data"]["settings"]}
        assert "aligner" in names

    def test_dict_shaped_path_value_lands_in_reference_paths(self):
        client = _client(_EXTERNAL_DETAILS)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("456"))
        assert "genome" in parsed["data"]["reference_paths"]

    def test_dict_shaped_vmeta_collection_lands_in_sample_inputs(self):
        client = _client(_EXTERNAL_DETAILS)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("456"))
        assert {"name": "reads", "dataset": "AAV_UCP1"} in parsed["data"]["sample_inputs"]


class TestSummaryEnrichment:
    """The compact summary should carry the run's own identity and the dataset
    id needed to re-point samples — both are what a scientist actually asks for."""

    _DETAILS_WITH_RUN = {
        "name": "UCP1_AAV (Duplicated)",
        "summary": "<p>UCP1 vs GFP AAV Mice&nbsp;</p>",
        "mainPipeline": {"id": 1539, "name": "PMM RNASeq DE", "version": "2.0.0"},
        "project": {"id": 2664, "name": "BC_RNAseq"},
        "inputs": [
            {
                "name": "reads",
                "type": "vmetaCollection",
                "value": "AAV_UCP1_GFP_Ben",
                "vmetaCollectionId": "6a4fbd740cc2ec76b722219c",
            },
        ],
        "processOptions": {},
    }

    def test_surfaces_run_name_and_plaintext_description(self):
        client = _client(self._DETAILS_WITH_RUN)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("12219"))
        assert parsed["data"]["run"]["name"] == "UCP1_AAV (Duplicated)"
        # HTML tags stripped and entities decoded for chat display
        assert parsed["data"]["run"]["description"] == "UCP1 vs GFP AAV Mice"
        # the human name, not just the numeric id, leads the summary
        assert "UCP1_AAV (Duplicated)" in parsed["summary"]

    def test_keeps_vmeta_collection_id_for_repointing_samples(self):
        client = _client(self._DETAILS_WITH_RUN)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("12219"))
        sample = parsed["data"]["sample_inputs"][0]
        assert sample["dataset"] == "AAV_UCP1_GFP_Ben"
        assert sample["vmetaCollectionId"] == "6a4fbd740cc2ec76b722219c"

    def test_falls_back_to_run_id_when_name_missing(self):
        details = dict(self._DETAILS_WITH_RUN)
        details.pop("name")
        client = _client(details)
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.get_run_details("12219"))
        assert "#12219" in parsed["summary"]
