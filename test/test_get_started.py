"""Tests for get_started — the recipe surface.

X1 in the plan was MCP `prompts`. FastMCP serves them and nginx cannot block
them (prompts/list is a JSON-RPC method inside the same POST body as
tools/list), but whether the claude.ai connector UI *renders* prompts to a user
is unconfirmed — and an unsurfaced recipe helps nobody. Tools are surfaced for
certain, so the recipes live in one.
"""
import json

from src.foundry_mcp import server


def _recipes(**kwargs):
    return json.loads(server.get_started(**kwargs))


class TestGetStarted:
    def test_needs_no_backend_call(self):
        """Guidance must work before credentials resolve — it is what the model
        reads to find out how to use everything else."""
        parsed = _recipes()
        assert parsed["data"]["recipes"]

    def test_covers_the_three_journeys_a_scientist_arrives_with(self):
        names = " ".join(r["name"].lower() for r in _recipes()["data"]["recipes"])
        assert "start" in names or "new analysis" in names
        assert "fail" in names or "wrong" in names
        assert "result" in names

    def test_the_starting_recipe_chains_discovery_through_to_launch(self):
        recipe = next(r for r in _recipes()["data"]["recipes"]
                      if r["id"] == "start_an_analysis")
        chained = " ".join(recipe["steps"])
        for tool in ("recommend_pipeline", "plan_run", "duplicate_run",
                     "update_run", "initiate_run"):
            assert tool in chained
        assert chained.index("recommend_pipeline") < chained.index("plan_run")
        assert chained.index("plan_run") < chained.index("duplicate_run")
        assert chained.index("duplicate_run") < chained.index("initiate_run")

    def test_the_failure_recipe_reaches_for_the_log(self):
        recipe = next(r for r in _recipes()["data"]["recipes"]
                      if r["id"] == "diagnose_a_failure")
        chained = " ".join(recipe["steps"])
        assert "get_run_log" in chained
        assert "get_run" in chained

    def test_the_results_recipe_leads_with_findings_not_a_file_list(self):
        recipe = next(r for r in _recipes()["data"]["recipes"]
                      if r["id"] == "find_my_results")
        chained = " ".join(recipe["steps"])
        assert "list_runs" in chained
        assert "summarize_results" in chained
        assert "suggest_apps" in chained
        # The findings must come before the file inventory.
        assert chained.index("summarize_results") < chained.index("list_results")

    def test_every_recipe_says_when_to_use_it(self):
        for recipe in _recipes()["data"]["recipes"]:
            assert recipe["when"]
            assert recipe["steps"]
            assert recipe["id"]

    def test_filters_to_one_recipe_by_topic(self):
        parsed = _recipes(topic="failed")
        ids = [r["id"] for r in parsed["data"]["recipes"]]
        assert ids == ["diagnose_a_failure"]

    def test_an_unknown_topic_falls_back_to_everything(self):
        parsed = _recipes(topic="quantum teleportation")
        assert len(parsed["data"]["recipes"]) == 3
        assert "all" in parsed["summary"].lower()

    def test_repeats_the_compute_guardrail(self):
        parsed = _recipes()
        blob = json.dumps(parsed)
        assert "HPC" in blob or "compute" in blob
        assert "confirm" in blob.lower()

    def test_uses_the_standard_envelope(self):
        assert list(_recipes()) == ["summary", "next_steps", "data"]
