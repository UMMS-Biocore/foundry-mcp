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
import re
from contextlib import ExitStack
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
        if url == accessions.ENA_SEARCH:
            rows = []
            for acc in params["includeAccessions"].split(","):
                answer = self.ena.get(acc, [])
                if isinstance(answer, tuple):
                    return answer
                rows.extend(answer)
            # ENA does not answer in the order asked (seen live), so neither does the fake.
            return 200, json.dumps(rows[::-1])
        if url == accessions.EUTILS_ESEARCH:
            accs = re.findall(r"(\w+)\[ACCN\]", params["term"])
            ids = [f"uid-{acc}" for acc in accs if self.gds.get(acc)]
            return 200, json.dumps({"esearchresult": {"count": str(len(ids)), "idlist": ids}})
        if url == accessions.EUTILS_ESUMMARY:
            uids = params["id"].split(",")
            result = {"uids": uids}
            for uid in uids:
                acc = uid[len("uid-"):]
                rec = dict(self.gds[acc])
                rec.setdefault("accession", acc)
                result[uid] = rec
            return 200, json.dumps({"result": result})
        if url.startswith(accessions.BIOSTUDIES_STUDIES):
            acc = url.rsplit("/", 1)[1]
            body = self.biostudies.get(acc)
            if body is None:
                return 404, json.dumps({"message": "not found"})
            return 200, json.dumps(body)
        raise AssertionError(f"unexpected url {url}")


def _patched(http):
    stack = ExitStack()
    for target in ("_http_get", "_http_post"):
        stack.enter_context(patch.object(accessions, target, http))
    stack.enter_context(patch.object(accessions, "_pause", lambda: None))
    return stack


def _resolve(accs, http):
    with _patched(http):
        return accessions.build_plan(accs)


def _urls(http, url):
    return [p for u, p in http.calls if u == url]


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
            ena={"SRX000005": [_run("SRR000005", "SAMN00000005", "GSM5", "cell 5",
                                              experiment="SRX000005")]})
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
            if url == accessions.ENA_SEARCH:
                raise accessions.requests.ConnectionError("down")
            return 200, json.dumps([_run("SRR000003", "SAMN00000003", "GSM3", "c3")])
        plan = _resolve(["SRR000001", "SRP000003", "SRR000002"], http)
        assert plan["run_count"] == 1
        assert [u["accession"] for u in plan["unresolved"]] == ["SRR000001", "SRR000002"]
        assert all("could not reach the archive" in u["reason"] for u in plan["unresolved"])

    def test_runs_reached_twice_are_counted_once(self):
        row = _run("SRR000001", "SAMN00000001", "GSM1", "c1")
        http = FakeHttp(ena={"SRR000001": [row], "SAMN00000001": [row]})
        assert _resolve(["SRR000001", "SAMN00000001"], http)["run_count"] == 1


def _runs(n, start=1):
    return {f"SRR{i:06d}": [_run(f"SRR{i:06d}", f"SAMN{i:08d}", f"GSM{i}", f"cell {i}")]
            for i in range(start, start + n)}


