"""Tests for the pipeline-discovery tools (list_featured_pipelines,
recommend_pipeline)."""
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


# A miniature stand-in for the real curated catalog, using real names and
# real summary phrasing pulled from live staging.
CATALOG = [
    _pipe(1405, "RNA-seq Pipeline",
          "RNA-seq pipeline includes Quality Control, rRNA filtering, Genome "
          "Alignment using HISAT2 and STAR, and estimating gene and isoform "
          "expression levels by RSEM, featureCounts and Salmon."),
    _pipe(1275, "Variant Calling Pipeline (GATK4)",
          "This Gatk4 pipeline is intended for calling variants in samples."),
    _pipe(813, "Single Cell-10X Genomics",
          "This pipeline maps 10X Genomics reads to selected genome."),
    _pipe(1420, "ATAC-seq Pipeline",
          "This pipeline maps reads to selected genome using Bowtie2 and "
          "identifies regions of open chromatin."),
    _pipe(1483, "CHIP-seq Pipeline",
          "ChIP-seq Pipeline maps reads with Bowtie2, removes duplicates and "
          "calls peaks with MACS2."),
    _pipe(1467, "DE module",
          "Runs DESeq2 to find differentially expressed genes between groups."),
    _pipe(744, "tRNA-Seq Pipeline", "Quantifies transfer RNA fragments."),
    _pipe(164, "FastQC", "FastQC provides quality control checks on raw "
                         "sequence data."),
]


def _recommend_client(catalog=None, example_run=None):
    """A client whose first call returns the catalog and whose later calls
    (the per-pipeline example-run lookups) return a run page."""
    c = MagicMock()
    page = _page(catalog if catalog is not None else CATALOG)
    runs = {"total": 1 if example_run else 0, "take": 1, "skip": 0,
            "data": [example_run] if example_run else []}

    def _call(**kwargs):
        return page if kwargs.get("endpoint") == "/api/pipeline/v1/" else runs

    c.call.side_effect = _call
    return c


def _run_row(rid=12194, name="Mouse Aligner Tests", pid=1405):
    return {"id": rid, "name": name, "status": "Completed", "pipelineId": pid,
            "dateCreated": "2026-07-15T20:47:14.000Z", "username": "kucukura",
            "projectId": 1841, "pipelineName": "RNA-seq Pipeline"}


