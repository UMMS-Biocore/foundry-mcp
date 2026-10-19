"""Tests for get_pipeline, create_pipeline_config and create_pipeline.

The SDK's real Pipelines and PipelineBuilder run against a mocked `call`, so
these hold the tools to the exact edge strings the editor saves. The process
and pipeline fixtures are pipelines 1777 and 1545 as the server returned them
on 2026-10-19.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from viafoundry.pipelines import DEFAULT_PIPE_CONFIG, Pipelines, parse_edge

from src.foundry_mcp import server


def _param(pid, name, file_type, qualifier, sname=None):
    return {"parameter_id": pid, "name": name, "file_type": file_type, "qualifier": qualifier, "sname": sname or name}


PROCESSES = {
    3017: {"name": "gpu_smoke_check",
           "inputs": [_param(252, "text", "text", "val", "label")],
           "outputs": [_param(201, "outputFileTxt", "txt", "file", '"gpu_check.txt"')]},
    2357: {"name": "Xenium_Download_Dataset",
           "inputs": [_param(639, "samplesheet", "samplesheet", "set")],
           "outputs": [_param(131, "outputDir", "directory", "set")]},
    2356: {"name": "Xenium_Data_Analysis",
           "inputs": [_param(637, "inputDir", "directory", "set"),
                      _param(547, "inputFileTsv", "tsv", "file", "metadatasheet"),
                      _param(638, "inputFileTsv", "tsv", "set", "cellstats")],
           "outputs": [_param(385, "rdsFile", "RDS", "file"), _param(164, "outputHTML", "html", "file")]},
}

DETAILS_1777 = {
    "updateAllowed": True,
    "pipeline": {"main_pipeline_1777": {
        "id": 1777, "name": "GPU smoke check (bootcamp)", "summary": "s", "description": "",
        "pipeline_group_id": 667, "pipeline_type": "ViaFoundry", "version": "1.0.0",
        "rev_comment": "Initial Version", "perms": 3, "pin": "false", "publicly_searchable": "false",
        "script_pipe_config": DEFAULT_PIPE_CONFIG,
        "nodes": json.dumps({"g-1": [354.6, 389, "3017", "gpu_smoke_check", {}],
                             "g-2": [82.3, 453.7, "inPro", "inputParam", {}],
                             "g-3": [768.0, 453.7, "outPro", "outputParam", {}]}),
        "edges": json.dumps({"edges": ["o-inPro-1-252-2_i-3017-0-252-1", "i-outPro-1-201-3_o-3017-0-201-1"]}),
        "mainG": json.dumps({"mainG": [61.9, 72.4, 0.7]}),
    }},
}

EDGES_1545 = [
    "o-2357-0-131-5_i-2356-0-637-0", "o-inPro-1-639-7_i-2357-0-639-5", "o-inPro-1-638-9_i-2356-1-547-0",
    "o-inPro-1-638-9_i-2356-2-638-0", "i-outPro-1-164-4_o-2356-1-164-0", "i-outPro-1-385-3_o-2356-0-385-0",
]
NAMES_1545 = {"g-0": "Xenium_Data_Analysis", "g-3": "Analysis_Results_RDS", "g-4": "HTML_Report",
              "g-5": "Xenium_Download_Dataset", "g-7": "samplesheet", "g-9": "metadatasheet"}

GRAPH_1777 = dict(
    processes=[{"key": "check", "process_id": 3017}],
    inputs=[{"key": "label", "name": "inputParam"}],
    outputs=[{"key": "report", "name": "outputParam"}],
    connections=[{"from": "label", "to": "check"}, {"from": "check", "to": "report"}],
)

GRAPH_1545 = dict(
    processes=[{"key": "download", "process_id": 2357}, {"key": "analysis", "process_id": 2356}],
    inputs=[{"key": "samplesheet"}, {"key": "metadatasheet"}],
    outputs=[{"key": "rds", "name": "Analysis_Results_RDS", "publish": "rdata"},
             {"key": "html", "name": "HTML_Report", "publish": "html"}],
    connections=[
        {"from": "download", "to": "analysis", "input": "inputDir"},
        {"from": "samplesheet", "to": "download"},
        {"from": "metadatasheet", "to": "analysis", "input": 2},
        {"from": "metadatasheet", "to": "analysis", "input": "metadatasheet"},
        {"from": "analysis", "to": "html", "output": "outputHTML"},
        {"from": "analysis", "to": "rds", "output": 385},
    ],
)


def _client(processes=PROCESSES, details=None, created=None):
    """A client whose `call` answers the process, details and save endpoints."""
    client = MagicMock()

    def call(*args, **kwargs):
        method = kwargs.get("method", args[0] if args else None)
        endpoint = kwargs.get("endpoint", args[1] if len(args) > 1 else None)
        if method == "GET" and endpoint.startswith("/api/v1/process/"):
            pid = int(endpoint.rsplit("/", 1)[1])
            if pid not in processes:
                raise RuntimeError("404 Not Found")
            return processes[pid]
        if method == "GET" and endpoint.endswith("/details"):
            return details
        if method == "POST" and endpoint == "/api/pipeline/v1":
            return created
        raise AssertionError(f"unexpected request {method} {endpoint}")

    client.call.side_effect = call
    client.pipelines = Pipelines(client)
    return client


def _run(tool, client, **kwargs):
    with patch.object(server, "get_client", return_value=client):
        return json.loads(tool(**kwargs))


def _posts(client):
    return [c for c in client.call.call_args_list if "POST" in c.args or c.kwargs.get("method") == "POST"]


def _shape(names, edges):
    """Edges with node keys replaced by node names, so graphs numbered differently compare equal."""
    shaped = set()
    for edge in edges:
        src, dst = parse_edge(edge)
        shaped.add((names[src["node"]], src["param"], src["slot"], src["parameterId"],
                    names[dst["node"]], dst["param"], dst["slot"], dst["parameterId"]))
    return shaped


def _tools():
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


@pytest.mark.parametrize("name,required", [
    ("get_pipeline", ["pipeline_id"]),
    ("create_pipeline_config", ["name", "pipeline_group_id", "processes", "connections"]),
    ("create_pipeline", ["name", "pipeline_group_id", "processes", "connections", "confirm_create"]),
])
def test_tool_is_registered_with_its_arguments(name, required):
    tool = _tools()[name]
    assert "connections" in tool.description or name == "get_pipeline"
    assert tool.description.strip()
    assert set(required) <= set(tool.inputSchema.get("required", []))


def test_create_pipeline_warns():
    assert "WARNING" in _tools()["create_pipeline"].description


class TestConfig:
    def test_rebuilds_1777_exactly_and_writes_nothing(self):
        client = _client()
        out = _run(server.create_pipeline_config, client, name="GPU smoke check", pipeline_group_id=667, **GRAPH_1777)
        assert out["edges"] == ["o-inPro-1-252-2_i-3017-0-252-1", "i-outPro-1-201-3_o-3017-0-201-1"]
        assert {k: (n["param"], n["nodeName"]) for k, n in out["nodes"].items()} == {
            "g-1": ("3017", "gpu_smoke_check"), "g-2": ("inPro", "inputParam"), "g-3": ("outPro", "outputParam")}
        assert out["connections"] == [
            {"from": "label", "output": None, "output_slot": 1, "to": "check", "input": "text", "input_slot": 0},
            {"from": "check", "output": "outputFileTxt", "output_slot": 0, "to": "report", "input": None, "input_slot": 1},
        ]
        assert out["unconnected_inputs"] == []
        assert _posts(client) == []

    def test_rebuilds_the_1545_graph(self):
        out = _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **GRAPH_1545)
        names = {k: n["nodeName"] for k, n in out["nodes"].items()}
        assert _shape(names, out["edges"]) == _shape(NAMES_1545, EDGES_1545)
        rds = next(n for n in out["nodes"].values() if n["nodeName"] == "Analysis_Results_RDS")
        assert rds["nodeSettings"] == {"pubWeb": "rdata"}

    def test_reports_inputs_nothing_feeds(self):
        graph = dict(GRAPH_1545, connections=GRAPH_1545["connections"][:2])
        out = _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **graph)
        assert out["unconnected_inputs"] == [
            {"process": "analysis", "input": "inputFileTsv", "slot": 1},
            {"process": "analysis", "input": "inputFileTsv", "slot": 2},
        ]

    def test_type_mismatch_is_refused_by_the_callers_key(self):
        graph = dict(GRAPH_1545, connections=[{"from": "analysis", "to": "download"}])
        with pytest.raises(ToolError) as refused:
            _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **graph)
        detail = json.loads(str(refused.value))["detail"]
        assert "'analysis'" in detail and "g-" not in detail

    @pytest.mark.parametrize("graph,match", [
        (dict(GRAPH_1777, connections=[{"from": "label", "to": "nowhere"}]), "not the key"),
        (dict(GRAPH_1777, inputs=[{"key": "check"}]), "used twice"),
        (dict(GRAPH_1777, processes=[{"process_id": 3017}]), "needs a"),
        (dict(GRAPH_1777, processes=[], inputs=[], outputs=[], connections=[]), "at least one process"),
    ])
    def test_malformed_graph_is_refused(self, graph, match):
        with pytest.raises(ToolError, match=match):
            _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **graph)

    def test_a_pipeline_id_given_as_a_process_id_is_refused(self):
        graph = dict(GRAPH_1777, processes=[{"key": "check", "process_id": 1545}])
        with pytest.raises(ToolError, match="separate id spaces"):
            _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **graph)

    def test_an_sdk_without_pipelines_is_refused(self):
        client = _client()
        client.pipelines = None
        with pytest.raises(ToolError, match="1.3.0"):
            _run(server.create_pipeline_config, client, name="x", pipeline_group_id=1, **GRAPH_1777)


class TestCreate:
    @pytest.mark.parametrize("confirm", [False, None, "true", 1])
    def test_without_consent_is_refused_before_any_request(self, confirm):
        client = _client()
        with pytest.raises(ToolError, match="confirm_create must be true"):
            _run(server.create_pipeline, client, name="x", pipeline_group_id=1, confirm_create=confirm, **GRAPH_1777)
        client.call.assert_not_called()

    def test_creates_a_new_private_pipeline_from_the_rebuilt_graph(self):
        client = _client(created={"id": 1779, "name": "GPU smoke check"})
        out = _run(server.create_pipeline, client, name="GPU smoke check", pipeline_group_id=667,
                   confirm_create=True, summary="s", **GRAPH_1777)
        assert out["pipeline_id"] == 1779
        (post,) = _posts(client)
        body = post.kwargs["data"]
        assert "id" not in body
        assert body["permission"] == 3 and body["pipelineGroupId"] == 667 and body["summary"] == "s"
        assert body["edges"] == ["o-inPro-1-252-2_i-3017-0-252-1", "i-outPro-1-201-3_o-3017-0-201-1"]

    def test_a_refused_graph_writes_nothing(self):
        client = _client()
        graph = dict(GRAPH_1545, connections=[{"from": "analysis", "to": "download"}])
        with pytest.raises(ToolError, match="refused"):
            _run(server.create_pipeline, client, name="x", pipeline_group_id=1, confirm_create=True, **graph)
        assert _posts(client) == []


class TestGet:
    def test_reads_1777_and_names_its_connections(self):
        out = _run(server.get_pipeline, _client(details=DETAILS_1777), pipeline_id=1777)
        assert out["name"] == "GPU smoke check (bootcamp)" and out["updateAllowed"] is True
        assert out["edges"] == ["o-inPro-1-252-2_i-3017-0-252-1", "i-outPro-1-201-3_o-3017-0-201-1"]
        assert out["connections"] == [
            {"from": "g-2 inputParam", "output": None, "output_slot": 1,
             "to": "g-1 gpu_smoke_check", "input": "text", "input_slot": 0},
            {"from": "g-1 gpu_smoke_check", "output": "outputFileTxt", "output_slot": 0,
             "to": "g-3 outputParam", "input": None, "input_slot": 1},
        ]

    def test_an_unreadable_process_still_returns_the_graph(self):
        out = _run(server.get_pipeline, _client(processes={}, details=DETAILS_1777), pipeline_id=1777)
        assert out["edges"] and "connections_error" in out and "connections" not in out
