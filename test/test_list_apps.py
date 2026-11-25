import json
from unittest.mock import MagicMock, patch

from src.foundry_mcp import server


def _fake_backend(apps, default_take=10, max_take=200, supports_search=True):
    """A stand in for GET /api/v1/app that pages like the real route."""
    calls = []

    def call(method, endpoint, params=None, **kwargs):
        assert method == "GET"
        assert endpoint == "/api/v1/app"
        params = dict(params or {})
        calls.append(params)
        if "search" in params and not supports_search:
            raise RuntimeError('400 Bad Request: "search" is not allowed')
        take = int(params.get("take", default_take))
        if take > max_take:
            raise RuntimeError("400 Bad Request: take must be <= 200")
        skip = int(params.get("skip", 0))
        rows = apps
        needle = str(params.get("search") or "").lower()
        if needle:
            rows = [
                a for a in rows
                if needle in a["name"].lower() or needle in a["owner"].lower()
            ]
        return {"skip": skip, "take": take, "total": len(rows),
                "data": rows[skip:skip + take]}

    client = MagicMock()
    client.call.side_effect = call
    return client, calls


def _apps(n):
    return [{"id": i, "name": f"app{i:04d}", "owner": "admin"} for i in range(1, n + 1)]


def _run(client, **kwargs):
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.list_apps(**kwargs))


class TestListAppsPaging:
    def test_returns_apps_beyond_the_first_page(self):
        client, calls = _fake_backend(_apps(25))
        out = _run(client)
        assert isinstance(out, list)
        assert [a["id"] for a in out] == list(range(1, 26))
        assert all("take" in c and "skip" in c for c in calls)

    def test_pages_past_the_route_maximum(self):
        client, calls = _fake_backend(_apps(450))
        out = _run(client)
        assert [a["id"] for a in out] == list(range(1, 451))
        assert [c["skip"] for c in calls] == [0, 200, 400]

    def test_search_finds_an_app_beyond_the_first_page(self):
        apps = _apps(30)
        apps[24]["name"] = "DEBrowser"
        client, calls = _fake_backend(apps)
        out = _run(client, search="debrowser")
        assert [a["id"] for a in out] == [25]
        assert calls[0]["search"] == "debrowser"

    def test_search_falls_back_when_server_lacks_search(self):
        apps = _apps(30)
        apps[24]["name"] = "DEBrowser"
        client, _ = _fake_backend(apps, supports_search=False)
        out = _run(client, search="debrowser")
        assert [a["id"] for a in out] == [25]

    def test_no_match_returns_empty_list(self):
        client, _ = _fake_backend(_apps(30))
        assert _run(client, search="nothing-like-this") == []

    def test_cap_reports_truncation(self):
        client, calls = _fake_backend(_apps(server._APP_LIST_MAX + 50))
        out = _run(client)
        assert out["truncated"] is True
        assert out["total"] == server._APP_LIST_MAX + 50
        assert len(out["apps"]) == server._APP_LIST_MAX
        assert "search" in out["note"]
        assert sum(int(c["take"]) for c in calls) <= server._APP_LIST_MAX

    def test_stops_when_server_ignores_skip(self):
        # A full page every time, always the same rows: stop once a page adds
        # nothing new instead of looping up to the cap.
        apps = _apps(200)
        client = MagicMock()
        client.call.return_value = {"skip": 0, "take": 200, "total": 5000, "data": apps}
        out = _run(client)
        assert [a["id"] for a in out] == list(range(1, 201))
        assert client.call.call_count == 2

    def test_plain_list_response_is_still_accepted(self):
        client = MagicMock()
        client.call.return_value = _apps(3)
        out = _run(client)
        assert [a["id"] for a in out] == [1, 2, 3]
