"""Tests for list_datasets: scope filters, paging and row trimming."""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


USER = {"id": 7, "vmeta": {"userId": "vmeta-me"}, "username": "me@example.com"}

DATASET_ROW = {
    "_id": "ds1",
    "name": "mouse reads",
    "owner": {"_id": "vmeta-me", "username": "me@example.com", "email": "me@example.com", "name": "Me"},
    "createdAt": "2026-10-01T00:00:00Z",
    "updatedAt": "2026-10-02T00:00:00Z",
    "perms": {"read": {"user": ["vmeta-me"]}},
    "file_ids": {"canvas-a": ["f1", "f2"], "canvas-b": ["f3"]},
    "fileCount": 3,
    "tags": [{"id": "t1", "name": "demo", "color": "#fff", "isActive": True}],
}


def _patched_client(*call_returns):
    """MagicMock ViaFoundryClient whose .call returns the given values in order."""
    client = MagicMock()
    client.call.side_effect = list(call_returns)
    return client


def _search_call(client):
    calls = [c for c in client.call.call_args_list if c.kwargs["endpoint"] == "/api/v1/vmeta/dataset/search"]
    assert len(calls) == 1
    return calls[0]


class TestListDatasetsScope:
    def test_default_scope_is_mine_and_filters_by_vmeta_owner(self):
        client = _patched_client(USER, {"results": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_datasets()
        first = client.call.call_args_list[0]
        assert first.kwargs == {"method": "GET", "endpoint": "/api/v1/auth/user"}
        call = _search_call(client)
        assert call.kwargs["method"] == "POST"
        assert call.kwargs["data"] == {
            "filter": {"owner._id": {"eq": "vmeta-me"}},
            "take": 20,
            "skip": 0,
            "sort": "updatedAt",
            "order": "desc",
        }

    def test_shared_scope_excludes_own_and_public(self):
        client = _patched_client(USER, {"results": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_datasets(scope="shared")
        assert _search_call(client).kwargs["data"]["filter"] == {
            "owner._id": {"ne": "vmeta-me"},
            "perms.read.user": {"nin": "everyone"},
        }

    def test_public_scope_needs_no_user_lookup(self):
        client = _patched_client({"results": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_datasets(scope="public")
        assert client.call.call_count == 1
        assert _search_call(client).kwargs["data"]["filter"] == {"perms.read.user": {"in": "everyone"}}

    def test_all_scope_sends_no_filter(self):
        client = _patched_client({"results": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_datasets(scope="all")
        assert client.call.call_count == 1
        assert "filter" not in _search_call(client).kwargs["data"]

    def test_unknown_scope_errors_without_calling_backend(self):
        client = _patched_client()
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_datasets(scope="everything"))
        assert "scope" in parsed["error"]
        assert client.call.call_count == 0

    def test_scope_is_case_insensitive(self):
        client = _patched_client({"results": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_datasets(scope="ALL")
        assert "filter" not in _search_call(client).kwargs["data"]

    def test_mine_without_vmeta_id_errors(self):
        client = _patched_client({"id": 7, "vmeta": {"userId": None}})
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_datasets())
        assert "error" in parsed
        assert client.call.call_count == 1


class TestListDatasetsQuery:
    def test_search_query_becomes_escaped_name_regex(self):
        client = _patched_client(USER, {"results": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_datasets(search_query="mouse (mm10)")
        flt = _search_call(client).kwargs["data"]["filter"]
        assert flt["name"] == {"regex": r"mouse\ \(mm10\)"}
        assert flt["owner._id"] == {"eq": "vmeta-me"}

    def test_paging_and_sort_pass_through(self):
        client = _patched_client({"results": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_datasets(scope="all", take=5, skip=10, sort="name", order="asc")
        data = _search_call(client).kwargs["data"]
        assert (data["take"], data["skip"], data["sort"], data["order"]) == (5, 10, "name", "asc")

    def test_bad_order_falls_back_to_desc(self):
        client = _patched_client({"results": 0, "data": []})
        with patch.object(server, "get_client", return_value=client):
            server.list_datasets(scope="all", order="newest")
        assert _search_call(client).kwargs["data"]["order"] == "desc"


class TestListDatasetsOutput:
    def test_rows_are_trimmed_and_total_reported(self):
        client = _patched_client(USER, {"status": "success", "results": 41, "data": [DATASET_ROW]})
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_datasets())
        assert parsed["total"] == 41
        assert parsed["scope"] == "mine"
        assert parsed["returned"] == 1
        assert parsed["datasets"] == [{
            "id": "ds1",
            "name": "mouse reads",
            "owner": "me@example.com",
            "file_count": 3,
            "canvas_ids": ["canvas-a", "canvas-b"],
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-02T00:00:00Z",
            "tags": ["demo"],
        }]
        # file lists and permission internals never reach the model
        assert "f1" not in json.dumps(parsed)
        assert "perms" not in json.dumps(parsed)

    def test_backend_error_is_returned_as_error(self):
        client = MagicMock()
        client.call.side_effect = Exception("boom")
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_datasets(scope="all"))
        assert parsed == {"error": "boom"}
