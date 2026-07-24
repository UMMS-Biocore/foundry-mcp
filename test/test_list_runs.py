"""Tests for list_runs tag filtering and sort-key normalization."""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _patched_client(*call_returns):
    """MagicMock ViaFoundryClient whose .call returns the given values in order."""
    client = MagicMock()
    if len(call_returns) == 1:
        client.call.return_value = call_returns[0]
    else:
        client.call.side_effect = list(call_returns)
    return client


class TestNormalizeRunSort:
    def test_valid_field_passes_through(self):
        assert server._normalize_run_sort("dateCreated") == "dateCreated"
        assert server._normalize_run_sort("name") == "name"
        assert server._normalize_run_sort("dateCreatedLastRun") == "dateCreatedLastRun"

    def test_datemodified_alias_maps_to_datecreated(self):
        assert server._normalize_run_sort("dateModified") == "dateCreated"
        assert server._normalize_run_sort("date_modified") == "dateCreated"
        assert server._normalize_run_sort("updatedAt") == "dateCreated"

    def test_unknown_and_empty_fall_back_to_datecreated(self):
        assert server._normalize_run_sort("bogus") == "dateCreated"
        assert server._normalize_run_sort("") == "dateCreated"

    def test_pipelineid_is_not_advertised_as_sortable(self):
        """The run-list route builds its sort whitelist as
        `Object.keys(runListAPIDbMap).filter(key => key !== "pipelineId")`, so
        sorting by pipelineId is a hard 400 (verified live). Offering it in the
        tool contract sent models straight into that error."""
        assert "pipelineId" not in server.RUN_LIST_SORT_FIELDS
        assert server._normalize_run_sort("pipelineId") == "pipelineName"


class TestListRunsSort:
    def test_invalid_sort_is_normalized_before_request(self):
        client = _patched_client({"data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_runs(sort="dateModified")
        params = client.call.call_args.kwargs["params"]
        assert params["sort"] == "dateCreated"


class TestListRunsTags:
    def test_no_tags_omits_tagids(self):
        client = _patched_client({"data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_runs()
        client.call.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/run/list",
            params={"take": 10, "skip": 0, "sort": "dateCreated", "order": "desc"},
            data={"searchKey": ""},
        )

    def test_tag_name_resolved_to_id_and_sent_as_tagids(self):
        tag_resp = {"data": [{"id": "uuid-demo", "name": "demo"}, {"id": "uuid-qc", "name": "QC"}]}
        client = _patched_client(tag_resp, {"data": [{"id": 1}]})
        with patch.object(server, "get_client", return_value=client):
            result = server.list_runs(tags="demo")
        # First call resolves tag names for the run entity type.
        first = client.call.call_args_list[0]
        assert first.kwargs["method"] == "GET"
        assert first.kwargs["endpoint"] == "/api/v1/tag"
        assert first.kwargs["params"] == {"entityType": "run"}
        # Second call filters the run list by the resolved tag id.
        second = client.call.call_args_list[1]
        assert second.kwargs["endpoint"] == "/api/v1/run/list"
        assert second.kwargs["data"]["tagIds"] == ["uuid-demo"]
        assert json.loads(result) == {"data": [{"id": 1}]}

    def test_tag_match_is_case_insensitive(self):
        tag_resp = {"data": [{"id": "uuid-qc", "name": "QC"}]}
        client = _patched_client(tag_resp, {"data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_runs(tags="qc")
        assert client.call.call_args_list[1].kwargs["data"]["tagIds"] == ["uuid-qc"]

    def test_unknown_tag_errors_and_skips_run_list(self):
        tag_resp = {"data": [{"id": "uuid-demo", "name": "demo"}]}
        client = _patched_client(tag_resp)
        with patch.object(server, "get_client", return_value=client):
            result = server.list_runs(tags="nope")
        parsed = json.loads(result)
        assert "nope" in parsed["error"]
        assert parsed["available_run_tags"] == ["demo"]
        # Only the tag lookup ran; the run list was never queried.
        assert client.call.call_count == 1
