"""Tests for diff_run_defaults: a compact read back of what a run changed.

An agent that saved a run's inputs and per process forms had to dump every
entry (61 in one case) to confirm its edits. diff_run_defaults lists only the
values that differ from the pipeline defaults, and the ones left empty where
the pipeline has a default.
"""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _inp(name, value, type_="input", **extra):
    row = {"id": 1, "name": name, "type": type_, "value": value,
           "collectionId": None, "vmetaCollectionId": ""}
    row.update(extra)
    return row


PARAMS = {"pipelineType": "via", "inputs": {
    "pipe": {
        "schema": [{"varName": "params.mate", "type": "dropdown", "defaultVal": "pair"},
                   {"varName": "params.genome_build", "type": "input", "defaultVal": "mm10"},
                   {"varName": "params.run_STAR", "type": "dropdown", "defaultVal": "yes"},
                   {"varName": "params.min_len", "type": "input", "defaultVal": "9"},
                   {"varName": "pipeline_note", "type": "input", "defaultVal": "keep"}],
        "inputNodes": [{"varName": "params.reads", "qualifier": "set", "file": True}],
    },
    "processes": {
        "13_46": {"schema": [{"varName": "params_STAR", "type": "input",
                              "defaultVal": "--runThreadN 4"},
                             {"varName": "remove_previous_reads", "type": "checkbox",
                              "defaultVal": "false"},
                             {"varName": "aligner_Parameters", "type": "input",
                              "defaultVal": ["-k 1", "-k 2"]},
                             {"varName": "threads", "type": "input", "defaultVal": 8}]},
        "4": {"schema": [{"varName": "test_name", "type": "input", "defaultVal": "t1"},
                         {"varName": "no_default", "type": "input"}]},
    },
}}

INPUTS = [
    _inp("reads", "set1", "vmetaCollection", vmetaCollectionId="69743e8d41a0a599bb36f6a8"),
    _inp("mate", "single"),          # changed
    _inp("genome_build", "mm10"),    # same as default
    _inp("run_STAR", ""),            # empty where a default exists
    _inp("min_len", 9),              # same, given as a number
    _inp("extra_input", "x"),        # not defined by the pipeline
]

OPTIONS = {
    "pipe": {"pipeline_note": "keep"},
    "13_46": {
        "params_STAR": "--runThreadN 16",       # changed
        "remove_previous_reads": False,         # same as "false"
        "aligner_Parameters_ind0": "-k 1",      # same as default[0]
        "aligner_Parameters_ind1": "-k 3",      # changed against default[1]
        "threads": "8",                         # same, number vs string
    },
    "4": {"test_name": " ", "no_default": "given"},  # empty with default; set with none
}


def _details(inputs=INPUTS, options=OPTIONS, **extra):
    d = {"name": "Diff run", "inputs": inputs, "processOptions": options,
         "mainPipeline": {"id": 1405, "name": "RNA-seq", "version": "2.7.4"}}
    d.update(extra)
    return d


def _client(details, params=PARAMS):
    c = MagicMock()

    def _call(**kwargs):
        endpoint = kwargs.get("endpoint", "")
        assert kwargs.get("method") == "GET", "diff_run_defaults must only read"
        if endpoint.endswith("/details"):
            return details
        if endpoint.endswith("/parameter-list"):
            if isinstance(params, Exception):
                raise params
            return params
        raise AssertionError(f"unexpected call {endpoint}")

    c.call.side_effect = _call
    return c


def _diff(details=None, params=PARAMS):
    client = _client(details or _details(), params)
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.diff_run_defaults("12500")), client


def _keys(entries):
    return [e["key"] for e in entries]


