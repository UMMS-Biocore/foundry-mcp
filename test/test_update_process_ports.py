"""update_process keeps stored port ids.

The server updates a port in place only when the request names its id. Before
this, the SDK model dropped every id, so each update deleted and recreated
every port: new ids, and output ports lost their stored publish pattern. These
tests pin the request body the tool actually sends.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from foundry_mcp import server
from foundry_mcp.process_ports import (
    PortMergeError,
    build_update_body,
    merge_side,
    stored_port_to_request,
)


def _row(id, name, parameter_id=7, **extra):
    row = {
        "id": id, "parameter_id": parameter_id, "sname": name,
        "operator": "", "closure": "", "reg_ex": None, "optional": "",
        "test": "", "name": "catalog", "file_type": "fastq", "qualifier": "file",
    }
    row.update(extra)
    return row


STORED = {
    "id": 5,
    "owner_id": 1,
    "name": "align",
    "inputs": [_row(11, "reads"), _row(12, "genome", parameter_id=8, test="hg38.fa")],
    "outputs": [_row(21, "bam", parameter_id=9, reg_ex="*.bam", operator="mode", closure="flatten")],
}

BASE = {
    "name": "align",
    "summary": "",
    "menuGroupId": 1,
    "revisionComment": "edit",
    "script": {"body": "echo new", "header": "", "footer": "", "language": "bash"},
    "permissionSettings": {"viewPermissions": 3},
}


class TestStoredPortToRequest:
    def test_maps_get_fields_to_put_fields(self):
        port = stored_port_to_request(STORED["outputs"][0])
        assert port == {
            "id": 21, "parameterId": 9, "displayName": "bam", "operator": "mode",
            "operatorContent": "flatten", "optional": False, "test": "", "regEx": "*.bam",
        }

    def test_decodes_the_stored_display_name(self):
        # The server encodes displayName on the way in; sending the stored value
        # back as is would encode it a second time on every save.
        port = stored_port_to_request(_row(1, "a &amp; b"))
        assert port["displayName"] == "a & b"

    def test_reads_optional_from_its_stored_string(self):
        assert stored_port_to_request(_row(1, "x", optional="true"))["optional"] is True
        assert stored_port_to_request(_row(1, "x", optional=""))["optional"] is False


class TestMergeSide:
    def test_matches_by_id_and_keeps_unset_fields(self):
        ports, changes = merge_side("outputParameters", STORED["outputs"], [{"id": 21, "displayName": "aligned"}])
        assert ports == [{
            "id": 21, "parameterId": 9, "displayName": "aligned", "operator": "mode",
            "operatorContent": "flatten", "optional": False, "test": "", "regEx": "*.bam",
        }]
        assert changes["updated"] == ["aligned"]

    def test_matches_by_display_name_when_no_id_is_given(self):
        ports, changes = merge_side("inputParameters", STORED["inputs"], [
            {"displayName": "reads", "parameterId": 7, "test": "", "optional": True},
            {"displayName": "genome"},
        ])
        assert [p["id"] for p in ports] == [11, 12]
        assert ports[1]["test"] == "hg38.fa"
        assert changes["updated"] == ["reads"] and changes["kept"] == ["genome"]

    def test_an_unmatched_port_is_added_without_an_id(self):
        new = {"displayName": "index", "parameterId": 10, "test": ""}
        ports, changes = merge_side("inputParameters", STORED["inputs"], [{"id": 11}, {"id": 12}, new])
        assert "id" not in ports[2]
        assert changes["added"] == ["index"]

    def test_a_left_out_port_is_reported_as_removed(self):
        _, changes = merge_side("inputParameters", STORED["inputs"], [{"id": 11}])
        assert changes["removed"] == ["genome"]

    def test_accepts_ports_echoed_in_the_get_shape(self):
        echoed = dict(STORED["inputs"][0])
        ports, _ = merge_side("inputParameters", STORED["inputs"], [echoed])
        assert ports[0]["id"] == 11
        assert not {"sname", "parameter_id", "closure", "reg_ex", "name", "file_type", "qualifier"} & set(ports[0])

    def test_refuses_an_id_stored_on_the_other_side(self):
        with pytest.raises(PortMergeError, match="port id 21"):
            merge_side("inputParameters", STORED["inputs"], [{"id": 21}])

    def test_refuses_the_same_id_twice(self):
        with pytest.raises(PortMergeError, match="more than once"):
            merge_side("inputParameters", STORED["inputs"], [{"id": 11}, {"id": 11}])

    def test_refuses_an_ambiguous_display_name(self):
        rows = [_row(1, "reads"), _row(2, "reads")]
        with pytest.raises(PortMergeError, match="2 stored ports named 'reads'"):
            merge_side("inputParameters", rows, [{"displayName": "reads"}])

    def test_an_id_match_is_not_taken_by_a_name_match(self):
        rows = [_row(1, "reads"), _row(2, "reads")]
        ports, _ = merge_side("inputParameters", rows, [{"displayName": "reads"}, {"id": 1}])
        assert [p["id"] for p in ports] == [2, 1]


class TestBuildUpdateBody:
    def test_a_side_left_out_is_sent_back_as_stored(self):
        body, summary = build_update_body(dict(BASE), STORED)
        assert [p["id"] for p in body["inputParameters"]] == [11, 12]
        assert [p["id"] for p in body["outputParameters"]] == [21]
        assert body["outputParameters"][0]["regEx"] == "*.bam"
        assert summary["inputParameters"]["kept"] == ["reads", "genome"]

    def test_an_empty_side_without_the_flag_is_refused(self):
        with pytest.raises(PortMergeError, match="removeAllOutputParameters"):
            build_update_body({**BASE, "outputParameters": []}, STORED)

    def test_an_empty_side_with_the_flag_is_allowed(self):
        body, summary = build_update_body(
            {**BASE, "outputParameters": [], "removeAllOutputParameters": True}, STORED)
        assert body["outputParameters"] == []
        assert summary["outputParameters"]["removed"] == ["bam"]


def _client(stored=STORED):
    client = MagicMock()

    def call(method, endpoint, **kwargs):
        if endpoint == "/api/v1/auth/user":
            return {"id": 1}
        if endpoint == f"/api/v1/process/{stored['id']}":
            return json.loads(json.dumps(stored))
        raise AssertionError(f"unexpected call {method} {endpoint}")

    client.call.side_effect = call
    client.process.update_process.return_value = {"id": 5, "name": "align"}
    return client


def _sent_body(client):
    config = client.process.update_process.call_args.args[1]
    return config.model_dump(mode="json", exclude_none=True, by_alias=True)


class TestUpdateProcessTool:
    def test_a_script_only_edit_keeps_every_port_id(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            result = json.loads(server.update_process("5", dict(BASE)))
        sent = _sent_body(client)
        assert [p["id"] for p in sent["inputParameters"]] == [11, 12]
        assert [p["id"] for p in sent["outputParameters"]] == [21]
        assert sent["outputParameters"][0]["regEx"] == "*.bam"
        assert sent["inputParameters"][1]["test"] == "hg38.fa"
        assert result["port_changes"]["outputParameters"]["removed"] == []

    def test_sends_only_fields_the_server_accepts(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            server.update_process("5", {**BASE, "inputParameters": STORED["inputs"]})
        allowed = {"id", "parameterId", "displayName", "operator", "operatorContent", "optional", "test", "regEx"}
        for port in _sent_body(client)["inputParameters"]:
            assert set(port) <= allowed

    def test_a_refused_merge_sends_nothing(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            result = json.loads(server.update_process("5", {**BASE, "inputParameters": [{"id": 999}]}))
        assert result["error"] == "Ports could not be applied"
        client.process.update_process.assert_not_called()

    def test_an_sdk_without_port_ids_is_refused(self):
        from viafoundry.models.domain.process import ConfigParameter
        client = _client()
        fields = {k: v for k, v in ConfigParameter.model_fields.items() if k != "id"}
        with patch.object(server, "get_client", return_value=client), \
             patch.object(ConfigParameter, "model_fields", fields):
            result = json.loads(server.update_process("5", dict(BASE)))
        assert "cannot keep process port ids" in result["error"]
        client.process.update_process.assert_not_called()

    def test_another_users_process_is_still_refused(self):
        client = _client({**STORED, "owner_id": 2})
        with patch.object(server, "get_client", return_value=client):
            result = json.loads(server.update_process("5", dict(BASE)))
        assert result["error"] == "Ownership check failed"
        client.process.update_process.assert_not_called()
