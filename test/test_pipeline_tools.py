"""Tests for get_pipeline, list_pipeline_revisions, create_pipeline_config,
create_pipeline and update_pipeline.

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


def _client(processes=PROCESSES, details=None, created=None, revisions=None, modules=None):
    """A client whose `call` answers the process, details, revisions and save endpoints.

    `modules` maps a pipeline id to its details, for pipelines used as modules.
    """
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
            pipeline_id = int(endpoint.split("/")[-2])
            return (modules or {}).get(pipeline_id, details)
        if method == "GET" and endpoint.endswith("/revisions"):
            if isinstance(revisions, Exception):
                raise revisions
            return revisions
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
    ("list_pipeline_revisions", ["pipeline_id"]),
    ("update_pipeline", ["pipeline_id", "processes", "connections", "confirm_update"]),
    ("get_pipeline_ports", ["pipeline_id"]),
])
def test_tool_is_registered_with_its_arguments(name, required):
    tool = _tools()[name]
    assert "connections" in tool.description or name in ("get_pipeline", "list_pipeline_revisions", "get_pipeline_ports")
    assert tool.description.strip()
    assert set(required) <= set(tool.inputSchema.get("required", []))


@pytest.mark.parametrize("name", ["create_pipeline", "update_pipeline"])
def test_writing_tools_warn(name):
    assert "WARNING" in _tools()[name].description


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
        (dict(GRAPH_1777, processes=[], connections=[]), "at least one process"),
        (dict(GRAPH_1777, processes=[{"key": "check", "process_id": 3017.0}]), "positive whole number"),
        (dict(GRAPH_1777, processes=[{"key": "check", "process_id": "abc"}]), "positive whole number"),
        (dict(GRAPH_1777, processes=[{"key": "check", "process_id": -5}]), "positive whole number"),
        (dict(GRAPH_1777, processes=[{"key": "check", "process_id": True}]), "positive whole number"),
    ])
    def test_malformed_graph_is_refused(self, graph, match):
        with pytest.raises(ToolError, match=match):
            _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **graph)

    def test_a_process_id_as_digits_is_accepted(self):
        graph = dict(GRAPH_1777, processes=[{"key": "check", "process_id": "3017"}])
        out = _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **graph)
        assert out["edges"] == ["o-inPro-1-252-2_i-3017-0-252-1", "i-outPro-1-201-3_o-3017-0-201-1"]

    def test_a_callers_key_that_looks_like_a_node_key_is_quoted_as_given(self):
        graph = dict(GRAPH_1777, processes=[{"key": "g-2", "process_id": 3017}],
                     connections=[{"from": "g-1", "to": "g-2"}])
        with pytest.raises(ToolError) as refused:
            _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **graph)
        assert json.loads(str(refused.value))["detail"].startswith("A connection names 'g-1',")

    def test_a_pipeline_id_given_as_a_process_id_is_refused(self):
        graph = dict(GRAPH_1777, processes=[{"key": "check", "process_id": 1545}])
        with pytest.raises(ToolError, match="separate id spaces"):
            _run(server.create_pipeline_config, _client(), name="x", pipeline_group_id=1, **graph)

    def test_an_sdk_without_pipelines_is_refused(self):
        client = _client()
        client.pipelines = None
        with pytest.raises(ToolError, match="1.4.0"):
            _run(server.create_pipeline_config, client, name="x", pipeline_group_id=1, **GRAPH_1777)


class TestCreate:
    @pytest.mark.parametrize("confirm", [False, None, "true", 1])
    def test_without_consent_is_refused_before_any_request(self, confirm):
        client = _client()
        with pytest.raises(ToolError, match="confirm_create must be true"):
            _run(server.create_pipeline, client, name="x", pipeline_group_id=1, confirm_create=confirm, **GRAPH_1777)
        client.call.assert_not_called()

    @pytest.mark.parametrize("confirm", ["true", "yes", "1", 1])
    def test_a_client_cannot_consent_with_a_string_or_number(self, confirm):
        """Through the MCP layer, where lax validation would turn these into True."""
        client = _client(created={"id": 9})
        with patch.object(server, "get_client", return_value=client), pytest.raises(Exception):
            asyncio.run(server.mcp.call_tool("create_pipeline", dict(
                name="x", pipeline_group_id=1, confirm_create=confirm, **GRAPH_1777)))
        assert _posts(client) == []

    def test_a_client_consents_with_true(self):
        client = _client(created={"id": 9})
        with patch.object(server, "get_client", return_value=client):
            asyncio.run(server.mcp.call_tool("create_pipeline", dict(
                name="x", pipeline_group_id=1, confirm_create=True, **GRAPH_1777)))
        assert len(_posts(client)) == 1

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

    @pytest.mark.parametrize("details", [{}, None, {"pipeline": {}}])
    def test_an_id_with_no_pipeline_is_refused(self, details):
        with pytest.raises(ToolError, match="separate id spaces"):
            _run(server.get_pipeline, _client(details=details), pipeline_id=3017)

    def test_a_node_without_a_name_is_still_described(self):
        details = json.loads(json.dumps(DETAILS_1777))
        saved = details["pipeline"]["main_pipeline_1777"]
        saved["nodes"] = json.dumps({"g-1": {"xCoordinate": 0, "yCoordinate": 0, "param": "3017", "nodeSettings": {}},
                                     "g-2": [0, 0, "inPro", "inputParam", {}], "g-3": [0, 0, "outPro", "outputParam", {}]})
        out = _run(server.get_pipeline, _client(details=details), pipeline_id=1777)
        assert out["connections"][0]["to"] == "g-1 None"

    def test_an_unreadable_process_still_returns_the_graph(self):
        out = _run(server.get_pipeline, _client(processes={}, details=DETAILS_1777), pipeline_id=1777)
        assert out["edges"] and "connections_error" in out and "connections" not in out


# Pipeline 1800 uses pipeline 1777 as a module on g-1, written the way the
# editor and backend module-ports.ts number module ports: pi/po, the inner
# process id, the port's order in the module, the inner parameter id, g-N.
DETAILS_1800 = {
    "updateAllowed": True,
    "pipeline": {"main_pipeline_1800": dict(
        DETAILS_1777["pipeline"]["main_pipeline_1777"], id=1800, name="Uses a module",
        nodes=json.dumps({"g-1": [300, 400, "p1777", "GPU smoke module", {}],
                          "g-2": [80, 450, "inPro", "label", {}],
                          "g-3": [700, 450, "outPro", "report", {}],
                          "g-4": [700, 600, "outPro", "unused", {}]}),
        edges=json.dumps({"edges": ["o-inPro-1-252-2_pi-3017-0-252-1", "i-outPro-1-201-3_po-3017-0-201-1"]}),
    )},
}

# A module whose input is fed by nothing: 1777's graph saved without its inPro edge.
DETAILS_1800_UNFED = {
    "updateAllowed": True,
    "pipeline": {"main_pipeline_1800": dict(
        DETAILS_1800["pipeline"]["main_pipeline_1800"],
        edges=json.dumps({"edges": ["po-3017-0-201-1_i-outPro-1-201-3"]}),
    )},
}


class TestGetModules:
    def test_names_a_module_s_ports_by_the_module_s_input_and_output_nodes(self):
        client = _client(details=DETAILS_1800, modules={1777: DETAILS_1777})
        out = _run(server.get_pipeline, client, pipeline_id=1800)
        assert "connections_error" not in out
        assert out["connections"] == [
            {"from": "g-2 label", "output": None, "output_slot": 1,
             "to": "g-1 GPU smoke module", "input": "inputParam", "input_slot": 0},
            {"from": "g-1 GPU smoke module", "output": "outputParam", "output_slot": 0,
             "to": "g-3 report", "input": None, "input_slot": 1},
        ]
        assert out["unconnected_inputs"] == []
        assert out["modules"] == [{"node": "g-1", "pipeline_id": 1777}]

    def test_an_end_written_module_side_first_still_reads_in_dataflow_order(self):
        client = _client(details=DETAILS_1800_UNFED, modules={1777: DETAILS_1777})
        out = _run(server.get_pipeline, client, pipeline_id=1800)
        assert out["connections"] == [
            {"from": "g-1 GPU smoke module", "output": "outputParam", "output_slot": 0,
             "to": "g-3 report", "input": None, "input_slot": 1},
        ]
        assert out["unconnected_inputs"] == [{"process": "g-1 GPU smoke module", "input": "inputParam", "slot": 0}]

    def test_an_unreadable_module_still_returns_the_graph(self):
        client = _client(details=DETAILS_1800, modules={1777: {}})
        out = _run(server.get_pipeline, client, pipeline_id=1800)
        assert out["edges"] and "connections_error" in out and "connections" not in out


    def test_returns_its_graph_as_the_arguments_a_save_takes(self):
        client = _client(details=DETAILS_1800, modules={1777: DETAILS_1777})
        out = _run(server.get_pipeline, client, pipeline_id=1800)
        assert out["graph"] == {
            "processes": [],
            "modules": [{"key": "g-1", "pipeline_id": 1777, "name": "GPU smoke module", "x": 300, "y": 400}],
            "inputs": [{"key": "g-2", "name": "label", "x": 80, "y": 450}],
            "outputs": [{"key": "g-3", "name": "report", "x": 700, "y": 450},
                        {"key": "g-4", "name": "unused", "x": 700, "y": 600}],
            "connections": [{"from": "g-2", "to": "g-1", "input": 0}, {"from": "g-1", "output": 0, "to": "g-3"}],
        }


def _pipeline_details(pipeline_id, name, nodes, edges, **fields):
    """Details as GET /api/pipeline/v1/{id}/details returns them, on 1777's saved fields."""
    saved = dict(DETAILS_1777["pipeline"]["main_pipeline_1777"], id=pipeline_id, name=name,
                 nodes=json.dumps(nodes), edges=json.dumps({"edges": edges}), **fields)
    return {"updateAllowed": True, "pipeline": {f"main_pipeline_{pipeline_id}": saved}}


