"""Tests for summarize_results — the headline findings from a finished run.

Fixtures mirror live run 12194 on staging. The DE columns and the
`<dir>/outputs/` layout were read from the real report, not invented; the top
significant gene there really is Fgf21 at log2FC +2.14, padj 6.7e-12.
"""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _f(process, path, name=None, ext=None, size=1000):
    return {"id": path, "processName": process, "file_path": path,
            "name": name or path.rsplit("/", 1)[-1],
            "extension": ext or path.rsplit(".", 1)[-1],
            "fileSize": size, "routePath": f"/report-resources/uuid/pubweb/{path}"}


FILES = [
    _f("DE_module_RSEM", "DESeq2_RSEM/control_vs_exper_des.Rmd", ext="Rmd", size=40267),
    _f("DE_module_RSEM", "DESeq2_RSEM/control_vs_exper_des.html", ext="html", size=1432991),
    _f("DE_module_RSEM", "DESeq2_RSEM/inputs/metadata.tsv", size=131),
    _f("DE_module_RSEM", "DESeq2_RSEM/outputs/control_vs_exper_all_deseq2_results.tsv", size=6110),
    _f("DE_module_RSEM", "DESeq2_RSEM/outputs/control_vs_exper_sig_deseq2_results.tsv", size=902),
    _f("DE_module_Kallisto", "DESeq2_Kallisto/outputs/control_vs_exper_sig_deseq2_results.tsv", size=700),
    _f("MultiQC", "multiqc/multiqc_report.html", ext="html", size=4807841),
    _f("Overall_Summary", "summary/overall_summary.tsv", size=744),
]

SIG_RSEM = [
    {"gene": "Fgf21", "baseMean": 335.9, "log2FoldChange": 2.1379, "padj": 6.696e-12},
    {"gene": "AK208554", "baseMean": 42.6, "log2FoldChange": 2.4233, "padj": 1.46e-08},
    {"gene": "Adcy9", "baseMean": 198.8, "log2FoldChange": -1.8283, "padj": 1.12e-08},
    {"gene": "Zfp174", "baseMean": 33.0, "log2FoldChange": -1.3226, "padj": 0.00128},
    {"gene": "Mid1", "baseMean": 12.0, "log2FoldChange": 1.05, "padj": 0.04},
]
SIG_KALLISTO = [
    {"gene": "Fgf21", "baseMean": 300.0, "log2FoldChange": 2.05, "padj": 1e-11},
]
QC = [
    {"Sample": "exper_rep1", "Total Reads": 18068, "Unique Reads Aligned (STAR)": 16905},
    {"Sample": "exper_rep3", "Total Reads": 9601, "Unique Reads Aligned (STAR)": 8532},
    {"Sample": "control_rep1", "Total Reads": 15000, "Unique Reads Aligned (STAR)": 14000},
]


def _client(files=None, tables=None):
    files = FILES if files is None else files
    default_tables = {
        "DESeq2_RSEM/outputs/control_vs_exper_sig_deseq2_results.tsv": SIG_RSEM,
        "DESeq2_Kallisto/outputs/control_vs_exper_sig_deseq2_results.tsv": SIG_KALLISTO,
        "summary/overall_summary.tsv": QC,
    }
    tables = default_tables if tables is None else tables
    c = MagicMock()
    c.reports.fetch_report_data.return_value = {"report": True}

    class _DF:
        def __init__(self, rows):
            self._rows = rows
            self.columns = list(rows[0].keys()) if rows else []
            self.shape = (len(rows), len(self.columns))

        def to_dict(self, orient="records"):
            return self._rows

    def _load(_report, file_path, sep="\t"):
        if file_path not in tables:
            raise RuntimeError(f"File '{file_path}' not found in the files of this report.")
        return _DF(tables[file_path])

    c.reports.load_file.side_effect = _load

    import pandas  # noqa: F401 - only for the shape the SDK returns
    c.reports.get_all_files.return_value = _FilesFrame(files)
    return c


class _FilesFrame(list):
    """Minimal stand-in for the SDK's files DataFrame: iterable of dict rows."""

    def to_dict(self, orient="records"):
        return list(self)


def _summarize(**kwargs):
    client = _client(**kwargs)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.summarize_results("12194")), client


