"""Tests for make_sample_sheet.

BOTH formats were read off the cluster from live run 12194, not guessed:

  metadata.tsv      sample_name<TAB>group
  comparisons.tsv   controls<TAB>treats<TAB>names
                    control<TAB>exper<TAB>control_vs_exper

An earlier draft of this work guessed the comparison filename and concluded the
run was broken. Read the real thing.
"""
import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server

LAUNCH_DIR = "/pi/alper.kucukural-umw/kucukura/test/aligners"
UUID = "8597b4294de7456ea44b72079e242213"

GROUPS = {"control_rep1": "control", "control_rep2": "control",
          "exper_rep1": "exper", "exper_rep2": "exper"}


def _client(upload_report=True, launch_dir=LAUNCH_DIR):
    c = MagicMock()

    def _call(**kwargs):
        endpoint = kwargs.get("endpoint", "")
        if endpoint.endswith("/details"):
            return {"name": "R", "runUUID": UUID, "launchDirectory": launch_dir,
                    "runEnvironment": {"selectedId": 550}, "inputs": [],
                    "processOptions": {}}
        if "configurations/v1/user" in endpoint:
            return {"UPLOAD_REPORT": upload_report}
        return {}

    c.call.side_effect = _call
    return c


def _make(groups=None, comparisons=None, upload_fails=False, **kwargs):
    """Returns (parsed, upload_mock). The upload helper is patched because it
    is a raw multipart POST, not a client.call()."""
    client = _client(**kwargs)
    uploader = MagicMock(
        side_effect=RuntimeError("upload exploded") if upload_fails else None)
    with patch.object(server, "get_client", return_value=client), \
         patch.object(server, "_upload_run_file", uploader):
        return json.loads(server.make_sample_sheet(
            "12194", GROUPS if groups is None else groups, comparisons)), uploader


class TestRendering:
    def test_renders_the_verified_groups_format(self):
        text = server._render_groups_tsv(
            {"control_rep1": "control", "exper_rep1": "exper"})
        assert text == ("sample_name\tgroup\n"
                        "control_rep1\tcontrol\n"
                        "exper_rep1\texper\n")

    def test_renders_the_verified_comparisons_format(self):
        text = server._render_comparisons_tsv([["control", "exper"]])
        assert text == ("controls\ttreats\tnames\n"
                        "control\texper\tcontrol_vs_exper\n")

    def test_an_explicit_comparison_name_is_kept(self):
        text = server._render_comparisons_tsv([["control", "exper", "my_label"]])
        assert text.splitlines()[1].endswith("\tmy_label")

    def test_group_order_follows_first_appearance_not_the_alphabet(self):
        """A scientist reading the sheet expects their own ordering back."""
        text = server._render_groups_tsv({"z_sample": "treated",
                                          "a_sample": "control"})
        assert text.splitlines()[1].startswith("z_sample")


class TestMakeSampleSheet:
    def test_returns_the_absolute_cluster_paths_to_set_as_inputs(self):
        parsed, _ = _make(comparisons=[["control", "exper"]])
        data = parsed["data"]
        assert data["groups_file"] == (
            f"{LAUNCH_DIR}/foundryUploads/run12194/metadata.tsv")
        assert data["compare_file"] == (
            f"{LAUNCH_DIR}/foundryUploads/run12194/comparisons.tsv")

    def test_uploads_with_a_relative_dir(self):
        """The endpoint writes to the web server's report dir and path.join()s
        `dir` onto it — an absolute dir silently nests inside that directory
        instead of landing where the input points."""
        _, uploader = _make(comparisons=[["control", "exper"]])
        dirs = {c.kwargs["remote_dir"] for c in uploader.call_args_list}
        assert dirs == {"foundryUploads/run12194"}
        for d in dirs:
            assert not d.startswith("/")

    def test_uploads_both_sheets(self):
        _, uploader = _make(comparisons=[["control", "exper"]])
        names = {c.kwargs["file_name"] for c in uploader.call_args_list}
        assert names == {"metadata.tsv", "comparisons.tsv"}

    def test_only_the_groups_sheet_when_no_comparisons_are_given(self):
        parsed, uploader = _make()
        names = {c.kwargs["file_name"] for c in uploader.call_args_list}
        assert names == {"metadata.tsv"}
        assert parsed["data"]["compare_file"] is None

    def test_always_returns_the_content_so_it_is_never_a_black_box(self):
        parsed, _ = _make(comparisons=[["control", "exper"]])
        assert "sample_name\tgroup" in parsed["data"]["groups_tsv"]
        assert "controls\ttreats\tnames" in parsed["data"]["comparisons_tsv"]

    def test_next_steps_wire_the_paths_into_update_run_then_preflight(self):
        parsed, _ = _make(comparisons=[["control", "exper"]])
        joined = " ".join(parsed["next_steps"])
        assert "update_run" in joined
        assert "groups_file" in joined
        assert "preflight_run" in joined

    def test_refuses_a_group_with_no_replicates(self):
        parsed, _ = _make(groups={"a": "control", "b": "control", "c": "solo"})
        assert "error" in parsed
        assert "solo" in parsed["error"]

    def test_a_single_replicate_group_can_be_forced(self):
        """Refusing outright would be a dead end for someone who means it."""
        client = _client()
        with patch.object(server, "get_client", return_value=client), \
             patch.object(server, "_upload_run_file", MagicMock()):
            parsed = json.loads(server.make_sample_sheet(
                "12194", {"a": "control", "b": "control", "c": "solo"},
                allow_single_replicate=True))
        assert "error" not in parsed

    def test_refuses_a_comparison_naming_an_unknown_group(self):
        parsed, _ = _make(comparisons=[["control", "typo_group"]])
        assert "error" in parsed
        assert "typo_group" in parsed["error"]

    def test_refuses_empty_groups(self):
        parsed, _ = _make(groups={})
        assert "error" in parsed

    def test_refuses_when_the_run_has_no_launch_directory(self):
        """Without it there is no path to point the input at."""
        parsed, _ = _make(launch_dir="")
        assert "error" in parsed
        assert "launch" in parsed["error"].lower()

    def test_degrades_honestly_when_the_upload_feature_is_off(self):
        parsed, uploader = _make(comparisons=[["control", "exper"]],
                                 upload_report=False)
        assert "error" not in parsed
        assert uploader.call_count == 0
        assert parsed["data"]["uploaded"] is False
        assert "sample_name\tgroup" in parsed["data"]["groups_tsv"]
        joined = " ".join(parsed["next_steps"]) + parsed["summary"]
        assert "UPLOAD_REPORT" in joined or "not enabled" in joined.lower()

    def test_a_failed_upload_is_reported_not_swallowed(self):
        parsed, _ = _make(comparisons=[["control", "exper"]], upload_fails=True)
        assert "error" in parsed

    def test_accepts_groups_as_a_list_of_pairs(self):
        parsed, _ = _make(groups=[["s1", "control"], ["s2", "control"]])
        assert "error" not in parsed
        assert "s1\tcontrol" in parsed["data"]["groups_tsv"]

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.make_sample_sheet("12194", GROUPS)
        assert json.loads(result) == {"error": "boom"}