class TestBatching:
    """A long list of runs, experiments or GEO samples costs a few calls, not one per accession."""

    def test_twenty_runs_take_one_ena_call(self):
        http = FakeHttp(ena=_runs(20))
        plan = _resolve(list(_runs(20)), http)
        assert plan["run_count"] == 20 and plan["unresolved"] == []
        searches = _urls(http, accessions.ENA_SEARCH)
        assert len(searches) == 1 and _urls(http, accessions.ENA_FILEREPORT) == []
        assert searches[0]["result"] == "read_run"
        assert searches[0]["fields"] == accessions.ENA_FIELDS

    def test_long_run_lists_are_chunked(self):
        http = FakeHttp(ena=_runs(450))
        plan = _resolve(list(_runs(450)), http)
        assert plan["run_count"] == 450
        sizes = [len(p["includeAccessions"].split(",")) for p in _urls(http, accessions.ENA_SEARCH)]
        assert sizes == [200, 200, 50]

    def test_records_follow_the_order_asked_not_the_order_answered(self):
        http = FakeHttp(ena=_runs(3))
        plan = _resolve(["SRR000002", "SRR000003", "SRR000001"], http)
        assert [r["geo_id"] for r in plan["records"]] == ["SRR000002", "SRR000003", "SRR000001"]

    def test_a_run_missing_from_the_batch_is_unresolved_with_the_reason(self):
        http = FakeHttp(ena=_runs(1))
        plan = _resolve(["SRR000001", "SRR000404"], http)
        assert plan["run_count"] == 1
        assert plan["unresolved"] == [{"accession": "SRR000404",
                                       "reason": "ENA lists no public read runs for SRR000404."}]

    def test_runs_and_experiments_go_in_separate_batches(self):
        # ENA refuses a batch that mixes accession types (400, seen live).
        ena = _runs(2)
        ena["SRX000009"] = [_run("SRR000009", "SAMN00000009", "GSM9", "c9", experiment="SRX000009")]
        http = FakeHttp(ena=ena)
        plan = _resolve(["SRR000001", "SRX000009", "SRR000002"], http)
        assert plan["run_count"] == 3
        batches = sorted(p["includeAccessions"] for p in _urls(http, accessions.ENA_SEARCH))
        assert batches == ["SRR000001,SRR000002", "SRX000009"]

    def test_a_refused_batch_falls_back_to_one_lookup_each(self):
        ena = _runs(2)
        ena["SRR000009"] = (400, json.dumps({"message": "bad accession"}))
        http = FakeHttp(ena=ena)
        plan = _resolve(["SRR000001", "SRR000009", "SRR000002"], http)
        assert plan["run_count"] == 2
        assert plan["unresolved"][0]["accession"] == "SRR000009"
        assert "bad accession" in plan["unresolved"][0]["reason"]

    def test_twenty_gsms_take_one_esearch_one_esummary_and_one_ena_call(self):
        gds = {f"GSM{i}": {"entrytype": "GSM", "extrelations": [
            {"relationtype": "SRA", "targetobject": f"SRX{i:06d}"}]} for i in range(1, 21)}
        ena = {f"SRX{i:06d}": [_run(f"SRR{i:06d}", f"SAMN{i:08d}", f"GSM{i}", f"cell {i}",
                                    experiment=f"SRX{i:06d}")] for i in range(1, 21)}
        http = FakeHttp(gds=gds, ena=ena)
        plan = _resolve(list(gds), http)
        assert plan["run_count"] == 20 and plan["unresolved"] == []
        assert len(_urls(http, accessions.EUTILS_ESEARCH)) == 1
        assert len(_urls(http, accessions.EUTILS_ESUMMARY)) == 1
        assert len(_urls(http, accessions.ENA_SEARCH)) == 1
        assert _urls(http, accessions.ENA_FILEREPORT) == []

    def test_a_repeated_gsm_is_looked_up_once(self):
        http = FakeHttp(
            gds={"GSM1": {"entrytype": "GSM", "extrelations": [
                {"relationtype": "SRA", "targetobject": "SRX000001"}]}},
            ena={"SRX000001": [_run("SRR000001", "SAMN00000001", "GSM1", "c1", experiment="SRX000001")]})
        plan = _resolve(["GSM1", "gsm1"], http)
        assert plan["run_count"] == 1
        assert _urls(http, accessions.EUTILS_ESEARCH)[0]["term"] == "GSM1[ACCN] AND gsm[ETYP]"

    def test_an_unknown_gsm_in_a_batch_is_unresolved_alone(self):
        http = FakeHttp(
            gds={"GSM1": {"entrytype": "GSM", "extrelations": [
                {"relationtype": "SRA", "targetobject": "SRX000001"}]}},
            ena={"SRX000001": [_run("SRR000001", "SAMN00000001", "GSM1", "c1", experiment="SRX000001")]})
        plan = _resolve(["GSM1", "GSM404"], http)
        assert plan["run_count"] == 1
        assert plan["unresolved"] == [{"accession": "GSM404", "reason": "GEO has no record for GSM404."}]


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
    with _patched(http), patch.object(server, "get_client", return_value=client):
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
        assert [s.split(" | ")[3] for s in data["samples"]] == ["pair", "pair"]

    def test_samples_are_one_line_rows(self):
        out, _ = _tool(["SRP000001"], _two_samples(), dry_run=True)
        data = out["data"]
        assert data["sample_columns"] == "name | sample_accession | sample_alias | layout | runs | raw size"
        assert data["samples"] == [
            f"a | SAMN00000001 | GSM1 | pair | SRR000001 | {server._human_size(30)}",
            f"b | SAMN00000002 | GSM2 | pair | SRR000002 | {server._human_size(70)}",
        ]
        assert data["samples_omitted"] == 0

    def test_a_long_plan_lists_the_first_hundred_samples_and_counts_the_rest(self):
        runs = _runs(150)
        out, _ = _tool(list(runs), FakeHttp(ena=runs), dry_run=True)
        data = out["data"]
        assert data["sample_count"] == 150
        assert len(data["samples"]) == 100
        assert data["samples_omitted"] == 50
        assert data["samples"][0].startswith("cell_1 | SAMN00000001 |")
        assert "50" in out["summary"]

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
        assert out["data"]["layout_breakdown"] is None
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


