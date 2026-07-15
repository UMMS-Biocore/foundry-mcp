"""Opt-in live integration test. Runs only when VIAFOUNDRY_LIVE_* env vars are set."""
import json
import os
import pytest

from src.viafoundry_mcp import server, config

LIVE = all(os.environ.get(k) for k in (
    "VIAFOUNDRY_LIVE_HOST", "VIAFOUNDRY_LIVE_TOKEN", "VIAFOUNDRY_LIVE_SOURCE_RUN"
))
pytestmark = pytest.mark.skipif(not LIVE, reason="live ViaFoundry creds not set")


def test_get_run_details_roundtrip():
    config.set_credentials(
        os.environ["VIAFOUNDRY_LIVE_HOST"], os.environ["VIAFOUNDRY_LIVE_TOKEN"]
    )
    try:
        result = json.loads(
            server.get_run_details(os.environ["VIAFOUNDRY_LIVE_SOURCE_RUN"])
        )
        assert "error" not in result
        assert "permission" in result and "groupId" in result
    finally:
        config.set_credentials(None, None)