PARAMS_1545 = {"g-0": "2356", "g-3": "outPro", "g-4": "outPro", "g-5": "2357", "g-7": "inPro", "g-9": "inPro"}
DETAILS_1545 = _pipeline_details(
    1545, "Xenium", {n: [0, 0, PARAMS_1545[n], name, {}] for n, name in NAMES_1545.items()}, EDGES_1545)

# 1800's graph as tool arguments: 1777 as a module between a pipeline input and output.
GRAPH_1800 = dict(
    processes=[],
    modules=[{"key": "smoke", "pipeline_id": 1777, "name": "GPU smoke module"}],
    inputs=[{"key": "label"}],
    outputs=[{"key": "report"}],
    connections=[{"from": "label", "to": "smoke", "input": "inputParam"},
                 {"from": "smoke", "to": "report", "output": 0}],
)


class TestPorts:
    def test_lists_a_pipeline_s_ports_as_a_module(self):
        client = _client(details=DETAILS_1545, processes=PROCESSES)
        out = _run(server.get_pipeline_ports, client, pipeline_id=1545)
        assert out["pipeline_id"] == 1545 and out["name"] == "Xenium"
        assert out["inputs"] == [
            {"slot": 0, "name": "samplesheet", "process_id": 2357, "parameter_id": 639,
             "file_type": "samplesheet", "qualifier": "set"},
            {"slot": 1, "name": "metadatasheet", "process_id": 2356, "parameter_id": 638,
             "file_type": "tsv", "qualifier": "set"},
        ]
        assert [(p["slot"], p["name"], p["file_type"]) for p in out["outputs"]] == \
            [(0, "HTML_Report", "html"), (1, "Analysis_Results_RDS", "RDS")]
        assert _posts(client) == []

    @pytest.mark.parametrize("details", [{}, None, {"pipeline": {}}])
    def test_an_id_with_no_pipeline_is_refused(self, details):
        with pytest.raises(ToolError, match="could not be read"):
            _run(server.get_pipeline_ports, _client(details=details), pipeline_id=3017)


