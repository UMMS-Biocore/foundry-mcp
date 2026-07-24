"""Tests for prepare_samples — turning a pile of fastq paths into a dataset.

The dataset row shape is verified against live staging:
  {name, file1, file2, file3, file4, file_layout: "pair"|"single"}
posted to /api/v1/vmeta/dataset/{id}/addFile as {canvasId, file}.
"""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _client(dataset_id="6a62b921c5d9e44e73f433ac", canvases=None, fail_add=False):
    canvases = canvases if canvases is not None else [
        {"_id": "69743dc841a0a599bb36d3de", "name": "default"}]
    c = MagicMock()

    def _call(**kwargs):
        endpoint = kwargs.get("endpoint", "")
        if endpoint.endswith("/vmeta/dataset/create"):
            return {"status": "success", "data": {"_id": dataset_id}}
        if endpoint.endswith("/addFile"):
            if fail_add:
                raise RuntimeError("addFile exploded")
            return {"status": "success", "data": {"fileCount": 1}}
        if endpoint.endswith("/vmeta/canvas/search"):
            return {"status": "success", "data": canvases}
        return {}

    c.call.side_effect = _call
    return c


def _prepare(files, **kwargs):
    client = kwargs.pop("client", None) or _client(**kwargs)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.prepare_samples("my_study", files)), client


def _added(client):
    """The file objects actually posted, in order."""
    return [c.kwargs["data"]["file"] for c in client.call.call_args_list
            if c.kwargs.get("endpoint", "").endswith("/addFile")]


class TestPairDetection:
    def test_pairs_the_R1_R2_convention(self):
        pairs, unpaired = server._pair_fastqs([
            "/d/sampleA_R2_001.fastq.gz", "/d/sampleA_R1_001.fastq.gz"])
        assert unpaired == []
        assert pairs == [{"name": "sampleA", "file1": "/d/sampleA_R1_001.fastq.gz",
                          "file2": "/d/sampleA_R2_001.fastq.gz",
                          "file_layout": "pair"}]

    def test_pairs_the_dot_1_dot_2_convention(self):
        """This is what the live mousetest dataset uses: exper_rep3.1.gz."""
        pairs, _ = server._pair_fastqs(["/d/exper_rep3.1.gz", "/d/exper_rep3.2.gz"])
        assert pairs[0]["name"] == "exper_rep3"
        assert pairs[0]["file1"].endswith(".1.gz")
        assert pairs[0]["file2"].endswith(".2.gz")

    def test_pairs_the_underscore_1_convention(self):
        pairs, _ = server._pair_fastqs(["/d/sampleB_1.fastq", "/d/sampleB_2.fastq"])
        assert pairs[0]["name"] == "sampleB"
        assert pairs[0]["file_layout"] == "pair"

    def test_treats_an_unmated_file_as_single_end(self):
        pairs, unpaired = server._pair_fastqs(["/d/solo.fastq.gz"])
        assert pairs[0] == {"name": "solo", "file1": "/d/solo.fastq.gz",
                            "file2": "", "file_layout": "single"}
        assert unpaired == []

    def test_reports_a_mate_that_is_missing_rather_than_hiding_it(self):
        """An R1 with no R2 is far more likely a forgotten file than a
        deliberate single-end run — say so instead of quietly halving it."""
        pairs, unpaired = server._pair_fastqs([
            "/d/good_R1.fastq.gz", "/d/good_R2.fastq.gz", "/d/lonely_R1.fastq.gz"])
        assert [p["name"] for p in pairs] == ["good", "lonely"]
        assert unpaired == ["lonely"]

    def test_allows_mixed_layouts_in_one_dataset(self):
        pairs, _ = server._pair_fastqs([
            "/d/p_R1.fq.gz", "/d/p_R2.fq.gz", "/d/s.fq.gz"])
        assert {p["file_layout"] for p in pairs} == {"pair", "single"}

    def test_sorts_samples_by_name_for_a_stable_dataset(self):
        pairs, _ = server._pair_fastqs(["/d/c.fq", "/d/a.fq", "/d/b.fq"])
        assert [p["name"] for p in pairs] == ["a", "b", "c"]

    def test_strips_every_common_fastq_extension(self):
        for path, expected in (("/d/x.fastq.gz", "x"), ("/d/x.fq.gz", "x"),
                               ("/d/x.fastq", "x"), ("/d/x.fq", "x"),
                               ("/d/x.gz", "x")):
            pairs, _ = server._pair_fastqs([path])
            assert pairs[0]["name"] == expected, path

    def test_does_not_pair_across_different_directories(self):
        """Same basename in two directories is two samples, not a pair."""
        pairs, _ = server._pair_fastqs(["/a/s_R1.fq.gz", "/b/s_R1.fq.gz"])
        assert len(pairs) == 2


