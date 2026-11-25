import json
import random
from unittest.mock import MagicMock, patch

import requests

from src.foundry_mcp import server

ME = 7
PUBLIC, GROUP, PRIVATE = 63, 15, 3
MY_GROUPS = {100}


# The SDK's own text for every 400: the server's reason is not in it.
SDK_BAD_REQUEST = "Error 302: Bad Request: Check the request parameters or payload."


def _sdk_error(status, message="Internal Server Error", details=None):
    """
    Raise like the SDK: a RuntimeError with its fixed text, raised while
    handling the HTTPError, so the route's body survives only on that
    chained HTTPError's response.
    """
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({"message": message, "details": details or []}).encode()
    response.headers["Content-Type"] = "application/json"
    try:
        raise requests.HTTPError(response=response)
    except requests.HTTPError:
        raise RuntimeError(SDK_BAD_REQUEST if status == 400
                           else f"Error 999: HTTP {status}")


def _joi_refusal(path, kind, message):
    """The route's 400 for a query that fails validation."""
    _sdk_error(400, message, [{"message": message, "path": [path], "type": kind}])


def _visible(app, view):
    owned = app["owner"]["id"] == ME
    public = app["perms"] == PUBLIC
    group = app["group_id"] in MY_GROUPS and app["perms"] == GROUP
    if view == "1":
        return owned
    if view == "2":
        return not owned and (public or group)
    if view == "4":
        return owned or public or group
    raise AssertionError(f"unexpected type {view}")


def _fake_backend(apps, supports_search=True, fail_on=None, seed=0):
    """
    A stand in for GET /api/v1/app that behaves like the real route: type
    defaults to 1 (owned only), take to 10 (max 200), sort to dateCreated asc.
    Rows tied on the sort column come back in an arbitrary order on every
    call, as LIMIT/OFFSET over a non unique sort does.
    """
    calls = []
    rng = random.Random(seed)

    def call(method, endpoint, params=None, **kwargs):
        assert method == "GET"
        assert endpoint == "/api/v1/app"
        params = dict(params or {})
        calls.append(params)
        if fail_on and fail_on[0] == len(calls):
            _sdk_error(fail_on[1])
        if "search" in params and not supports_search:
            _joi_refusal("search", "object.unknown", "'search' is not allowed")
        if len(str(params.get("search") or "")) > 200:
            _joi_refusal("search", "string.max",
                         "'search' length must be less than or equal to 200 characters long")
        take = int(params.get("take", 10))
        if take > 200:
            _joi_refusal("take", "number.max", "'take' must be less than or equal to 200")
        skip = int(params.get("skip", 0))
        view = str(params.get("type", "1"))
        sort = params.get("sort", "dateCreated")
        order = params.get("order", "asc")
        rows = [a for a in apps if _visible(a, view)]
        needle = str(params.get("search") or "").lower()
        if needle:
            rows = [a for a in rows
                    if needle in a["name"].lower()
                    or needle in a["owner"]["username"].lower()]
        rows = [dict(a, own=a["owner"]["id"] == ME) for a in rows]
        rng.shuffle(rows)
        rows.sort(key=lambda a: a[sort], reverse=(order == "desc"))
        return {"skip": skip, "take": take, "total": len(rows),
                "data": rows[skip:skip + take]}

    client = MagicMock()
    client.call.side_effect = call
    return client, calls


def _app(i, owner_id=ME, perms=PRIVATE, group_id=None, name=None):
    return {
        "id": i, "name": name or f"app{i:04d}", "description": f"App {i}",
        "version": "1.0", "status": "active", "image": f"quay.io/x/app{i}:1",
        "cmd": "run --long command line", "env": "A=1", "volume": "/data",
        "blankLaunch": False, "port": 3838, "appKind": "shiny",
        "dateCreated": "2026-01-01T00:00:00Z",
        "owner": {"id": owner_id, "username": f"user{owner_id}"},
        "perms": perms, "group_id": group_id,
    }