class TestConfigModules:
    def test_rebuilds_1800_exactly(self):
        client = _client(modules={1777: DETAILS_1777})
        out = _run(server.create_pipeline_config, client, name="x", pipeline_group_id=1, **GRAPH_1800)
        assert out["edges"] == ["o-inPro-1-252-2_pi-3017-0-252-1", "i-outPro-1-201-3_po-3017-0-201-1"]
        assert out["nodes"]["g-1"]["param"] == "p1777" and out["nodes"]["g-1"]["nodeName"] == "GPU smoke module"
        assert out["connections"] == [
            {"from": "label", "output": None, "output_slot": 1, "to": "smoke", "input": "inputParam", "input_slot": 0},
            {"from": "smoke", "output": "outputParam", "output_slot": 0, "to": "report", "input": None, "input_slot": 1},
        ]
        assert _posts(client) == []

    def test_a_module_feeds_a_process_and_reports_its_unfed_inputs(self):
        graph = dict(
            processes=[{"key": "analysis", "process_id": 2356}],
            modules=[{"key": "xenium", "pipeline_id": 1545}],
            connections=[{"from": "xenium", "to": "analysis", "output": "HTML_Report", "input": "inputDir"}],
        )
        with pytest.raises(ToolError, match="'xenium' output HTML_Report"):
            _run(server.create_pipeline_config, _client(modules={1545: DETAILS_1545}), name="x", pipeline_group_id=1, **graph)
        graph["connections"] = []
        out = _run(server.create_pipeline_config, _client(modules={1545: DETAILS_1545}), name="x", pipeline_group_id=1, **graph)
        assert {"process": "xenium", "input": "metadatasheet", "slot": 1} in out["unconnected_inputs"]

    @pytest.mark.parametrize("modules,match", [
        ([{"key": "smoke", "pipeline_id": "abc"}], "positive whole number"),
        ([{"pipeline_id": 1777}], "needs a"),
        ([{"key": "smoke", "pipeline_id": 4242}], "could not be read"),
    ])
    def test_a_malformed_module_is_refused(self, modules, match):
        graph = dict(GRAPH_1800, modules=modules, connections=[])
        with pytest.raises(ToolError, match=match):
            _run(server.create_pipeline_config, _client(modules={1777: DETAILS_1777, 4242: {}}),
                 name="x", pipeline_group_id=1, **graph)

    def test_an_unknown_module_port_is_refused_by_the_callers_key(self):
        graph = dict(GRAPH_1800, connections=[{"from": "label", "to": "smoke", "input": "nope"}])
        with pytest.raises(ToolError) as refused:
            _run(server.create_pipeline_config, _client(modules={1777: DETAILS_1777}), name="x", pipeline_group_id=1, **graph)
        assert "'smoke' has no input matching 'nope'" in json.loads(str(refused.value))["detail"]

    def test_create_saves_the_module_edges(self):
        client = _client(modules={1777: DETAILS_1777}, created={"id": 1801})
        _run(server.create_pipeline, client, name="x", pipeline_group_id=1, confirm_create=True, **GRAPH_1800)
        (post,) = _posts(client)
        assert post.kwargs["data"]["edges"] == ["o-inPro-1-252-2_pi-3017-0-252-1", "i-outPro-1-201-3_po-3017-0-201-1"]


