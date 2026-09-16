"""Tests for the metadata record update tools.

These are the only tools that change an existing metadata record, so the
checks focus on what reaches the server: the payload passes through
unchanged, reserved keys never leave the tool, and a batch with one bad row
writes nothing at all.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from src.foundry_mcp import server


CANVAS = "69743dc841a0a599bb36d3de"
COLLECTION = "file"


def _client():
    client = MagicMock()
    client.metadata.update_data.side_effect = lambda c, n, rid, data: {"_id": rid, **data}
    return client


class TestRegistration:
    @pytest.mark.parametrize("name", ["update_metadata_record", "update_metadata_records"])
    def test_tool_is_registered_with_a_description(self, name):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        assert name in tools
        assert tools[name].description and tools[name].description.strip()


class TestUpdateMetadataRecord:
    def test_passes_arguments_through_unchanged(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_record(CANVAS, COLLECTION, "r1", {"group": "chow.wt"}))

        client.metadata.update_data.assert_called_once_with(CANVAS, COLLECTION, "r1", {"group": "chow.wt"})
        assert out == {"_id": "r1", "group": "chow.wt"}

    @pytest.mark.parametrize("key", ["_id", "owner", "perms", "DID"])
    def test_rejects_reserved_keys_without_calling_the_server(self, key):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_record(CANVAS, COLLECTION, "r1", {"group": "x", key: "y"}))

        assert key in out["error"]
        client.metadata.update_data.assert_not_called()

    def test_rejects_an_empty_update(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_record(CANVAS, COLLECTION, "r1", {}))

        assert "error" in out
        client.metadata.update_data.assert_not_called()

    def test_server_error_is_returned_as_json(self):
        client = _client()
        client.metadata.update_data.side_effect = Exception("Error 2043: Failed to update data r1: 403")
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_record(CANVAS, COLLECTION, "r1", {"group": "x"}))

        assert out == {"error": "Error 2043: Failed to update data r1: 403"}


class TestUpdateMetadataRecords:
    def test_updates_each_row_and_reports_each_result(self):
        client = _client()
        updates = [
            {"data_id": "r1", "update_data": {"group": "chow.wt"}},
            {"data_id": "r2", "update_data": {"group": "hfd.wt"}},
        ]
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_records(CANVAS, COLLECTION, updates))

        assert [c.args for c in client.metadata.update_data.call_args_list] == [
            (CANVAS, COLLECTION, "r1", {"group": "chow.wt"}),
            (CANVAS, COLLECTION, "r2", {"group": "hfd.wt"}),
        ]
        assert out["updated"] == 2
        assert out["failed"] == 0
        assert [r["data_id"] for r in out["results"]] == ["r1", "r2"]
        assert all(r["ok"] for r in out["results"])

    def test_one_failing_row_does_not_stop_the_others(self):
        client = _client()

        def update(c, n, rid, data):
            if rid == "r1":
                raise Exception("Error 2043: Failed to update data r1: not found")
            return {"_id": rid, **data}

        client.metadata.update_data.side_effect = update
        updates = [
            {"data_id": "r1", "update_data": {"group": "a"}},
            {"data_id": "r2", "update_data": {"group": "b"}},
        ]
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_records(CANVAS, COLLECTION, updates))

        assert out["updated"] == 1
        assert out["failed"] == 1
        assert out["results"][0] == {"data_id": "r1", "ok": False, "error": "Error 2043: Failed to update data r1: not found"}
        assert out["results"][1]["ok"] is True

    @pytest.mark.parametrize(
        "bad_row",
        [
            {"data_id": "r2", "update_data": {"owner": "someone"}},
            {"data_id": "", "update_data": {"group": "b"}},
            {"update_data": {"group": "b"}},
            {"data_id": "r2", "update_data": {}},
            {"data_id": "r1", "update_data": {"group": "dup"}},
        ],
        ids=["reserved-key", "empty-id", "missing-id", "empty-update", "duplicate-id"],
    )
    def test_an_invalid_row_rejects_the_whole_batch_before_any_write(self, bad_row):
        client = _client()
        updates = [{"data_id": "r1", "update_data": {"group": "a"}}, bad_row]
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_records(CANVAS, COLLECTION, updates))

        assert "error" in out
        assert out["invalid_rows"][0]["index"] == 1
        client.metadata.update_data.assert_not_called()

    def test_rejects_an_empty_batch(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_records(CANVAS, COLLECTION, []))

        assert "error" in out
        client.metadata.update_data.assert_not_called()