def _apps(n):
    return [_app(i) for i in range(1, n + 1)]


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

    def test_server_ignoring_skip_stops_and_reports_truncation(self):
        # A full page every time, always the same rows: stop once a page adds
        # nothing new instead of looping to the cap, and do not claim that
        # the list is complete.
        apps = _apps(200)
        client = MagicMock()
        client.call.return_value = {"skip": 0, "take": 200, "total": 5000, "data": apps}
        out = _run(client)
        assert out["truncated"] is True
        assert [a["id"] for a in out["apps"]] == list(range(1, 201))
        assert client.call.call_count == 2

    def test_plain_list_response_is_still_accepted(self):
        client = MagicMock()
        client.call.return_value = _apps(3)
        out = _run(client)
        assert [a["id"] for a in out] == [1, 2, 3]


class TestListAppsVisibility:
    def test_includes_public_and_group_shared_apps(self):
        apps = [
            _app(1),
            _app(2, owner_id=9, perms=PUBLIC),
            _app(3, owner_id=9, perms=GROUP, group_id=100),
            _app(4, owner_id=9, perms=GROUP, group_id=200),
            _app(5, owner_id=9, perms=PRIVATE),
        ]
        client, calls = _fake_backend(apps)
        out = _run(client)
        assert [a["id"] for a in out] == [1, 2, 3]
        assert all(c["type"] == "4" for c in calls)


class TestListAppsOrdering:
    def test_sorts_by_id_so_tied_dates_do_not_skip_or_repeat(self):
        # Every app has the same creation date, so a date sort would let rows
        # move between pages.
        client, calls = _fake_backend(_apps(650), seed=3)
        out = _run(client)
        assert [a["id"] for a in out] == list(range(1, 651))
        assert all(c["sort"] == "id" and c["order"] == "asc" for c in calls)


class TestListAppsRows:
    def test_rows_are_compact(self):
        client, _ = _fake_backend(_apps(2))
        out = _run(client)
        assert out[0] == {
            "id": 1, "name": "app0001", "description": "App 1",
            "version": "1.0", "status": "active", "image": "quay.io/x/app1:1",
            "appKind": "shiny", "owner": "user7",
        }

    def test_row_without_app_kind_omits_it(self):
        apps = _apps(1)
        del apps[0]["appKind"]
        client, _ = _fake_backend(apps)
        assert "appKind" not in _run(client)[0]


class TestListAppsSearchFallback:
    def test_falls_back_when_server_rejects_search(self):
        apps = _apps(30)
        apps[24]["name"] = "DEBrowser"
        client, calls = _fake_backend(apps, supports_search=False)
        out = _run(client, search="debrowser")
        assert [a["id"] for a in out] == [25]
        assert "search" in calls[0] and "search" not in calls[1]

    def test_server_error_on_first_page_is_reported(self):
        client, calls = _fake_backend(_apps(30), fail_on=(1, 500))
        out = _run(client, search="app0025")
        assert "error" in out
        assert len(calls) == 1

    def test_server_error_on_a_later_page_is_reported(self):
        client, calls = _fake_backend(_apps(450), fail_on=(2, 500))
        out = _run(client, search="app")
        assert "error" in out
        assert len(calls) == 2

    def test_other_bad_request_is_reported(self):
        client = MagicMock()
        client.call.side_effect = lambda **kw: _joi_refusal(
            "type", "string.base", "'type' must be a string")
        out = _run(client, search="x")
        assert out == {"error": SDK_BAD_REQUEST}
        assert client.call.call_count == 1

    def test_a_rejected_search_value_is_reported_not_retried(self):
        # The server knows search, it only refuses this value: the fallback
        # would silently return a locally filtered list instead.
        client, calls = _fake_backend(_apps(30))
        out = _run(client, search="x" * 201)
        assert out == {"error": SDK_BAD_REQUEST}
        assert len(calls) == 1

    def test_fallback_reads_the_refusal_from_the_response_body(self):
        # The SDK text never names search; only the chained body does.
        client, calls = _fake_backend(_apps(3), supports_search=False)
        _run(client, search="app0002")
        assert len(calls) == 2
        client = MagicMock()
        client.call.side_effect = lambda **kw: _sdk_error(400, "something else")
        assert _run(client, search="app0002") == {"error": SDK_BAD_REQUEST}

    def test_fallback_also_matches_the_owner(self):
        apps = [_app(1), _app(2, owner_id=9, perms=PUBLIC)]
        client, _ = _fake_backend(apps, supports_search=False)
        out = _run(client, search="USER9")
        assert [a["id"] for a in out] == [2]

    def test_fallback_truncation_says_rest_was_not_checked(self):
        apps = _apps(server._APP_LIST_MAX + 10)
        client, _ = _fake_backend(apps, supports_search=False)
        out = _run(client, search="app0001")
        assert out["truncated"] is True
        assert out["total"] is None
        assert "not checked" in out["note"]
        assert [a["id"] for a in out["apps"]] == [1]