class TestUpdateModules:
    def test_a_pipeline_holding_a_module_round_trips_through_get_pipeline(self):
        client = _client(details=DETAILS_1800, modules={1777: DETAILS_1777}, created={"id": 1800})
        graph = _run(server.get_pipeline, client, pipeline_id=1800)["graph"]
        out = _run(server.update_pipeline, client, pipeline_id=1800, confirm_update=True, **graph)
        (post,) = _posts(client)
        saved = DETAILS_1800["pipeline"]["main_pipeline_1800"]
        assert post.kwargs["data"]["edges"] == json.loads(saved["edges"])["edges"] == out["edges"]
        assert {k: (n["param"], n["nodeName"], n["xCoordinate"]) for k, n in post.kwargs["data"]["nodes"].items()} == \
            {k: (n[2], n[3], n[0]) for k, n in json.loads(saved["nodes"]).items()}

    @pytest.mark.parametrize("module_id,modules,revisions", [
        (1777, {}, None),
        (1790, {}, {"total": 2, "data": [{"id": 1790}, {"id": 1777}]}),
        (1800, {1800: DETAILS_1800}, None),
    ])
    def test_a_module_that_is_or_holds_this_pipeline_or_a_revision_is_refused(self, module_id, modules, revisions):
        client = _client(details=DETAILS_1777, modules=modules, revisions=revisions, created={"id": 1777})
        graph = dict(GRAPH_1800, modules=[{"key": "smoke", "pipeline_id": module_id}], connections=[])
        with pytest.raises(ToolError, match="cannot hold itself"):
            _run(server.update_pipeline, client, pipeline_id=1777, confirm_update=True, **graph)
        assert _posts(client) == []

    def test_unreadable_revisions_refuse_a_save_with_a_module(self):
        client = _client(details=DETAILS_1777, modules={1800: DETAILS_1800}, revisions=RuntimeError("500"))
        with pytest.raises(ToolError, match="revisions"):
            _run(server.update_pipeline, client, pipeline_id=1777, confirm_update=True,
                 **dict(GRAPH_1800, modules=[{"key": "smoke", "pipeline_id": 1800}], connections=[]))
        assert _posts(client) == []


