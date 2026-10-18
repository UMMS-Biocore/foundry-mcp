"""Tests for create_dataset_from_accessions and its resolver.

Every outside service is mocked at accessions._http_get. The response shapes
were read from live read-only calls on 2026-10-17:
  * ENA portal filereport (result=read_run, format=json): a JSON array; an
    accession of a supported type with no public runs answers 200 with an
    empty array; an unsupported accession type answers 400 with a message.
    A paired run's fastq_bytes is "r1;r2".
  * NCBI eutils esearch db=gds with term "<acc>[ACCN] AND gse[ETYP]" returns
    the one uid; esummary carries `extrelations` with relationtype SRA and the
    SRP (series) or SRX (sample) as targetobject, and `bioproject`.
  * BioStudies /api/v1/studies/<E-MTAB-n>: `section.links` is a list of lists
    of {url, attributes: [{name: Type, value: ENA}]}; unknown study is 404.

The dataset record shape mirrors the web GEO import, which production uses:
  {name, geo_id: "<one run accession>", file_layout: "single"|"pair"}
posted to /api/v1/vmeta/dataset/{id}/addFile as {canvasId, file}. One run per
record, because the initial run step downloads only the first id of a GEO
record and addFile refuses a repeated name.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from src.foundry_mcp import accessions, server


def _run(run, sample, alias, title, layout="SINGLE", strategy="RNA-Seq", fastq_bytes="1000",
         experiment="SRX1"):
    return {
        "run_accession": run, "sample_accession": sample, "sample_alias": alias,
        "sample_title": title, "experiment_accession": experiment,
        "library_layout": layout, "library_strategy": strategy,
        "instrument_platform": "ILLUMINA", "fastq_bytes": fastq_bytes,
        "study_accession": "PRJNA1",
    }


class FakeHttp:
    """Routes (url, params) to canned (status, body) answers."""

    def __init__(self, ena=None, gds=None, biostudies=None):
        self.ena = ena or {}            # accession -> list of rows | (status, body)
        self.gds = gds or {}            # accession -> esummary record (dict) | None
        self.biostudies = biostudies or {}  # accession -> json | None (404)
        self.calls = []

    def __call__(self, url, params=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        if url == accessions.ENA_FILEREPORT:
            acc = params["accession"]
            answer = self.ena.get(acc, [])
            if isinstance(answer, tuple):
                return answer
            return 200, json.dumps(answer)
        if url == accessions.EUTILS_ESEARCH:
            acc = params["term"].split("[")[0]
            rec = self.gds.get(acc)
            ids = [f"uid-{acc}"] if rec else []
            return 200, json.dumps({"esearchresult": {"count": str(len(ids)), "idlist": ids}})
        if url == accessions.EUTILS_ESUMMARY:
            uid = params["id"]
            acc = uid[len("uid-"):]
            rec = dict(self.gds[acc])
            rec.setdefault("accession", acc)
            return 200, json.dumps({"result": {"uids": [uid], uid: rec}})
        if url.startswith(accessions.BIOSTUDIES_STUDIES):
            acc = url.rsplit("/", 1)[1]
            body = self.biostudies.get(acc)
            if body is None:
                return 404, json.dumps({"message": "not found"})
            return 200, json.dumps(body)
        raise AssertionError(f"unexpected url {url}")


def _resolve(accs, http):
    with patch.object(accessions, "_http_get", http), patch.object(accessions, "_pause", lambda: None):
        return accessions.build_plan(accs)


class TestClassify:
    @pytest.mark.parametrize("acc,kind", [
        ("GSE36552", "geo_series"), ("gsm922275", "geo_sample"),
        ("SRP011546", "ena"), ("ERP017126", "ena"), ("DRP000001", "ena"),
        ("PRJNA153427", "ena"), ("PRJEB1787", "ena"),
        ("SRX130009", "ena"), ("ERX000001", "ena"),
        ("SRR445730", "ena"), ("ERR000001", "ena"), ("DRR000001", "ena"),
        ("SAMN00828842", "ena"), ("SAMEA1234567", "ena"),
        ("E-MTAB-5061", "arrayexpress"),
    ])
    def test_known_types(self, acc, kind):
        assert accessions.classify(acc) == kind

    @pytest.mark.parametrize("acc", ["", "GDS123", "hello", "SRR12", "E-MTAB-"])
    def test_unknown_types(self, acc):
        assert accessions.classify(acc) is None


class TestResolve:
    def test_ena_run_accession_resolves_directly(self):
        http = FakeHttp(ena={"SRR000001": [_run("SRR000001", "SAMN00000001", "GSM1", "cell 1")]})
        plan = _resolve(["SRR000001"], http)
        assert plan["run_count"] == 1
        assert plan["unresolved"] == []
        call = http.calls[0]
        assert call[1]["result"] == "read_run"
        assert "fastq_bytes" in call[1]["fields"] and "library_layout" in call[1]["fields"]

    def test_gse_goes_through_eutils_to_the_srp(self):
        http = FakeHttp(
            gds={"GSE9": {"entrytype": "GSE", "bioproject": "PRJNA9",
                          "extrelations": [{"relationtype": "SRA", "targetobject": "SRP000009"}]}},
            ena={"SRP000009": [_run("SRR000001", "SAMN00000001", "GSM1", "cell 1"),
                          _run("SRR000002", "SAMN00000002", "GSM2", "cell 2")]})
        plan = _resolve(["GSE9"], http)
        assert plan["run_count"] == 2 and plan["sample_count"] == 2
        term = [p["term"] for u, p in http.calls if u == accessions.EUTILS_ESEARCH][0]
        assert term == "GSE9[ACCN] AND gse[ETYP]"
        assert [p["accession"] for u, p in http.calls if u == accessions.ENA_FILEREPORT] == ["SRP000009"]

    def test_gse_falls_back_to_bioproject_when_no_sra_relation(self):
        http = FakeHttp(gds={"GSE9": {"entrytype": "GSE", "bioproject": "PRJNA9", "extrelations": []}},
                        ena={"PRJNA9": [_run("SRR000001", "SAMN00000001", "GSM1", "c1")]})
        assert _resolve(["GSE9"], http)["run_count"] == 1

    def test_gse_without_sequencing_is_unresolved_not_guessed(self):
        http = FakeHttp(gds={"GSE7": {"entrytype": "GSE", "bioproject": "", "extrelations": []}})
        plan = _resolve(["GSE7"], http)
        assert plan["run_count"] == 0
        assert plan["unresolved"][0]["accession"] == "GSE7"
        assert "SRA" in plan["unresolved"][0]["reason"]

    def test_gsm_goes_through_its_srx(self):
        http = FakeHttp(
            gds={"GSM5": {"entrytype": "GSM", "title": "x",
                          "extrelations": [{"relationtype": "SRA", "targetobject": "SRX000005"}]}},
            ena={"SRX000005": [_run("SRR000005", "SAMN00000005", "GSM5", "cell 5")]})
        plan = _resolve(["GSM5"], http)
        assert plan["run_count"] == 1
        term = [p["term"] for u, p in http.calls if u == accessions.EUTILS_ESEARCH][0]
        assert term == "GSM5[ACCN] AND gsm[ETYP]"

    def test_unknown_geo_accession_is_unresolved(self):
        plan = _resolve(["GSE404"], FakeHttp())
        assert plan["unresolved"] == [{"accession": "GSE404", "reason": "GEO has no record for GSE404."}]

    def test_arrayexpress_goes_through_biostudies_to_ena(self):
        http = FakeHttp(
            biostudies={"E-MTAB-1": {"accno": "E-MTAB-1", "section": {"links": [
                [{"url": "E-MTAB-0", "attributes": [{"name": "Type", "value": "Biostudies"}]}],
                [{"url": "ERP000001", "attributes": [{"name": "Type", "value": "ENA"}]}],
            ]}}},
            ena={"ERP000001": [_run("ERR000001", "SAMEA000001", "s1", "sample one")]})
        plan = _resolve(["E-MTAB-1"], http)
        assert plan["run_count"] == 1
        assert [p["accession"] for u, p in http.calls if u == accessions.ENA_FILEREPORT] == ["ERP000001"]

    def test_arrayexpress_without_ena_link_is_unresolved(self):
        http = FakeHttp(biostudies={"E-MTAB-2": {"section": {"links": []}}})
        plan = _resolve(["E-MTAB-2"], http)
        assert plan["unresolved"][0]["accession"] == "E-MTAB-2"
        assert "ENA" in plan["unresolved"][0]["reason"]

    def test_arrayexpress_not_found(self):
        plan = _resolve(["E-MTAB-3"], FakeHttp())
        assert "no study" in plan["unresolved"][0]["reason"]

    def test_empty_ena_answer_is_unresolved(self):
        plan = _resolve(["SRP000404"], FakeHttp(ena={"SRP000404": []}))
        assert plan["unresolved"][0]["accession"] == "SRP000404"

    def test_ena_refusal_is_reported_with_its_message(self):
        http = FakeHttp(ena={"SRR000009": (400, json.dumps({"message": "bad accession"}))})
        plan = _resolve(["SRR000009"], http)
        assert "bad accession" in plan["unresolved"][0]["reason"]

    def test_unrecognized_accession_is_reported_without_any_call(self):
        http = FakeHttp()
        plan = _resolve(["GDS1"], http)
        assert plan["unresolved"][0]["accession"] == "GDS1"
        assert http.calls == []

    def test_network_failure_is_per_accession(self):
        def http(url, params=None):
            if params and params.get("accession") == "SRR000001":
                raise accessions.requests.ConnectionError("down")
            return 200, json.dumps([_run("SRR000002", "SAMN00000002", "GSM2", "c2")])
        plan = _resolve(["SRR000001", "SRR000002"], http)
        assert plan["run_count"] == 1
        assert plan["unresolved"][0]["accession"] == "SRR000001"

    def test_runs_reached_twice_are_counted_once(self):
        row = _run("SRR000001", "SAMN00000001", "GSM1", "c1")
        http = FakeHttp(ena={"SRR000001": [row], "SAMN00000001": [row]})
        assert _resolve(["SRR000001", "SAMN00000001"], http)["run_count"] == 1


class TestGrouping:
    def test_groups_runs_by_sample_and_sums_bytes(self):
        http = FakeHttp(ena={"SRP000001": [
            _run("SRR000001", "SAMN00000001", "GSM1", "4-cell embryo#2 -Cell#2", layout="PAIRED", fastq_bytes="100;200"),
            _run("SRR000002", "SAMN00000001", "GSM1", "4-cell embryo#2 -Cell#2", layout="PAIRED", fastq_bytes="300;400"),
            _run("SRR000003", "SAMN00000002", "GSM2", "hESC passage#0 -Cell#1", fastq_bytes="50"),
        ]})
        plan = _resolve(["SRP000001"], http)
        assert plan["sample_count"] == 2
        assert plan["run_count"] == 3
        assert plan["total_raw_bytes"] == 1050
        first = plan["samples"][0]
        assert first["name"] == "4-cell_embryo_2_-Cell_2"
        assert first["layout"] == "pair"
        assert first["runs"] == ["SRR000001", "SRR000002"]
        assert first["raw_bytes"] == 1000
        assert plan["samples"][1]["layout"] == "single"

    def test_one_record_per_run_with_unique_names(self):
        http = FakeHttp(ena={"SRP000001": [
            _run("SRR000001", "SAMN00000001", "GSM1", "a", layout="PAIRED", fastq_bytes="1;1"),
            _run("SRR000002", "SAMN00000001", "GSM1", "a", layout="PAIRED", fastq_bytes="1;1"),
            _run("SRR000003", "SAMN00000002", "GSM2", "b"),
        ]})
        plan = _resolve(["SRP000001"], http)
        assert plan["records"] == [
            {"name": "a_SRR000001", "geo_id": "SRR000001", "file_layout": "pair"},
            {"name": "a_SRR000002", "geo_id": "SRR000002", "file_layout": "pair"},
            {"name": "b", "geo_id": "SRR000003", "file_layout": "single"},
        ]
        assert any("2 runs" in w for w in plan["warnings"])

    def test_colliding_titles_get_the_sample_alias(self):
        http = FakeHttp(ena={"SRP000001": [_run("SRR000001", "SAMN00000001", "GSM1", "cell"),
                                      _run("SRR000002", "SAMN00000002", "GSM2", "cell")]})
        names = [s["name"] for s in _resolve(["SRP000001"], http)["samples"]]
        assert names == ["cell_GSM1", "cell_GSM2"]

    def test_missing_title_falls_back_to_alias_then_accession(self):
        http = FakeHttp(ena={"SRP000001": [_run("SRR000001", "SAMN00000001", "GSM1", ""),
                                      _run("SRR000002", "SAMN00000002", "", "")]})
        names = [s["name"] for s in _resolve(["SRP000001"], http)["samples"]]
        assert names == ["GSM1", "SAMN00000002"]

    def test_mixed_layout_and_non_rna_are_warnings(self):
        http = FakeHttp(ena={"SRP000001": [
            _run("SRR000001", "SAMN00000001", "GSM1", "a", layout="PAIRED", fastq_bytes="1;1"),
            _run("SRR000002", "SAMN00000001", "GSM1", "a", layout="SINGLE"),
            _run("SRR000003", "SAMN00000002", "GSM2", "b", strategy="ChIP-Seq"),
        ]})
        plan = _resolve(["SRP000001"], http)
        joined = " ".join(plan["warnings"])
        assert "mixes paired and single" in joined
        assert "ChIP-Seq" in joined
        assert plan["samples"][0]["layout"] == "mixed"

    def test_paired_run_with_one_fastq_is_flagged(self):
        http = FakeHttp(ena={"SRR000001": [_run("SRR000001", "SAMN00000001", "GSM1", "a", layout="PAIRED", fastq_bytes="10")]})
        assert any("one fastq" in w for w in _resolve(["SRR000001"], http)["warnings"])

    def test_unknown_layout_is_unresolved(self):
        http = FakeHttp(ena={"SRR000001": [_run("SRR000001", "SAMN00000001", "GSM1", "a", layout="")]})
        plan = _resolve(["SRR000001"], http)
        assert plan["records"] == []
        assert plan["unresolved"][0]["accession"] == "SRR000001"


def _client(dataset_id="6a62b921c5d9e44e73f433ac", fail_add=False):
    c = MagicMock()

    def _call(**kwargs):
        endpoint = kwargs.get("endpoint", "")
        if endpoint.endswith("/vmeta/dataset/create"):
            return {"status": "success", "data": {"_id": dataset_id}}
        if endpoint.endswith("/addFile"):
            if fail_add:
                raise RuntimeError("addFile exploded")
            return {"status": "success"}
        if endpoint.endswith("/vmeta/canvas/search"):
            return {"status": "success", "data": [{"_id": "69743dc841a0a599bb36d3de"}]}
        return {}

    c.call.side_effect = _call
    return c


def _tool(accs, http, client=None, **kwargs):
    client = client or _client()
    with patch.object(accessions, "_http_get", http), patch.object(accessions, "_pause", lambda: None), \
            patch.object(server, "get_client", return_value=client):
        try:
            out = json.loads(server.create_dataset_from_accessions("geo_study", accs, **kwargs))
        except ToolError as e:
            out = {"tool_error": json.loads(str(e))}
    return out, client


def _two_samples():
    return FakeHttp(ena={"SRP000001": [
        _run("SRR000001", "SAMN00000001", "GSM1", "a", layout="PAIRED", fastq_bytes="10;20"),
        _run("SRR000002", "SAMN00000002", "GSM2", "b", layout="PAIRED", fastq_bytes="30;40"),
    ]})


class TestTool:
    def test_dry_run_writes_nothing(self):
        out, client = _tool(["SRP000001"], _two_samples(), dry_run=True)
        assert client.call.call_count == 0
        data = out["data"]
        assert data["dry_run"] is True
        assert data["vmetaCollectionId"] is None
        assert data["sample_count"] == 2 and data["run_count"] == 2
        assert data["total_raw_bytes"] == 100
        assert [s["layout"] for s in data["samples"]] == ["pair", "pair"]

    def test_creates_dataset_and_posts_the_geo_record_shape(self):
        out, client = _tool(["SRP000001"], _two_samples())
        assert out["data"]["vmetaCollectionId"] == "6a62b921c5d9e44e73f433ac"
        posts = [c.kwargs for c in client.call.call_args_list if c.kwargs["endpoint"].endswith("/addFile")]
        assert [p["data"] for p in posts] == [
            {"canvasId": "69743dc841a0a599bb36d3de", "file": {"name": "a", "geo_id": "SRR000001", "file_layout": "pair"}},
            {"canvasId": "69743dc841a0a599bb36d3de", "file": {"name": "b", "geo_id": "SRR000002", "file_layout": "pair"}},
        ]
        create = [c.kwargs for c in client.call.call_args_list if c.kwargs["endpoint"].endswith("/dataset/create")]
        assert create[0]["data"] == {"name": "geo_study"}

    def test_explicit_canvas_skips_the_search(self):
        _, client = _tool(["SRP000001"], _two_samples(), canvas_id="abc")
        assert not any(c.kwargs["endpoint"].endswith("/canvas/search") for c in client.call.call_args_list)

    def test_nothing_resolved_is_an_error_and_writes_nothing(self):
        out, client = _tool(["SRP000404"], FakeHttp())
        assert "tool_error" in out
        assert client.call.call_count == 0

    def test_partial_add_failure_is_an_error_naming_the_dataset(self):
        out, _ = _tool(["SRP000001"], _two_samples(), client=_client(fail_add=True))
        assert "6a62b921c5d9e44e73f433ac" in out["tool_error"]["error"]

    def test_empty_inputs_are_refused(self):
        out, _ = _tool([], FakeHttp())
        assert "tool_error" in out
        out, _ = _tool([" "], FakeHttp())
        assert "tool_error" in out

    def test_single_end_plan_says_to_set_mate_single(self):
        http = FakeHttp(ena={"SRP000001": [_run("SRR000001", "SAMN00000001", "GSM1", "a"),
                                           _run("SRR000002", "SAMN00000002", "GSM2", "b")]})
        out, _ = _tool(["SRP000001"], http, dry_run=True)
        assert out["data"]["layout"] == "single"
        assert out["data"]["mate"] == "single"
        steps = " ".join(out["next_steps"])
        assert "mate" in steps and "'single'" in steps
        assert "single_or_paired_end_reads" in steps

    def test_paired_plan_says_to_set_mate_pair(self):
        out, _ = _tool(["SRP000001"], _two_samples())
        assert out["data"]["layout"] == "paired"
        assert out["data"]["mate"] == "pair"
        assert "'pair'" in " ".join(out["next_steps"])

    def test_mixed_plan_gives_the_breakdown_and_no_mate(self):
        http = FakeHttp(ena={"SRP000001": [
            _run("SRR000001", "SAMN00000001", "GSM1", "a", layout="PAIRED", fastq_bytes="1;1"),
            _run("SRR000002", "SAMN00000002", "GSM2", "b")]})
        out, _ = _tool(["SRP000001"], http, dry_run=True)
        assert out["data"]["layout"] == "mixed"
        assert out["data"]["mate"] is None
        assert out["data"]["layout_breakdown"] == {"paired": ["a"], "single": ["b"]}
        assert "split" in " ".join(out["next_steps"]).lower()

    def test_peak_inputs_estimate_is_six_times_raw(self):
        out, _ = _tool(["SRP000001"], _two_samples(), dry_run=True)
        assert out["data"]["total_raw_bytes"] == 100
        assert out["data"]["peak_inputs_bytes_estimate"] == 600
        assert "peak_inputs_size_estimate" in out["data"]

    def test_docstring_carries_the_storage_guidance(self):
        doc = server.create_dataset_from_accessions.__doc__
        assert "never delete" in doc.lower()
        assert "budget" in doc.lower()
        assert "cleanup_delay_hours" in doc
        assert "peak_inputs_bytes_estimate" in doc
        assert "mate" in doc
