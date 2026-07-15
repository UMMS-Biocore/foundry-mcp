"""Tests for run-execution tools."""
import json
from unittest.mock import MagicMock, patch

from src.viafoundry_mcp import server


def _patched_client(call_return):
    """Return a MagicMock ViaFoundryClient whose .call returns call_return."""
    client = MagicMock()
    client.call.return_value = call_return
    return client


class TestGetRunDetails:
    def test_hits_details_endpoint_and_returns_json(self):
        details = {
            "id": 123, "inputs": [], "processOptions": {},
            "permission": 2, "groupId": 5, "mainPipeline": {"id": 1408},
        }
        client = _patched_client(details)
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_details("123")
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/run/123/details"
        )
        assert json.loads(result)["groupId"] == 5

    def test_returns_error_json_on_exception(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError("boom")
        with patch.object(server, "get_client", return_value=client):
            result = server.get_run_details("123")
        assert json.loads(result) == {"error": "boom"}