class TestRecommendPipeline:
    def _top(self, goal, catalog=None, example_run=None):
        client = _recommend_client(catalog, example_run)
        with patch.object(server, "get_client", return_value=client):
            return json.loads(server.recommend_pipeline(goal)), client

    def test_matches_a_goal_phrased_without_any_pipeline_noun(self):
        parsed, _ = self._top("I have mouse RNA-seq and want differential expression")
        names = [r["name"] for r in parsed["data"]["recommendations"]]
        assert names[0] == "RNA-seq Pipeline"
        assert "DE module" in names

    def test_word_boundaries_keep_trna_from_hijacking_an_rna_seq_goal(self):
        """Naive substring scoring ranks 'tRNA-Seq Pipeline' as a top hit for
        'RNA-seq' because 'rna-seq' is literally inside 'tRNA-Seq'."""
        parsed, _ = self._top("bulk RNA-seq differential expression")
        assert parsed["data"]["recommendations"][0]["name"] == "RNA-seq Pipeline"
        assert "tRNA-Seq Pipeline" not in [
            r["name"] for r in parsed["data"]["recommendations"]]

    def test_understands_domain_phrasing_the_catalog_never_spells_out(self):
        parsed, _ = self._top("I want to look at chromatin accessibility")
        assert parsed["data"]["recommendations"][0]["name"] == "ATAC-seq Pipeline"

    def test_maps_transcription_factor_binding_to_chip_seq(self):
        parsed, _ = self._top("where does my transcription factor bind")
        assert parsed["data"]["recommendations"][0]["name"] == "CHIP-seq Pipeline"

    def test_maps_snp_calling_language_to_variant_calling(self):
        parsed, _ = self._top("find SNPs and somatic mutations in my samples")
        assert parsed["data"]["recommendations"][0]["name"].startswith(
            "Variant Calling")

    def test_every_recommendation_explains_itself(self):
        parsed, _ = self._top("single cell 10x experiment")
        for rec in parsed["data"]["recommendations"]:
            assert rec["reason"]
            assert isinstance(rec["score"], int)
        assert "match" in parsed["data"]["recommendations"][0]["reason"].lower()

    def test_returns_at_most_three(self):
        parsed, _ = self._top("rna seq expression genes quality peaks variants")
        assert len(parsed["data"]["recommendations"]) <= 3

    def test_no_plausible_match_is_admitted_not_guessed(self):
        parsed, _ = self._top("I want to bake a chocolate cake")
        assert parsed["data"]["recommendations"] == []
        assert "list_featured_pipelines" in " ".join(parsed["next_steps"])
        assert "no" in parsed["summary"].lower()

    def test_a_goal_of_only_filler_words_does_not_match_everything(self):
        parsed, _ = self._top("I want to run an analysis on my data please")
        assert parsed["data"]["recommendations"] == []

    def test_attaches_the_most_recent_successful_run_to_clone(self):
        parsed, client = self._top("mouse RNA-seq differential expression",
                                   example_run=_run_row())
        top = parsed["data"]["recommendations"][0]
        assert top["example_run"] == {"id": 12194, "name": "Mouse Aligner Tests",
                                      "dateCreated": "2026-07-15T20:47:14.000Z"}
        run_calls = [c for c in client.call.call_args_list
                     if c.kwargs.get("endpoint") == "/api/v1/run/list"]
        assert run_calls, "expected an example-run lookup"
        params = run_calls[0].kwargs["params"]
        assert params["filter"] == "pipelineId:eq=1405,status:eq=Completed|NextSuc"
        assert params["sort"] == "dateCreated"
        assert params["order"] == "desc"

    def test_says_so_when_a_pipeline_has_no_successful_run_to_clone(self):
        parsed, _ = self._top("mouse RNA-seq differential expression")
        assert parsed["data"]["recommendations"][0]["example_run"] is None

    def test_a_failing_example_lookup_does_not_sink_the_recommendation(self):
        client = MagicMock()
        page = _page(CATALOG)

        def _call(**kwargs):
            if kwargs.get("endpoint") == "/api/pipeline/v1/":
                return page
            raise RuntimeError("run list exploded")

        client.call.side_effect = _call
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.recommend_pipeline("RNA-seq expression"))
        assert parsed["data"]["recommendations"][0]["name"] == "RNA-seq Pipeline"
        assert parsed["data"]["recommendations"][0]["example_run"] is None

    def test_reads_the_whole_catalog_rather_than_server_side_search(self):
        """Server-side search orders by pin position, not relevance — verified
        live, where searchKeyword=atac puts Cell Ranger Count above ATAC-seq.
        Scoring must see the full catalog."""
        client = _recommend_client()
        with patch.object(server, "get_client", return_value=client):
            server.recommend_pipeline("atac")
        params = client.call.call_args_list[0].kwargs["params"]
        assert params["take"] == 100
        assert "searchKeyword" not in params

    def test_next_steps_route_to_plan_run_and_warn_about_compute(self):
        parsed, _ = self._top("RNA-seq differential expression",
                              example_run=_run_row())
        joined = " ".join(parsed["next_steps"])
        assert "plan_run" in joined
        assert "1405" in joined
        assert "HPC" in joined or "compute" in joined

    def test_a_short_filler_token_does_not_prefix_match_a_pipeline_name(self):
        """Live regression: "I ran a CRISPR screen" recommended Cell RANGER,
        because the 3-letter token "ran" prefix-matched "Ranger". Short tokens
        must match whole words; only longer ones may match a prefix."""
        catalog = CATALOG + [_pipe(1349, "Cell Ranger Count Pipeline",
                                   "Processes Chromium single cell data."),
                             _pipe(1470, "Crispr Screen",
                                   "MAGeCK analyses CRISPR knockout screens.")]
        parsed, _ = self._top("I ran a CRISPR knockout screen", catalog)
        names = [r["name"] for r in parsed["data"]["recommendations"]]
        assert names[0] == "Crispr Screen"
        assert "Cell Ranger Count Pipeline" not in names

    def test_longer_tokens_still_match_on_a_prefix(self):
        """"variant" must still hit "variants" — the suffix tolerance is what
        makes plain-language goals work at all."""
        parsed, _ = self._top("somatic variant in my tumour")
        assert parsed["data"]["recommendations"][0]["name"].startswith(
            "Variant Calling")

    def test_plain_read_quality_phrasing_finds_the_qc_pipeline(self):
        """Live regression: "check the quality of my sequencing reads" matched
        nothing, because the hints only knew the exact phrase "quality control"."""
        parsed, _ = self._top(
            "check the quality of my sequencing reads before anything else")
        assert parsed["data"]["recommendations"][0]["name"] == "FastQC"

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.recommend_pipeline("rna-seq")
        assert json.loads(result) == {"error": "boom"}
