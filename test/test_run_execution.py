"""Tests for run-execution tools."""
import json
from unittest.mock import MagicMock, patch

from src.viafoundry_mcp import server


def _patched_client(call_return):
    """Return a MagicMock ViaFoundryClient whose .call returns call_return."""
    client = MagicMock()
    client.call.return_value = call_return
    return client


class TestGetRunDetails:
    def test_hits_details_endpoint_and_returns_json(self):
        details = {
            "id": 123, "inputs": [], "processOptions": {},
            "permission": 2, "groupId": 5, "mainPipeline": {"id": 1408},
        }
        client = _patched_client(details)
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_details("123")
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/run/123/details"
        )
        assert json.loads(result)["groupId"] == 5

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_details("123")
        assert json.loads(result) == {"error": "boom"}


class TestCreateVmetaDataset:
    def test_posts_dataset_create_with_name(self):
        client = _patched_client({"_id": "6984ba1e8518d10eb6fe636d", "name": "run42"})
        with patch.object(server, "get_client", return_value=client):
            result = server.create_vmeta_dataset("run42")
        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/vmeta/dataset/create",
            data={"name": "run42"},
        )
        assert json.loads(result)["_id"] == "6984ba1e8518d10eb6fe636d"

    def test_rejects_empty_name(self):
        client = MagicMock()
        with patch.object(server, "get_client", return_value=client):
            result = server.create_vmeta_dataset("   ")
        assert "error" in json.loads(result)
        client.call.assert_not_called()


class TestDuplicateRun:
    def test_posts_duplicate_endpoint(self):
        client = _patched_client({"runId": 999})
        with patch.object(server, "get_client", return_value=client):
            result = server.duplicate_run("123")
        client.call.assert_called_once_with(
            method="POST", endpoint="/api/v1/run/123/duplicate", data={}
        )
        assert json.loads(result)["runId"] == 999


class TestUpdateRun:
    def _valid_args(self):
        return dict(
            run_id="999",
            inputs=[{"id": 1, "type": "input", "name": "genome", "value": "hg38"}],
            process_options={"5": {"sample_id": ["a", "b"], "group": ["g", "g"]}},
            permission=2,
            group_id=5,
        )

    def test_patches_save_endpoint_with_groupId_key(self):
        client = _patched_client({"ok": True})
        args = self._valid_args()
        with patch.object(server, "get_client", return_value=client):
            result = server.update_run(**args)
        client.call.assert_called_once_with(
            method="PATCH",
            endpoint="/api/v1/run/999/save",
            data={
                "inputs": args["inputs"],
                "processOptions": args["process_options"],
                "permission": 2,
                "groupId": 5,
            },
        )
        assert json.loads(result) == {"ok": True}

    def test_rejects_empty_string_value(self):
        client = MagicMock()
        args = self._valid_args()
        args["inputs"] = [{"id": 1, "type": "input", "name": "x", "value": ""}]
        with patch.object(server, "get_client", return_value=client):
            result = server.update_run(**args)
        assert "not allowed to be empty" in json.loads(result)["error"]
        client.call.assert_not_called()

    def test_rejects_missing_group_id(self):
        client = MagicMock()
        args = self._valid_args()
        args["group_id"] = None
        with patch.object(server, "get_client", return_value=client):
            result = server.update_run(**args)
        assert "groupId" in json.loads(result)["error"]
        client.call.assert_not_called()

    def test_rejects_mismatched_spreadsheet_arrays(self):
        client = MagicMock()
        args = self._valid_args()
        args["process_options"] = {"5": {"sample_id": ["a", "b"], "group": ["g"]}}
        with patch.object(server, "get_client", return_value=client):
            result = server.update_run(**args)
        assert "length" in json.loads(result)["error"].lower()
        client.call.assert_not_called()
