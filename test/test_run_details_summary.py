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
