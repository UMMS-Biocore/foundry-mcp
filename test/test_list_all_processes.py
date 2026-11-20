import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _client(rows):
    client = MagicMock()
    client.process.list_processes.return_value = rows
    return client


def _call(rows, **kwargs):
    client = _client(rows)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.list_all_processes(**kwargs))


# The list endpoint selects id, name and summary only, and its group join
# repeats a row once per matching group membership.
ROWS = [
    {"id": 1, "name": "FastQC", "summary": "Read quality report"},
    {"id": 1, "name": "FastQC", "summary": "Read quality report"},
    {"id": 2, "name": "STAR_align", "summary": "Splice aware aligner"},
    {"id": 3, "name": "trim_galore", "summary": "Adapter trimming, then FASTQC"},
    {"id": 2, "name": "STAR_align", "summary": "Splice aware aligner"},
    {"id": 4, "name": "featureCounts", "summary": None},
]


class TestListAllProcesses:
    def test_default_is_bounded_to_fifty(self):
        rows = [{"id": i, "name": f"p{i}", "summary": ""} for i in range(1, 121)]
        out = _call(rows)
        data = out["data"]
        assert data["total"] == 120
        assert data["returned"] == 50
        assert data["offset"] == 0
        assert len(data["processes"]) == 50
        assert data["processes"][0]["id"] == 1

    def test_deduplicates_by_id(self):
        out = _call(ROWS)
        ids = [p["id"] for p in out["data"]["processes"]]
        assert ids == [1, 2, 3, 4]
        assert out["data"]["total"] == 4

    def test_compact_row_has_no_invented_owner(self):
        out = _call(ROWS)
        row = out["data"]["processes"][0]
        assert row == {"id": 1, "name": "FastQC", "summary": "Read quality report"}

    def test_search_is_case_insensitive_over_name_and_summary(self):
        out = _call(ROWS, search="fastqc")
        ids = [p["id"] for p in out["data"]["processes"]]
        # Matches the name of 1 and the summary of 3.
        assert ids == [1, 3]
        assert out["data"]["total"] == 2

    def test_search_without_match(self):
        out = _call(ROWS, search="nothing-like-this")
        assert out["data"]["processes"] == []
        assert out["data"]["total"] == 0
        assert "No process matches" in out["summary"]

    def test_offset_pages_and_next_step_names_next_offset(self):
        rows = [{"id": i, "name": f"p{i}", "summary": ""} for i in range(1, 8)]
        out = _call(rows, limit=3, offset=3)
        data = out["data"]
        assert [p["id"] for p in data["processes"]] == [4, 5, 6]
        assert data["returned"] == 3
        assert data["offset"] == 3
        assert any("offset=6" in step for step in out["next_steps"])

    def test_last_page_has_no_next_page_hint(self):
        rows = [{"id": i, "name": f"p{i}", "summary": ""} for i in range(1, 8)]
        out = _call(rows, limit=3, offset=6)
        assert [p["id"] for p in out["data"]["processes"]] == [7]
        assert not any("offset=" in step for step in out.get("next_steps", []))

    def test_limit_is_capped(self):
        rows = [{"id": i, "name": f"p{i}", "summary": ""} for i in range(1, 801)]
        out = _call(rows, limit=10000)
        assert out["data"]["returned"] == 500
        out = _call(rows, limit=0)
        assert out["data"]["returned"] == 1
        out = _call(rows, offset=-5)
        assert out["data"]["offset"] == 0

    def test_long_summary_is_truncated(self):
        rows = [{"id": 1, "name": "p", "summary": "word " * 200}]
        out = _call(rows)
        assert len(out["data"]["processes"][0]["summary"]) <= 300

    def test_revision_and_owner_pass_through_when_present(self):
        rows = [{"id": 9, "name": "p", "summary": "", "rev_id": 2, "owner": "someone"}]
        out = _call(rows)
        assert out["data"]["processes"][0] == {
            "id": 9, "name": "p", "summary": "", "revision": 2, "owner": "someone",
        }

    def test_error_is_reported(self):
        client = MagicMock()
        client.process.list_processes.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.list_all_processes())
        assert out == {"error": "boom"}
