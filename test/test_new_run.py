import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from src.foundry_mcp import server


def _patched_client(call_return=None, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.call.side_effect = side_effect
    else:
        client.call.return_value = call_return
    return client


ENV = {
    "id": 550, "name": "Cluster", "transport": "ssh", "executor": "lsf",
    "hostname": "hpc.example.org", "username": "jdoe", "defWorkdir": None,
    "defPublishdir": None, "jobQueue": "short", "bashVariable": "x",
}


class TestDefaultExecutionDirectory:
    def test_declared_default_wins_as_is(self):
        env = {**ENV, "defWorkdir": "/project/lab/runs"}
        assert server._default_execution_directory(env, "My run") == "/project/lab/runs"

    def test_home_pattern_from_environment_login(self):
        assert server._default_execution_directory(ENV, "RNA seq 1") == "/home/jdoe/foundry-connect-runs/RNA_seq_1"

    @pytest.mark.parametrize("name", ["../../etc", "..", "a/../../b", ".hidden"])
    def test_run_name_cannot_escape_the_runs_folder(self, name):
        path = server._default_execution_directory(ENV, name)
        tail = path[len("/home/jdoe/foundry-connect-runs/"):]
        assert path.startswith("/home/jdoe/foundry-connect-runs/")
        assert "/" not in tail and tail not in ("", ".", "..") and not tail.startswith(".")

    def test_empty_run_name_becomes_run(self):
        assert server._default_execution_directory(ENV, "  ") == "/home/jdoe/foundry-connect-runs/run"

    @pytest.mark.parametrize("user", [None, "", "  ", "a/b", "two words"])
    def test_no_usable_login_means_no_default(self, user):
        assert server._default_execution_directory({**ENV, "username": user}, "r") is None


class TestCompactRunEnvironment:
    def test_keeps_only_the_fields_a_user_chooses_by(self):
        assert server._compact_run_environment(ENV) == {
            "id": 550, "name": "Cluster", "transport": "ssh", "executor": "lsf",
            "hostname": "hpc.example.org", "username": "jdoe",
            "defWorkdir": None, "defPublishdir": None,
        }
