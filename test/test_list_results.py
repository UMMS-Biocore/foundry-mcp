"""Tests for list_results and suggest_apps.

The original friction was that results came back as raw route-paths with no
indication of what any of them were.
"""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _f(process, path, size=1000):
    name = path.rsplit("/", 1)[-1]
    return {"id": path, "processName": process, "file_path": path, "name": name,
            "extension": name.rsplit(".", 1)[-1], "fileSize": size,
            "routePath": f"/report-resources/uuid/pubweb/{path}"}


FILES = [
    _f("DE_module_RSEM", "DESeq2_RSEM/control_vs_exper_des.Rmd", 40267),
    _f("DE_module_RSEM", "DESeq2_RSEM/control_vs_exper_des.html", 1432991),
    _f("DE_module_RSEM", "DESeq2_RSEM/inputs/metadata.tsv", 131),
    _f("DE_module_RSEM", "DESeq2_RSEM/outputs/control_vs_exper_sig_deseq2_results.tsv", 902),
    _f("DE_module_RSEM", "DESeq2_RSEM/outputs/control_vs_exper_all_deseq2_results.tsv", 6110),
    _f("RSEM_module", "rsem_summary/genes_expression_expected_count.tsv", 65181),
    _f("MultiQC", "multiqc/multiqc_report.html", 4807841),
    _f("Overall_Summary", "summary/overall_summary.tsv", 744),
]

APPS = [
    {"id": 30, "name": "GSEA Explorer"},
    {"id": 33, "name": "Cellxgene App"},
    {"id": 45, "name": "IGV"},
    {"id": 99, "name": "r-skinomicsexplorer"},
]


class _FilesFrame(list):
    def to_dict(self, orient="records"):
        return list(self)


def _client(files=None, apps=None):
    c = MagicMock()
    c.reports.fetch_report_data.return_value = {"report": True}
    c.reports.get_all_files.return_value = _FilesFrame(FILES if files is None else files)
    c.apps.list_apps.return_value = APPS if apps is None else apps

    def _call(**kwargs):
        if "app/v1" in kwargs.get("endpoint", ""):
            return APPS if apps is None else apps
        return {}

    c.call.side_effect = _call
    return c


def _run(tool, **kwargs):
    client = _client(**kwargs)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(tool("12194")), client


class TestListResults:
    def test_groups_files_by_what_they_are(self):
        parsed, _ = _run(server.list_results)
        groups = {g["group"] for g in parsed["data"]["groups"]}
        assert "Differential expression" in groups
        assert "Quality control" in groups
        assert "Quantification" in groups

    def test_returns_file_path_verbatim_so_it_can_be_loaded(self):
        """The trap this phase's own grounding fell into: building
        dir + "/" + name yields a path load_file cannot resolve."""
        parsed, _ = _run(server.list_results)
        de = next(g for g in parsed["data"]["groups"]
                  if g["group"] == "Differential expression")
        paths = {f["file_path"] for f in de["files"]}
        assert "DESeq2_RSEM/outputs/control_vs_exper_sig_deseq2_results.tsv" in paths

    def test_hides_intermediate_noise(self):
        parsed, _ = _run(server.list_results)
        shown = {f["file_path"] for g in parsed["data"]["groups"] for f in g["files"]}
        assert not any(p.endswith(".Rmd") for p in shown)
        assert not any("/inputs/" in p for p in shown)

    def test_keeps_the_human_readable_reports(self):
        """The MultiQC and DESeq2 HTML reports are what a scientist opens —
        hide them from bulk loading, not from the listing."""
        parsed, _ = _run(server.list_results)
        shown = {f["file_path"] for g in parsed["data"]["groups"] for f in g["files"]}
        assert "multiqc/multiqc_report.html" in shown

    def test_reports_human_sizes(self):
        parsed, _ = _run(server.list_results)
        qc = next(g for g in parsed["data"]["groups"] if g["group"] == "Quality control")
        multiqc = next(f for f in qc["files"] if f["name"] == "multiqc_report.html")
        assert multiqc["size"] == "4.6 MB"

    def test_counts_what_it_hid_rather_than_dropping_it_silently(self):
        parsed, _ = _run(server.list_results)
        assert parsed["data"]["hidden_intermediate_files"] == 2  # .Rmd + inputs/

    def test_next_steps_point_at_the_summary_and_apps(self):
        parsed, _ = _run(server.list_results)
        joined = " ".join(parsed["next_steps"])
        assert "summarize_results" in joined
        assert "suggest_apps" in joined

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.reports.fetch_report_data.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            assert json.loads(server.list_results("12194")) == {"error": "boom"}


class TestSuggestApps:
    def test_maps_de_results_to_gsea_explorer(self):
        parsed, _ = _run(server.suggest_apps)
        gsea = next(s for s in parsed["data"]["suggestions"]
                    if s["app_name"] == "GSEA Explorer")
        assert gsea["app_id"] == 30
        assert any("deseq2_results" in f for f in gsea["files"])
        assert gsea["reason"]

    def test_maps_h5ad_to_cellxgene(self):
        files = FILES + [_f("scRNA", "scrna/outputs/clustered.h5ad", 500000)]
        parsed, _ = _run(server.suggest_apps, files=files)
        names = {s["app_name"] for s in parsed["data"]["suggestions"]}
        assert "Cellxgene App" in names

    def test_maps_alignments_to_igv(self):
        files = FILES + [_f("STAR", "star/outputs/sample1.bam", 900000),
                         _f("STAR", "star/outputs/sample1.bw", 400000)]
        parsed, _ = _run(server.suggest_apps, files=files)
        igv = next(s for s in parsed["data"]["suggestions"] if s["app_name"] == "IGV")
        assert len(igv["files"]) == 2

    def test_omits_an_app_that_is_not_installed(self):
        """Recommending an app the instance does not have would hand the user a
        dead id. Match by name against the real app list."""
        parsed, _ = _run(server.suggest_apps, apps=[{"id": 99, "name": "qupath"}])
        assert parsed["data"]["suggestions"] == []
        assert "no viewer to suggest" in parsed["summary"].lower()
        assert "list_apps" in " ".join(parsed["next_steps"])

    def test_never_invents_an_app_id(self):
        parsed, _ = _run(server.suggest_apps)
        ids = {s["app_id"] for s in parsed["data"]["suggestions"]}
        assert ids <= {a["id"] for a in APPS}

    def test_hands_off_to_launch_app(self):
        parsed, _ = _run(server.suggest_apps)
        assert "launch_app" in " ".join(parsed["next_steps"])

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.reports.fetch_report_data.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            assert json.loads(server.suggest_apps("12194")) == {"error": "boom"}