# Pipeline 1545's revision list as the server returned it on 2026-10-19.
REVISIONS_1545 = {"total": 2, "data": [
    {"id": 1776, "version": "1.0.4", "revId": 1, "revComment": "Takes the sheet once", "revUUID": "u2",
     "createdAt": "2026-10-10T00:00:00.000Z"},
    {"id": 1545, "version": "1.0.3", "revId": 0, "revComment": "Initial Version", "revUUID": "u1",
     "createdAt": "2026-09-01T00:00:00.000Z"},
]}


class TestRevisions:
    def test_lists_each_revision_with_its_own_pipeline_id(self):
        client = _client(revisions=REVISIONS_1545)
        out = _run(server.list_pipeline_revisions, client, pipeline_id=1545)
        assert out["pipeline_id"] == 1545 and out["total"] == 2
        assert out["revisions"] == [
            {"pipeline_id": 1776, "version": "1.0.4", "rev_id": 1, "rev_comment": "Takes the sheet once",
             "created_at": "2026-10-10T00:00:00.000Z", "is_this_id": False},
            {"pipeline_id": 1545, "version": "1.0.3", "rev_id": 0, "rev_comment": "Initial Version",
             "created_at": "2026-09-01T00:00:00.000Z", "is_this_id": True},
        ]
        assert _posts(client) == []

    @pytest.mark.parametrize("revisions", [{"total": 0, "data": []}, {}, None, RuntimeError("404 Not Found")])
    def test_an_id_with_no_revisions_is_refused(self, revisions):
        with pytest.raises(ToolError, match="separate id spaces"):
            _run(server.list_pipeline_revisions, _client(revisions=revisions), pipeline_id=3017)


def _details(**fields):
    """DETAILS_1777 with some saved fields changed."""
    details = json.loads(json.dumps(DETAILS_1777))
    details["pipeline"]["main_pipeline_1777"].update(fields)
    return details