def _big_gse(n, series="GSE1"):
    """A GEO series of n sequenced GSMs. GEO lists a series' samples in no
    particular order, so the fake shuffles them, and the numbers cross a digit
    boundary (GSM95 .. GSM95+n) so a text sort would differ from a numeric one."""
    nums = list(range(95, 95 + n))
    listed = nums[::2][::-1] + nums[1::2]
    gds = {series: {"entrytype": "GSE", "bioproject": "PRJNA1",
                    "extrelations": [{"relationtype": "SRA", "targetobject": "SRP000001"}],
                    "samples": [{"accession": f"GSM{i}", "title": f"cell {i}"} for i in listed]}}
    ena = {}
    for i in nums:
        gds[f"GSM{i}"] = {"entrytype": "GSM", "extrelations": [
            {"relationtype": "SRA", "targetobject": f"SRX{i:06d}"}]}
        ena[f"SRX{i:06d}"] = [_run(f"SRR{i:06d}", f"SAMN{i:08d}", f"GSM{i}", f"cell {i}",
                                   experiment=f"SRX{i:06d}")]
    return gds, ena, [f"SRR{i:06d}" for i in nums]


def _page_runs(out):
    return [row.split(" | ")[4] for row in out["data"]["samples"]]


class TestPaging:
    """max_samples and offset page through a large GEO series."""

    def test_pages_cover_every_sample_once_in_a_stable_order(self):
        gds, ena, expected = _big_gse(350)
        seen, offset, offsets = [], 0, []
        while offset is not None:
            out, client = _tool(["GSE1"], FakeHttp(gds=gds, ena=ena), dry_run=True,
                                max_samples=100, offset=offset)
            data = out["data"]
            assert data["total_samples"] == 350 and data["offset"] == offset
            offsets.append(offset)
            seen.extend(_page_runs(out))
            offset = data["next_offset"]
        assert offsets == [0, 100, 200, 300]
        assert seen == expected
        assert client.call.call_count == 0
        again, _ = _tool(["GSE1"], FakeHttp(gds=gds, ena=ena), dry_run=True,
                         max_samples=100, offset=100)
        assert _page_runs(again) == expected[100:200]

    def test_a_page_looks_up_only_its_own_samples(self):
        gds, ena, _ = _big_gse(350)
        http = FakeHttp(gds=gds, ena=ena)
        _tool(["GSE1"], http, dry_run=True, max_samples=100, offset=200)
        gsm_terms = [p["term"] for p in _urls(http, accessions.EUTILS_ESEARCH) if "gsm[ETYP]" in p["term"]]
        assert len(gsm_terms) == 1 and gsm_terms[0].count("[ACCN]") == 100
        searched = [a for p in _urls(http, accessions.ENA_SEARCH) for a in p["includeAccessions"].split(",")]
        assert len(searched) == 100
        assert _urls(http, accessions.ENA_FILEREPORT) == []

    def test_reply_says_how_to_get_the_next_page(self):
        gds, ena, _ = _big_gse(250)
        out, _ = _tool(["GSE1"], FakeHttp(gds=gds, ena=ena), dry_run=True, max_samples=100)
        assert out["data"]["next_offset"] == 100
        assert "offset=100" in out["summary"] or "offset=100" in " ".join(out["next_steps"])
        last, _ = _tool(["GSE1"], FakeHttp(gds=gds, ena=ena), dry_run=True, max_samples=100, offset=200)
        assert last["data"]["next_offset"] is None
        assert last["data"]["sample_count"] == 50

    def test_offset_past_the_end_is_an_empty_page_on_a_dry_run(self):
        gds, ena, _ = _big_gse(120)
        http = FakeHttp(gds=gds, ena=ena)
        out, _ = _tool(["GSE1"], http, dry_run=True, max_samples=100, offset=500)
        data = out["data"]
        assert data["total_samples"] == 120 and data["sample_count"] == 0
        assert data["samples"] == [] and data["next_offset"] is None
        assert _urls(http, accessions.ENA_SEARCH) == []

    def test_offset_past_the_end_creates_nothing(self):
        gds, ena, _ = _big_gse(120)
        out, client = _tool(["GSE1"], FakeHttp(gds=gds, ena=ena), max_samples=100, offset=500)
        assert "120" in out["tool_error"]["error"]
        assert client.call.call_count == 0

    def test_a_page_without_dry_run_creates_a_dataset_of_that_page(self):
        gds, ena, expected = _big_gse(150)
        out, client = _tool(["GSE1"], FakeHttp(gds=gds, ena=ena), max_samples=100, offset=100)
        posts = [c.kwargs["data"]["file"]["geo_id"] for c in client.call.call_args_list
                 if c.kwargs["endpoint"].endswith("/addFile")]
        assert posts == expected[100:]
        assert out["data"]["vmetaCollectionId"] and out["data"]["next_offset"] is None

    def test_run_accessions_page_in_the_order_given(self):
        runs = _runs(5)
        order = ["SRR000005", "SRR000001", "SRR000004", "SRR000002", "SRR000003"]
        out, _ = _tool(order, FakeHttp(ena=runs), dry_run=True, max_samples=2, offset=2)
        assert _page_runs(out) == ["SRR000004", "SRR000002"]
        assert out["data"]["total_samples"] == 5 and out["data"]["next_offset"] == 4

    def test_gsm_accessions_page_in_the_order_given(self):
        gds, ena, _ = _big_gse(10)
        gsms = [f"GSM{i}" for i in (104, 95, 99, 100)]
        out, _ = _tool(gsms, FakeHttp(gds=gds, ena=ena), dry_run=True, max_samples=3)
        assert _page_runs(out) == ["SRR000104", "SRR000095", "SRR000099"]
        assert out["data"]["next_offset"] == 3

    def test_a_series_expands_in_place_in_a_mixed_list(self):
        gds, ena, expected = _big_gse(3)
        ena.update(_runs(2))
        out, _ = _tool(["SRR000001", "GSE1", "GSM96", "SRR000002"], FakeHttp(gds=gds, ena=ena),
                       dry_run=True, max_samples=10)
        assert _page_runs(out) == ["SRR000001"] + expected + ["SRR000002"]
        assert out["data"]["total_samples"] == 5

    @pytest.mark.parametrize("acc", ["SRP000001", "PRJNA1", "ERP000001", "E-MTAB-1"])
    def test_study_accessions_cannot_be_paged(self, acc):
        http = FakeHttp()
        out, _ = _tool([acc], http, dry_run=True, max_samples=10)
        assert acc in out["tool_error"]["error"]
        assert http.calls == []

    @pytest.mark.parametrize("kwargs", [{"max_samples": 0}, {"max_samples": -5}, {"offset": -1},
                                        {"max_samples": 10, "offset": -1}])
    def test_invalid_window_is_refused(self, kwargs):
        http = FakeHttp()
        out, _ = _tool(["SRR000001"], http, dry_run=True, **kwargs)
        assert "tool_error" in out
        assert http.calls == []

    def test_a_wide_page_still_lists_only_the_first_hundred(self):
        gds, ena, _ = _big_gse(350)
        out, _ = _tool(["GSE1"], FakeHttp(gds=gds, ena=ena), dry_run=True, max_samples=150)
        data = out["data"]
        assert data["sample_count"] == 150 and len(data["samples"]) == 100
        assert data["samples_omitted"] == 50 and data["next_offset"] == 150

    def test_no_window_resolves_a_series_as_before(self):
        gds, ena, _ = _big_gse(3)
        ena["SRP000001"] = [_run("SRR000001", "SAMN00000001", "GSM1", "c1")]
        http = FakeHttp(gds=gds, ena=ena)
        out, _ = _tool(["GSE1"], http, dry_run=True)
        assert [p["accession"] for p in _urls(http, accessions.ENA_FILEREPORT)] == ["SRP000001"]
        assert out["data"]["sample_count"] == 1
        assert out["data"]["offset"] == 0 and out["data"]["next_offset"] is None

    def test_docstring_explains_paging(self):
        doc = server.create_dataset_from_accessions.__doc__
        assert "max_samples" in doc and "offset" in doc and "next_offset" in doc


