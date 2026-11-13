"""Tests for the metadata field update and delete tools.

These tools change a field definition, which every record in its collection
is validated against, so the checks focus on what reaches the server:

- update sends only the keys the caller gave, plus the current value of
  every boolean flag the server would otherwise reset. The server's update
  schema defaults those flags to false, so a PATCH of {"enum": [...]} alone
  would quietly turn a required field into an optional one.
- unknown keys, access keys and an empty update never leave the tool.
- delete writes nothing unless confirm_delete is exactly True.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from src.foundry_mcp import server

FIELD_ID = "6977762ac783bd6d7ed0db01"

CURRENT_FIELD = {
    "_id": FIELD_ID,
    "name": "tissue",
    "label": "Tissue",
    "type": "String",
    "collectionID": "65c21d6a593f32e0103daf25",
    "enum": ["liver", "heart"],
    "required": True,
    "unique": False,
    "hidden": False,
    "deprecated": False,
    "trim": True,
    "ontology": {"include": ["UBERON", True]},
}


def _client(current=None):
    client = MagicMock()
    client.metadata.get_field.return_value = {"status": "success", "data": dict(current or CURRENT_FIELD)}
    client.metadata.update_field.side_effect = lambda fid, data: {"status": "success", "data": {"_id": fid, **data}}
    client.metadata.delete_field.return_value = {"status": "success", "data": "Deleted!"}
    return client


class TestRegistration:
    @pytest.mark.parametrize("name", ["update_metadata_field", "delete_metadata_field"])
    def test_tool_is_registered_with_a_warning(self, name):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        assert name in tools
        assert "WARNING" in tools[name].description


class TestUpdateMetadataField:
    def test_sends_the_given_keys_and_keeps_current_flags(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            json.loads(server.update_metadata_field(FIELD_ID, {"enum": ["liver", "heart", "lung"], "label": "Organ"}))

        client.metadata.update_field.assert_called_once()
        fid, sent = client.metadata.update_field.call_args.args
        assert fid == FIELD_ID
        assert sent["enum"] == ["liver", "heart", "lung"]
        assert sent["label"] == "Organ"
        # Flags the server would reset are sent back with their current value.
        assert sent["required"] is True
        assert sent["trim"] is True
        assert sent["unique"] is False
        # Keys that are neither given nor reset-prone are left alone.
        assert "name" not in sent
        assert "ontology" not in sent
        assert "type" not in sent

    def test_sends_a_plain_dict_so_sdk_model_defaults_never_apply(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            server.update_metadata_field(FIELD_ID, {"label": "Organ"})

        _, sent = client.metadata.update_field.call_args.args
        assert type(sent) is dict

    def test_a_flag_absent_on_the_field_is_not_invented(self):
        client = _client({"_id": FIELD_ID, "name": "x", "required": True})
        with patch.object(server, "get_client", return_value=client):
            server.update_metadata_field(FIELD_ID, {"label": "X"})

        _, sent = client.metadata.update_field.call_args.args
        assert sent == {"label": "X", "required": True}

    def test_a_flag_the_caller_gives_wins_over_the_current_value(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            server.update_metadata_field(FIELD_ID, {"required": False})

        _, sent = client.metadata.update_field.call_args.args
        assert sent["required"] is False

    def test_ontology_passes_through_untouched(self):
        ontology = {"include": ["UBERON", True], "exclude": [None, 3]}
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            server.update_metadata_field(FIELD_ID, {"ontology": ontology})

        _, sent = client.metadata.update_field.call_args.args
        assert sent["ontology"] == {"include": ["UBERON", True], "exclude": [None, 3]}

    def test_returns_the_server_response(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_field(FIELD_ID, {"label": "Organ"}))

        assert out["data"]["label"] == "Organ"

    @pytest.mark.parametrize("update", [{}, None, "label=Organ", ["label"]])
    def test_rejects_an_empty_or_non_object_update(self, update):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_field(FIELD_ID, update))

        assert "error" in out
        client.metadata.update_field.assert_not_called()

    @pytest.mark.parametrize("field_id", ["", "   ", None, "../run/123", "6977762ac783bd6d7ed0db01/../../run/1", "6977762ac783bd6d7ed0db0", "zz77762ac783bd6d7ed0db01"])
    def test_rejects_a_missing_field_id(self, field_id):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_field(field_id, {"label": "X"}))

        assert "field_id" in out["error"]
        client.metadata.update_field.assert_not_called()

    @pytest.mark.parametrize("key", ["active", "collectionID", "_id", "owner", "perms", "colour"])
    def test_rejects_keys_outside_the_allowlist(self, key):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_field(FIELD_ID, {"label": "X", key: "y"}))

        assert key in out["error"]
        client.metadata.update_field.assert_not_called()
        client.metadata.get_field.assert_not_called()

    def test_writes_nothing_when_the_field_cannot_be_read(self):
        client = _client()
        client.metadata.get_field.side_effect = Exception("Error 2023: not found")
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_field(FIELD_ID, {"label": "X"}))

        assert "not found" in out["error"]
        client.metadata.update_field.assert_not_called()

    def test_writes_nothing_when_the_read_returns_another_field(self):
        client = _client({"_id": "aaaaaaaaaaaaaaaaaaaaaaaa", "required": True})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_field(FIELD_ID, {"label": "X"}))

        assert "error" in out
        client.metadata.update_field.assert_not_called()

    def test_refuses_when_a_current_flag_cannot_be_sent_back(self):
        # The create schema allows `required` to be an array; the update
        # schema only takes a boolean, and leaving it out resets it to false.
        client = _client({"_id": FIELD_ID, "required": ["a", "b"]})
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_field(FIELD_ID, {"label": "X"}))

        assert "required" in out["error"]
        client.metadata.update_field.assert_not_called()

    def test_reports_a_server_error(self):
        client = _client()
        client.metadata.update_field.side_effect = Exception("Error 2024: forbidden")
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.update_metadata_field(FIELD_ID, {"label": "X"}))

        assert "forbidden" in out["error"]


class TestDeleteMetadataField:
    def test_deletes_when_confirmed(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.delete_metadata_field(FIELD_ID, confirm_delete=True))

        client.metadata.delete_field.assert_called_once_with(FIELD_ID)
        assert out["data"] == "Deleted!"

    @pytest.mark.parametrize("confirm", [False, None, "true", 1])
    def test_refuses_unless_confirm_delete_is_true(self, confirm):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.delete_metadata_field(FIELD_ID, confirm_delete=confirm))

        assert "confirm_delete" in out["error"]
        client.metadata.delete_field.assert_not_called()

    def test_confirm_delete_defaults_to_refusal(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.delete_metadata_field(FIELD_ID))

        assert "error" in out
        client.metadata.delete_field.assert_not_called()

    @pytest.mark.parametrize("field_id", ["", "   ", None, "../run/123", "6977762ac783bd6d7ed0db01/../../run/1", "6977762ac783bd6d7ed0db0", "zz77762ac783bd6d7ed0db01"])
    def test_rejects_a_missing_field_id(self, field_id):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.delete_metadata_field(field_id, confirm_delete=True))

        assert "field_id" in out["error"]
        client.metadata.delete_field.assert_not_called()

    def test_reports_a_server_error(self):
        client = _client()
        client.metadata.delete_field.side_effect = Exception("Error 2025: forbidden")
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.delete_metadata_field(FIELD_ID, confirm_delete=True))

        assert "forbidden" in out["error"]