class TestPrepareSamples:
    def test_creates_the_dataset_then_adds_a_row_per_sample(self):
        parsed, client = _prepare(["/d/a_R1.fq.gz", "/d/a_R2.fq.gz",
                                   "/d/b_R1.fq.gz", "/d/b_R2.fq.gz"])
        creates = [c for c in client.call.call_args_list
                   if c.kwargs.get("endpoint", "").endswith("/dataset/create")]
        assert len(creates) == 1
        assert creates[0].kwargs["data"] == {"name": "my_study"}
        assert [f["name"] for f in _added(client)] == ["a", "b"]

    def test_posts_rows_in_the_verified_live_shape(self):
        _, client = _prepare(["/d/a_R1.fq.gz", "/d/a_R2.fq.gz"])
        assert _added(client)[0] == {
            "name": "a", "file1": "/d/a_R1.fq.gz", "file2": "/d/a_R2.fq.gz",
            "file3": "", "file4": "", "file_layout": "pair"}

    def test_returns_the_id_named_as_the_run_input_expects_it(self):
        """The whole point: this id goes straight into a run's sample input."""
        parsed, _ = _prepare(["/d/a.fq.gz"])
        assert parsed["data"]["vmetaCollectionId"] == "6a62b921c5d9e44e73f433ac"
        assert parsed["data"]["sample_count"] == 1
        assert "update_run" in " ".join(parsed["next_steps"])

    def test_resolves_a_canvas_when_none_is_given(self):
        _, client = _prepare(["/d/a.fq.gz"])
        assert _added(client)
        canvas_ids = {c.kwargs["data"]["canvasId"]
                      for c in client.call.call_args_list
                      if c.kwargs.get("endpoint", "").endswith("/addFile")}
        assert canvas_ids == {"69743dc841a0a599bb36d3de"}

    def test_reports_unpaired_mates_in_the_summary(self):
        parsed, _ = _prepare(["/d/a_R1.fq.gz", "/d/a_R2.fq.gz",
                              "/d/lonely_R1.fq.gz"])
        assert parsed["data"]["unpaired"] == ["lonely"]
        assert "lonely" in " ".join(parsed["next_steps"])

    def test_refuses_to_silently_drop_a_file_that_collapses_to_the_same_name(self):
        """a_R1 + a_R2 + a bare a.fastq.gz all reduce to the sample "a". Without
        this the odd one out vanishes and the run analyses less data than the
        scientist believes it does."""
        parsed, _ = _prepare(["/d/a_R1.fq.gz", "/d/a_R2.fq.gz",
                              "/d/a.fastq.gz"])
        assert "error" in parsed
        assert "/d/a.fastq.gz" in parsed["error"]

    def test_every_given_file_lands_in_a_row_when_names_are_clean(self):
        parsed, client = _prepare(["/d/a_R1.fq.gz", "/d/a_R2.fq.gz",
                                   "/d/b.fq.gz"])
        used = set()
        for f in _added(client):
            used.update(x for x in (f["file1"], f["file2"]) if x)
        assert used == {"/d/a_R1.fq.gz", "/d/a_R2.fq.gz", "/d/b.fq.gz"}

    def test_rejects_an_empty_file_list(self):
        parsed, _ = _prepare([])
        assert "error" in parsed

    def test_rejects_a_blank_dataset_name(self):
        client = _client()
        with patch.object(server, "get_client", return_value=client):
            parsed = json.loads(server.prepare_samples("  ", ["/d/a.fq.gz"]))
        assert "error" in parsed

    def test_says_so_when_no_canvas_is_available(self):
        parsed, _ = _prepare(["/d/a.fq.gz"], canvases=[])
        assert "error" in parsed
        assert "canvas" in parsed["error"].lower()

    def test_reports_a_partial_failure_rather_than_claiming_success(self):
        """If rows fail to attach, the dataset exists but is not usable — the
        scientist must not be told it is ready."""
        parsed, _ = _prepare(["/d/a.fq.gz"], fail_add=True)
        assert "error" in parsed or parsed["data"]["sample_count"] == 0

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.prepare_samples("s", ["/d/a.fq.gz"])
        assert json.loads(result) == {"error": "boom"}