class TestPagingRefusals:
    """Paging refuses rather than shift total_samples or the offsets between calls."""

    def test_a_series_that_fails_to_expand_refuses_the_page(self):
        both, ena, _ = _big_gse(10)
        both["GSE2"] = dict(both["GSE1"], samples=[{"accession": f"GSM{i}"} for i in range(100, 105)])
        both["GSE1"] = dict(both["GSE1"], samples=[{"accession": f"GSM{i}"} for i in range(95, 100)])
        out, _ = _tool(["GSE1", "GSE2"], FakeHttp(gds=both, ena=ena), dry_run=True, max_samples=3)
        assert out["data"]["total_samples"] == 10
        flaky = {k: v for k, v in both.items() if k != "GSE1"}
        # Without the refusal this page would be GSE2's first three samples, resolved fine.
        out, client = _tool(["GSE1", "GSE2"], FakeHttp(gds=flaky, ena=ena), max_samples=3)
        error = out["tool_error"]["error"]
        assert "GSE1" in error and "GEO has no record for GSE1" in error
        assert client.call.call_count == 0

    def test_a_network_failure_on_a_series_refuses_the_page(self):
        gds, ena, _ = _big_gse(5)
        ena.update(_runs(1))
        fake = FakeHttp(gds=gds, ena=ena)

        def http(url, params=None):
            if url == accessions.EUTILS_ESEARCH and "gse[ETYP]" in params["term"]:
                raise accessions.requests.ConnectionError("down")
            return fake(url, params)
        out, client = _tool(["GSE1", "SRR000001"], http, max_samples=3)
        error = out["tool_error"]["error"]
        assert "GSE1" in error and "could not reach" in error
        assert client.call.call_count == 0

    def test_nothing_to_page_reports_the_unresolved_reasons(self):
        out, client = _tool(["GSE404"], FakeHttp(), dry_run=True, max_samples=10)
        error = out["tool_error"]["error"]
        assert "None of the accessions resolved" in error
        assert "GEO has no record for GSE404" in error
        assert "past the end" not in error

    @pytest.mark.parametrize("record", [
        {"entrytype": "GSE", "bioproject": "", "extrelations": [],
         "samples": [{"accession": "GSM1"}, {"accession": "GSM2"}]},
        {"entrytype": "GSE", "bioproject": "", "extrelations": [], "gdstype": "SuperSeries",
         "samples": [{"accession": "GSM3"}]},
    ], ids=["array_only", "superseries"])
    def test_a_series_without_sequencing_is_refused_like_the_unpaged_call(self, record):
        unpaged, _ = _tool(["GSE7"], FakeHttp(gds={"GSE7": record}), dry_run=True)
        reason = "GSE7 has no SRA sequencing runs linked"
        assert reason in unpaged["tool_error"]["error"]
        http = FakeHttp(gds={"GSE7": record})
        paged, _ = _tool(["GSE7"], http, dry_run=True, max_samples=10)
        assert reason in paged["tool_error"]["error"]
        assert not [p for p in _urls(http, accessions.EUTILS_ESEARCH) if "gsm[ETYP]" in p["term"]]
        mixed, _ = _tool(["GSE7", "SRR000001"], FakeHttp(gds={"GSE7": record}, ena=_runs(1)),
                         dry_run=True, max_samples=10)
        assert reason in mixed["tool_error"]["error"]

    def test_a_dry_run_page_with_no_runs_does_not_offer_to_create(self):
        gds = {f"GSM{i}": {"entrytype": "GSM", "extrelations": []} for i in (1, 2)}
        out, _ = _tool(["GSM1", "GSM2"], FakeHttp(gds=gds), dry_run=True, max_samples=1)
        assert out["data"]["run_count"] == 0
        assert "dry_run=False" not in " ".join(out["next_steps"])
        assert out["data"]["next_offset"] == 1

    @pytest.mark.parametrize("kwargs", [{"max_samples": 1.9}, {"max_samples": "10"},
                                        {"offset": 2.5}, {"offset": "1"}, {"max_samples": True},
                                        {"offset": True}])
    def test_non_integer_window_is_refused(self, kwargs):
        http = FakeHttp()
        out, _ = _tool(["SRR000001"], http, dry_run=True, **kwargs)
        assert "tool_error" in out
        assert http.calls == []

    def test_docstring_covers_the_paging_rules(self):
        doc = server.create_dataset_from_accessions.__doc__
        assert "do not mix" in doc.lower()
        assert "input entries" in doc
        for kind in ("SRS", "ERS", "DRS", "SAMD", "ERX", "DRX"):
            assert kind in doc
