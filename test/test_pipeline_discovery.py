"""Tests for the pipeline-discovery tools (list_featured_pipelines)."""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _client(call_return):
    c = MagicMock()
    c.call.return_value = call_return
    return c


def _page(data, total=None):
    return {"total": total if total is not None else len(data), "skip": 0,
            "take": len(data), "data": data}


def _pipe(pid, name, summary="", version="1.0.0", tags=None):
    """A row shaped like a real GET /api/pipeline/v1/ entry (verified against
    live staging 2026-07-23) — note `pin` is the STRING "true", and every row
    carries the window-function `totalCount` leak."""
    return {
        "id": pid, "name": name, "summary": summary,
        "createdAt": "2025-07-06T21:24:40.000Z",
        "modifiedAt": "2026-01-15T17:37:02.000Z",
        "version": version, "pipelineType": "ViaFoundry",
        "pin": "true", "pinOrder": 1, "aiEntity": 0,
        "totalCount": 36, "tags": tags if tags is not None else [],
    }


class TestListFeaturedPipelines:
    def test_asks_the_backend_for_the_released_curated_view(self):
        """type=1 (Released) is `pin='true' AND perms=Public` server-side, already
        ordered pinned-first / pinOrder / newest. That IS the featured catalog."""
        client = _client(_page([_pipe(1405, "RNA-seq Pipeline")]))
        with patch.object(server, "get_client", return_value=client):
            server.list_featured_pipelines()
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/pipeline/v1/",
            params={"type": "1", "take": 20, "skip": 0},
        )

    def test_preserves_backend_curation_order(self):
        rows = [_pipe(1405, "RNA-seq Pipeline"), _pipe(1275, "Variant Calling"),
                _pipe(813, "Single Cell-10X")]
        client = _client(_page(rows))
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_featured_pipelines())
        assert [p["id"] for p in parsed["data"]["pipelines"]] == [1405, 1275, 813]

    def test_entries_are_compact_and_drop_internal_noise(self):
        client = _client(_page([_pipe(1405, "RNA-seq Pipeline", "Aligns reads.")]))
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_featured_pipelines())
        entry = parsed["data"]["pipelines"][0]
        assert entry == {"id": 1405, "name": "RNA-seq Pipeline",
                         "summary": "Aligns reads.", "version": "1.0.0", "tags": []}

    def test_strips_html_and_entities_from_summaries(self):
        """The list endpoint does NOT decode summaries the way getPipeline does,
        so real rows arrive with &amp;-style entities and stray tags."""
        raw = "Cut &amp; Tag <b>pipeline</b>\n\nmaps  reads &gt; peaks"
        client = _client(_page([_pipe(1, "Cut and Tag", raw)]))
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_featured_pipelines())
        assert parsed["data"]["pipelines"][0]["summary"] == (
            "Cut & Tag pipeline maps reads > peaks")

    def test_truncates_a_long_summary_on_a_word_boundary(self):
        client = _client(_page([_pipe(1, "Big", "word " * 200)]))
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_featured_pipelines())
        summary = parsed["data"]["pipelines"][0]["summary"]
        assert len(summary) <= 300
        assert summary.endswith("…")
        assert "wor…" not in summary  # cut between words, not mid-word

    def test_reports_tag_names_only(self):
        tags = [{"id": "u-1", "name": "rna", "color": "#fff", "isActive": True}]
        client = _client(_page([_pipe(1, "RNA-seq", tags=tags)]))
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_featured_pipelines())
        assert parsed["data"]["pipelines"][0]["tags"] == ["rna"]

    def test_passes_a_search_keyword_through(self):
        client = _client(_page([]))
        with patch.object(server, "get_client", return_value=client):
            server.list_featured_pipelines(search="atac")
        assert client.call.call_args.kwargs["params"] == {
            "type": "1", "take": 20, "skip": 0, "searchKeyword": "atac"}

    def test_clamps_limit_to_the_backends_accepted_range(self):
        """The route's Joi schema is take: min(1).max(100) — an out-of-range take
        is a 400, so clamp rather than letting discovery hard-fail."""
        client = _client(_page([]))
        with patch.object(server, "get_client", return_value=client):
            server.list_featured_pipelines(limit=500)
        assert client.call.call_args.kwargs["params"]["take"] == 100
        with patch.object(server, "get_client", return_value=client):
            server.list_featured_pipelines(limit=0)
        assert client.call.call_args.kwargs["params"]["take"] == 1

    def test_envelope_summary_reports_counts_and_next_steps_guide_onward(self):
        client = _client(_page([_pipe(1405, "RNA-seq Pipeline")], total=36))
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_featured_pipelines())
        assert list(parsed) == ["summary", "next_steps", "data"]
        assert "36" in parsed["summary"]
        assert parsed["data"]["total"] == 36
        joined = " ".join(parsed["next_steps"])
        assert "recommend_pipeline" in joined
        assert "plan_run" in joined

    def test_empty_search_says_so_instead_of_returning_a_bare_list(self):
        client = _client(_page([], total=0))
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_featured_pipelines(search="zzz"))
        assert parsed["data"]["pipelines"] == []
        assert "zzz" in parsed["summary"]
        assert any("without a search" in s for s in parsed["next_steps"])

    def test_tolerates_a_bare_list_response(self):
        client = _client([_pipe(1405, "RNA-seq Pipeline")])
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.list_featured_pipelines())
        assert parsed["data"]["pipelines"][0]["id"] == 1405
        assert parsed["data"]["total"] == 1

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.list_featured_pipelines()
        assert json.loads(result) == {"error": "boom"}