class TestDiffRunDefaults:
    def test_lists_only_changed_values_with_their_defaults(self):
        parsed, _ = _diff()
        changed = {e["key"]: e for e in parsed["data"]["changed"]}
        assert set(changed) == {
            "inputs.reads",
            "inputs.mate",
            "processOptions.13_46.params_STAR",
            "processOptions.13_46.aligner_Parameters_ind1",
            "processOptions.4.no_default",
        }
        assert changed["inputs.mate"] == {"key": "inputs.mate", "value": "single", "default": "pair"}
        assert changed["processOptions.13_46.aligner_Parameters_ind1"]["default"] == "-k 2"
        assert changed["processOptions.4.no_default"]["default"] is None
        assert changed["inputs.reads"]["value"] == "69743e8d41a0a599bb36f6a8"

    def test_lists_empty_values_where_a_default_exists(self):
        parsed, _ = _diff()
        empty = parsed["data"]["empty_with_default"]
        assert _keys(empty) == ["inputs.run_STAR", "processOptions.4.test_name"]
        assert empty[0] == {"key": "inputs.run_STAR", "default": "yes"}

    def test_reports_saved_keys_the_pipeline_does_not_define(self):
        parsed, _ = _diff()
        assert parsed["data"]["not_in_pipeline"] == ["inputs.extra_input"]

    def test_counts_and_summary(self):
        parsed, _ = _diff()
        data = parsed["data"]
        assert data["run_id"] == "12500"
        assert data["compared"] == 13
        assert data["unchanged"] == 13 - 5 - 2
        assert "5" in parsed["summary"] and "2" in parsed["summary"]

    def test_only_reads(self):
        _, client = _diff()
        assert all(c.kwargs["method"] == "GET" for c in client.call.call_args_list)

    def test_a_run_matching_its_defaults_has_nothing_to_list(self):
        details = _details(inputs=[_inp("mate", "pair")], options={"4": {"test_name": "t1"}})
        parsed, _ = _diff(details)
        data = parsed["data"]
        assert data["changed"] == [] and data["empty_with_default"] == []
        assert data["not_in_pipeline"] == []
        assert "no value differs" in parsed["summary"].lower()

    def test_long_values_are_shortened(self):
        long_value = "x" * 500
        details = _details(inputs=[_inp("mate", long_value)], options={})
        parsed, _ = _diff(details)
        value = parsed["data"]["changed"][0]["value"]
        assert len(value) < 200 and value.startswith("xxx")

    def test_long_lists_are_shortened(self):
        column = [f"row{i}" for i in range(40)]
        details = _details(inputs=[], options={"13_46": {"aligner_Parameters": column}})
        parsed, _ = _diff(details)
        entry = parsed["data"]["changed"][0]
        assert entry["key"] == "processOptions.13_46.aligner_Parameters"
        assert len(json.dumps(entry)) < 400

    def test_external_pipeline_sections(self):
        params = {"pipelineType": "external", "inputs": {
            "input_output_options": {"schema": [
                {"varName": "params.input_output_options.outdir", "defaultVal": "results"},
                {"varName": "params.input_output_options.email", "defaultVal": None}]},
            "alignment_options": {"schema": [
                {"varName": "params.alignment_options.aligner", "defaultVal": "star_salmon"}]},
        }}
        details = _details(inputs={"input_output_options": {"outdir": "out", "email": None},
                                   "alignment_options": {"aligner": ""}}, options=None)
        parsed, _ = _diff(details, params)
        data = parsed["data"]
        assert _keys(data["changed"]) == ["inputs.input_output_options.outdir"]
        assert _keys(data["empty_with_default"]) == ["inputs.alignment_options.aligner"]

    def test_unreadable_parameter_list_is_an_error(self):
        parsed, _ = _diff(params=RuntimeError("boom"))
        assert "error" in parsed and "boom" in parsed["error"]

    def test_unreadable_process_options_are_said_plainly(self):
        details = _details(options=None, pipelineReadable=False)
        parsed, _ = _diff(details)
        assert "process_options_error" in parsed["data"]
        assert all(not k.startswith("processOptions.") for k in _keys(parsed["data"]["changed"]))


def _params_with(schema):
    return {"inputs": {"pipe": {"schema": []},
                       "processes": {"p1": {"schema": schema}}}}


class TestFormFieldDefaultsMatchTheRunPage:
    """The Run page reads a field's default from defVal before defaultVal,
    and keeps the first field of a name when a form lists it twice."""

    def test_defval_wins_over_defaultval(self):
        forms = server._form_field_defaults(_params_with([
            {"varName": "threads", "defVal": "4", "defaultVal": "8"},
        ]))
        assert forms == {"p1": {"threads": "4"}}

    def test_defaultval_is_used_when_defval_is_missing_or_empty(self):
        forms = server._form_field_defaults(_params_with([
            {"varName": "threads", "defaultVal": "8"},
            {"varName": "mode", "defVal": "", "defaultVal": "fast"},
        ]))
        assert forms == {"p1": {"threads": "8", "mode": "fast"}}

    def test_first_field_of_a_name_wins(self):
        forms = server._form_field_defaults(_params_with([
            {"varName": "threads", "defVal": "4"},
            {"varName": "threads", "defVal": "16"},
        ]))
        assert forms == {"p1": {"threads": "4"}}