class TestSummarizeResults:
    def test_reports_the_number_of_significant_genes_per_comparison(self):
        parsed, _ = _summarize()
        de = parsed["data"]["differential_expression"]
        rsem = next(d for d in de if d["quantifier"] == "RSEM")
        assert rsem["comparison"] == "control_vs_exper"
        assert rsem["significant_genes"] == 5

    def test_reports_top_up_and_down_regulated_genes(self):
        parsed, _ = _summarize()
        rsem = next(d for d in parsed["data"]["differential_expression"]
                    if d["quantifier"] == "RSEM")
        assert rsem["top_up"][0]["gene"] == "AK208554"      # +2.42, the largest
        assert rsem["top_down"][0]["gene"] == "Adcy9"       # -1.83, the largest drop
        assert rsem["top_up"][0]["padj"] == 1.46e-08

    def test_covers_every_quantifier_that_produced_results(self):
        parsed, _ = _summarize()
        quantifiers = {d["quantifier"] for d in parsed["data"]["differential_expression"]}
        assert quantifiers == {"RSEM", "Kallisto"}

    def test_says_where_the_quantifiers_disagree(self):
        """5 parallel DESeq2 dirs reporting near-identical answers is its own
        noise problem; a scientist needs the spread, not five lists."""
        parsed, _ = _summarize()
        agreement = parsed["data"]["agreement"]
        assert agreement["counts_by_quantifier"] == {"RSEM": 5, "Kallisto": 1}
        assert "Fgf21" in agreement["genes_in_all_quantifiers"]

    def test_reports_the_qc_verdict(self):
        parsed, _ = _summarize()
        qc = parsed["data"]["qc"]
        assert qc["samples"] == 3
        assert qc["total_reads"] == 18068 + 9601 + 15000
        # (16905 + 8532 + 14000) / 42669 = 0.9241...
        assert 92.0 <= qc["alignment_rate_pct"] <= 93.0

    def test_flags_a_sample_with_an_unusually_low_read_count(self):
        qc = QC + [{"Sample": "runt", "Total Reads": 12, "Unique Reads Aligned (STAR)": 5}]
        parsed, _ = _summarize(tables={
            "DESeq2_RSEM/outputs/control_vs_exper_sig_deseq2_results.tsv": SIG_RSEM,
            "summary/overall_summary.tsv": qc})
        assert "runt" in " ".join(parsed["data"]["qc"]["warnings"])

    def test_zero_significant_genes_is_a_result_not_an_error(self):
        """"Nothing was differentially expressed" is a real finding."""
        parsed, _ = _summarize(tables={
            "DESeq2_RSEM/outputs/control_vs_exper_sig_deseq2_results.tsv": [],
            "summary/overall_summary.tsv": QC})
        rsem = next(d for d in parsed["data"]["differential_expression"]
                    if d["quantifier"] == "RSEM")
        assert rsem["significant_genes"] == 0
        assert "error" not in parsed
        assert "no genes" in parsed["summary"].lower() or "0" in parsed["summary"]

    def test_a_run_with_no_de_output_says_so_plainly(self):
        files = [_f("MultiQC", "multiqc/multiqc_report.html", ext="html")]
        parsed, _ = _summarize(files=files, tables={})
        assert "error" not in parsed
        assert parsed["data"]["differential_expression"] == []
        assert "no differential" in parsed["summary"].lower()

    def test_never_downloads_the_big_html_or_the_rmd(self):
        """The DESeq2 html is 1.4 MB and the MultiQC one 4.8 MB — pulling them
        into a chat context would be both slow and useless."""
        _, client = _summarize()
        loaded = [c.args[1] for c in client.reports.load_file.call_args_list]
        assert not any(p.endswith((".html", ".Rmd")) for p in loaded)

    def test_ignores_the_inputs_directory(self):
        """<dir>/inputs/metadata.tsv is what went IN, not a result."""
        _, client = _summarize()
        loaded = [c.args[1] for c in client.reports.load_file.call_args_list]
        assert not any("/inputs/" in p for p in loaded)

    def test_points_at_the_report_and_the_next_tools(self):
        parsed, _ = _summarize()
        joined = " ".join(parsed["next_steps"])
        assert "suggest_apps" in joined or "list_results" in joined

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.reports.fetch_report_data.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.summarize_results("12194")
        assert json.loads(result) == {"error": "boom"}
