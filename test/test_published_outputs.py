"""Tests for list_published_outputs (where each final output of a run was published)."""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _client(call_return=None, side_effect=None):
    c = MagicMock()
    if side_effect is not None:
        c.call.side_effect = side_effect
    else:
        c.call.return_value = call_return
    return c


def _answer(**overrides):
    body = {
        "publishDir": "/scratch/runs/demo/report12",
        "storage": "cluster",
        "outputs": [
            {
                "processName": "STAR_align",
                "outputName": "aligned_bam",
                "dir": "/scratch/runs/demo/report12/aligned_bam",
                "files": ["/scratch/runs/demo/report12/aligned_bam/s1.bam"],
                "fileCount": 1,
            },
            {
                "processName": "featureCounts",
                "outputName": "counts",
                "dir": "/scratch/runs/demo/report12/counts",
                "files": [],
                "fileCount": 0,
            },
        ],
        "totalFiles": 1,
        "truncated": False,
    }
    body.update(overrides)
    return body


def _run(run_id="12", attempt_id=None, **client_kwargs):
    client = _client(**client_kwargs)
    with patch.object(server, "get_client", return_value=client):
        if attempt_id is None:
            raw = server.list_published_outputs(run_id)
        else:
            raw = server.list_published_outputs(run_id, attempt_id=attempt_id)
    return client, json.loads(raw)


class TestListPublishedOutputs:
    def test_calls_the_published_outputs_route(self):
        client, _ = _run(call_return=_answer())
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/run/12/published-outputs", params=None
        )

    def test_passes_attempt_id_when_given(self):
        client, _ = _run(attempt_id=7, call_return=_answer())
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/run/12/published-outputs",
            params={"attemptId": 7},
        )

    def test_envelope_carries_the_server_answer(self):
        _, result = _run(call_return=_answer())
        assert set(result) >= {"summary", "next_steps", "data"}
        data = result["data"]
        assert data["publish_dir"] == "/scratch/runs/demo/report12"
        assert data["storage"] == "cluster"
        assert data["total_files"] == 1
        assert data["truncated"] is False
        assert data["outputs"][0] == {
            "process": "STAR_align",
            "output": "aligned_bam",
            "dir": "/scratch/runs/demo/report12/aligned_bam",
            "files": ["/scratch/runs/demo/report12/aligned_bam/s1.bam"],
            "file_count": 1,
        }
        assert data["outputs"][1]["files"] == []

    def test_summary_explains_dir_files_and_truncated(self):
        _, result = _run(call_return=_answer())
        summary = result["summary"]
        assert "2 outputs" in summary
        assert "/scratch/runs/demo/report12" in summary
        assert "dir" in summary and "run environment" in summary
        assert "empty list does not mean the folder is empty" in summary
        assert "truncated" in summary

    def test_truncated_answer_says_paths_were_left_out(self):
        _, result = _run(call_return=_answer(truncated=True))
        assert "left out" in result["summary"]
        assert result["data"]["truncated"] is True

    def test_object_store_run_names_the_storage(self):
        answer = _answer(publishDir="s3://bucket/runs/report12", storage="s3")
        _, result = _run(call_return=answer)
        assert "s3" in result["summary"]
        assert result["data"]["storage"] == "s3"

    def test_no_publish_directory_known(self):
        answer = {"publishDir": "", "storage": "cluster", "outputs": [],
                  "totalFiles": 0, "truncated": False}
        _, result = _run(call_return=answer)
        assert "no publish directory" in result["summary"].lower()
        assert result["data"]["outputs"] == []

    def test_glob_published_output_has_no_process(self):
        answer = _answer(outputs=[{
            "processName": None, "outputName": "multiqc",
            "dir": "/scratch/runs/demo/report12/multiqc",
            "files": [], "fileCount": 0,
        }])
        _, result = _run(call_return=answer)
        assert result["data"]["outputs"][0]["process"] is None
        assert result["data"]["outputs"][0]["output"] == "multiqc"

    def test_not_found_names_both_causes(self):
        _, result = _run(side_effect=RuntimeError(
            "Error 305: Not Found: Run not found or you do not have access"))
        assert "error" in result
        text = result["error"]
        assert "12" in text
        assert "not found" in text.lower()
        assert "does not have access" in text or "cannot read" in text
        assert "older" in text

    def test_other_errors_are_reported(self):
        _, result = _run(side_effect=RuntimeError("Error 306: Internal Server Error"))
        assert "error" in result
        assert "Internal Server Error" in result["error"]

    def test_requires_a_run_id(self):
        _, result = _run(run_id="", call_return=_answer())
        assert "error" in result