class TestInstalledApps:
    def test_pages_through_the_callers_own_apps_only(self):
        client, calls = _fake_backend(_apps(25) + [_app(26, owner_id=9, perms=PUBLIC)])
        apps = server._installed_apps(client)
        assert [a["id"] for a in apps] == list(range(1, 26))
        assert all(c["type"] == "1" and c["sort"] == "id" for c in calls)

    def test_list_apps_still_shows_shared_apps_with_their_owner(self):
        client, _ = _fake_backend([_app(1), _app(2, owner_id=9, perms=PUBLIC)])
        out = _run(client)
        assert [(a["id"], a["owner"]) for a in out] == [(1, "user7"), (2, "user9")]


class _Frame(list):
    def to_dict(self, orient="records"):
        return list(self)


def _suggest(apps):
    client, _ = _fake_backend(apps)
    client.reports.fetch_report_data.return_value = {"report": True}
    client.reports.get_all_files.return_value = _Frame([{
        "id": "align/sample.bam", "processName": "align",
        "file_path": "align/sample.bam", "name": "sample.bam",
        "extension": "bam", "fileSize": 10,
    }])
    with patch.object(server, "get_client", return_value=client):
        return json.loads(server.suggest_apps("1"))


class TestSuggestAppsImpersonation:
    def test_same_named_app_of_another_user_is_not_suggested(self):
        out = _suggest([_app(5, owner_id=9, perms=PUBLIC, name="IGV")])
        ids = [s["app_id"] for s in out["data"]["suggestions"]]
        assert 5 not in ids

    def test_group_shared_same_named_app_is_not_suggested(self):
        out = _suggest([_app(5, owner_id=9, perms=GROUP, group_id=100, name="IGV")])
        assert [s["app_id"] for s in out["data"]["suggestions"]] == []

    def test_the_callers_own_app_is_suggested(self):
        out = _suggest([
            _app(5, owner_id=9, perms=PUBLIC, name="IGV"),
            _app(6, name="IGV"),
        ])
        assert [s["app_id"] for s in out["data"]["suggestions"]] == [6]

    def test_rows_not_marked_own_are_dropped_even_if_the_server_ignores_type(self):
        rows = [
            dict(_app(5, owner_id=9, perms=PUBLIC, name="IGV"), own=False),
            {k: v for k, v in _app(6, owner_id=9, perms=PUBLIC, name="IGV").items()},
        ]
        client = MagicMock()
        client.call.return_value = {"skip": 0, "take": 200, "total": 2, "data": rows}
        client.reports.fetch_report_data.return_value = {"report": True}
        client.reports.get_all_files.return_value = _Frame([{
            "id": "a.bam", "processName": "align", "file_path": "a.bam",
            "name": "a.bam", "extension": "bam", "fileSize": 10,
        }])
        assert "own" not in rows[1]
        with patch.object(server, "get_client", return_value=client):
            out = json.loads(server.suggest_apps("1"))
        assert out["data"]["suggestions"] == []

    def test_no_suggestion_says_only_own_apps_were_considered(self):
        out = _suggest([_app(5, owner_id=9, perms=PUBLIC, name="IGV")])
        assert "your own apps" in out["summary"]
        assert any("list_apps" in step and "shared" in step for step in out["next_steps"])
