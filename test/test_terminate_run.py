"""Tests for terminate_run, which stops a stale or hung run."""
import json
from unittest.mock import MagicMock, patch

import requests

from src.foundry_mcp import server


def _terminate(call_return="", side_effect=None, **kwargs):
    client = MagicMock()
    client.call.return_value = call_return
    if side_effect is not None:
        client.call.side_effect = side_effect
    with patch.object(server, "get_client", return_value=client):
        out = json.loads(server.terminate_run(**kwargs))
    return out, client


class TestRefusesWithoutConfirmation:
    def test_default_refuses_and_calls_nothing(self):
        client = MagicMock()
        with patch.object(server, "get_client", return_value=client) as get_client:
            out = json.loads(server.terminate_run("41"))
        assert "confirm_terminate" in out["error"]
        client.call.assert_not_called()
        get_client.assert_not_called()

    def test_truthy_non_true_value_refuses(self):
        out, client = _terminate(run_id="41", confirm_terminate="yes")
        assert "confirm_terminate" in out["error"]
        client.call.assert_not_called()

    def test_non_numeric_run_id_refuses(self):
        out, client = _terminate(run_id="abc", confirm_terminate=True)
        assert "run_id" in out["error"]
        client.call.assert_not_called()


class TestTerminates:
    def test_calls_terminate_endpoint_without_override(self):
        out, client = _terminate(run_id="41", confirm_terminate=True)
        client.call.assert_called_once_with(
            method="GET", endpoint="/api/v1/run/41/terminate"
        )
        assert "error" not in out

    def test_reply_is_an_envelope_with_next_steps(self):
        out, _ = _terminate(run_id=" 41 ", confirm_terminate=True)
        assert "41" in out["summary"]
        assert out["data"] == {"run_id": "41", "status": "Terminated"}
        steps = " ".join(out["next_steps"])
        assert "get_run" in steps
        assert "newrun" in steps
        assert "resume" in steps.lower()

    def test_integer_run_id_is_accepted(self):
        _, client = _terminate(run_id=41, confirm_terminate=True)
        assert client.call.call_args.kwargs["endpoint"] == "/api/v1/run/41/terminate"


class TestErrors:
    def test_plain_exception_becomes_error(self):
        out, _ = _terminate(side_effect=RuntimeError("boom"), run_id="41", confirm_terminate=True)
        assert out == {"error": "boom"}

    def test_server_refusal_message_is_kept(self):
        response = MagicMock()
        response.status_code = 409
        response.json.return_value = {
            "message": "Run 41 was not terminated: the agent could not confirm that job 7 stopped."
        }
        http_error = requests.HTTPError("409")
        http_error.response = response

        def _raise(**_kwargs):
            try:
                raise http_error
            except requests.HTTPError as e:
                raise RuntimeError("Request failed with status 409") from e

        out, _ = _terminate(side_effect=_raise, run_id="41", confirm_terminate=True)
        assert out["status"] == 409
        assert "could not confirm" in out["error"]