# 1777's graph with a second pipeline output, so a save visibly changes the edges.
GRAPH_1777_TWO_OUTPUTS = dict(
    GRAPH_1777,
    outputs=[{"key": "report", "name": "outputParam"}, {"key": "copy", "name": "copyParam"}],
    connections=GRAPH_1777["connections"] + [{"from": "check", "to": "copy"}],
)


class TestUpdate:
    @pytest.mark.parametrize("confirm", [False, None, "true", 1])
    def test_without_consent_is_refused_before_any_request(self, confirm):
        client = _client(details=DETAILS_1777)
        with pytest.raises(ToolError, match="confirm_update must be true"):
            _run(server.update_pipeline, client, pipeline_id=1777, confirm_update=confirm, **GRAPH_1777)
        client.call.assert_not_called()

    @pytest.mark.parametrize("confirm", ["true", "yes", "1", 1])
    def test_a_client_cannot_consent_with_a_string_or_number(self, confirm):
        client = _client(details=DETAILS_1777, created={"id": 1777})
        with patch.object(server, "get_client", return_value=client), pytest.raises(Exception):
            asyncio.run(server.mcp.call_tool("update_pipeline", dict(
                pipeline_id=1777, confirm_update=confirm, **GRAPH_1777)))
        assert _posts(client) == []

    def test_saves_the_rebuilt_graph_over_the_same_id_keeping_other_fields(self):
        client = _client(details=DETAILS_1777, created={"id": 1777})
        out = _run(server.update_pipeline, client, pipeline_id=1777, confirm_update=True, **GRAPH_1777_TWO_OUTPUTS)
        (post,) = _posts(client)
        body = post.kwargs["data"]
        assert body["id"] == 1777
        assert body["name"] == "GPU smoke check (bootcamp)" and body["summary"] == "s"
        assert body["permission"] == 3 and body["pipelineGroupId"] == 667
        assert len(body["edges"]) == 3 and body["edges"] == out["edges"]
        assert out["pipeline_id"] == 1777
        assert [c["to"] for c in out["connections"]] == ["check", "report", "copy"]
        assert out["previous_edges"] == ["o-inPro-1-252-2_i-3017-0-252-1", "i-outPro-1-201-3_o-3017-0-201-1"]

    def test_name_summary_and_description_change_only_when_given(self):
        client = _client(details=DETAILS_1777, created={"id": 1777})
        _run(server.update_pipeline, client, pipeline_id=1777, confirm_update=True,
             name="Renamed", description="d", **GRAPH_1777)
        body = _posts(client)[0].kwargs["data"]
        assert (body["name"], body["summary"], body["description"]) == ("Renamed", "s", "d")

    @pytest.mark.parametrize("fields,details_extra,match", [
        ({"perms": 63}, {}, "not private"),
        ({"perms": 15}, {}, "not private"),
        ({"pin": "true"}, {}, "pinned"),
        ({}, {"updateAllowed": False}, "cannot edit"),
    ])
    def test_a_shared_or_locked_pipeline_is_refused(self, fields, details_extra, match):
        details = _details(**fields)
        details.update(details_extra)
        client = _client(details=details, created={"id": 1777})
        with pytest.raises(ToolError, match=match):
            _run(server.update_pipeline, client, pipeline_id=1777, confirm_update=True, **GRAPH_1777)
        assert _posts(client) == []

    @pytest.mark.parametrize("details", [{}, None, {"pipeline": {}}])
    def test_an_id_with_no_pipeline_is_refused(self, details):
        client = _client(details=details)
        with pytest.raises(ToolError, match="separate id spaces"):
            _run(server.update_pipeline, client, pipeline_id=3017, confirm_update=True, **GRAPH_1777)
        assert _posts(client) == []

    def test_a_refused_graph_writes_nothing(self):
        client = _client(details=DETAILS_1777)
        graph = dict(GRAPH_1545, connections=[{"from": "analysis", "to": "download"}])
        with pytest.raises(ToolError, match="refused"):
            _run(server.update_pipeline, client, pipeline_id=1777, confirm_update=True, **graph)
        assert _posts(client) == []
