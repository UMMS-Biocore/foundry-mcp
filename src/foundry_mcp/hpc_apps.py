"""
Cluster app launch tools: launch, list, check and stop an app (marimo by
default) on one of the caller's run environments.

The tools talk to the `/api/v1/hpc-app-launches` routes. The functions here
take the HTTP call and the Foundry Connect web address as arguments, so the
server module stays the only place that knows how a client is made.
"""

import json
import re
from typing import Callable, List, Tuple

from mcp.server.fastmcp.exceptions import ToolError

from .client import RequestFailed

LAUNCHES = "/api/v1/hpc-app-launches"

# Same shape as LAUNCH_ID_PATTERN in the backend: 26 lowercase base32 characters.
_LAUNCH_ID = re.compile(r"[a-z2-7]{26}")

# A gateway answer with no verdict from the backend: the write may or may not have
# happened, so it is reported as unknown, never as a refusal.
_GATEWAY_STATUSES = (502, 503, 504)

_LAUNCH_MAY_EXIST = (
    "The launch may have been created before the answer was lost. Call hpc_app_list to "
    "check before trying again, so a second job is not started."
)
_STOP_MAY_HAVE_ARRIVED = (
    "The stop may have reached the server before the answer was lost. Call hpc_app_status "
    "to see whether the launch is stopping before trying again."
)

# `call(method, endpoint, **kwargs)` returns (HTTP status, parsed body).
Call = Callable[..., Tuple[int, object]]

_RECONNECT = (
    "This connection may not run apps. Reconnect Foundry Connect to your assistant and tick "
    "the permission to run apps on your cluster, then try again."
)


def _fail(error: str, reason: str, **extra):
    raise ToolError(json.dumps({"error": error, "reason": reason, **extra}))


def _request(call: Call, method: str, endpoint: str, may_have_arrived: str = "", **kwargs):
    try:
        return call(method, endpoint, **kwargs)
    except RequestFailed as e:
        message = e.message
        if e.may_have_arrived:
            message = f"{message} {may_have_arrived or 'Try again.'}"
        _fail(message, e.reason)


def _refused(body, status: int, fallback_reason: str, fallback_message: str):
    """Raise a ToolError from a refusal, keeping the server's reason and sentence."""
    body = body if isinstance(body, dict) else {}
    error_code = body.get("error")
    if status == 403 and error_code == "execution_not_granted":
        _fail(_RECONNECT, "execution_not_granted", http_status=status)
    reason = body.get("reason") or error_code or fallback_reason
    message = body.get("detail") or body.get("message") or fallback_message
    _fail(message, reason, http_status=status)


def _outcome_unknown(body, status: int, message: str):
    """Raise outcome_unknown when a write got a gateway answer instead of the backend's.

    A 5xx counts as the backend's refusal only when it carries a typed reason. A
    generic error handler body such as {"error": "Internal Server Error"} may follow
    a launch row that was already written, so it stays unknown.
    """
    typed = isinstance(body, dict) and bool(body.get("reason"))
    if status in _GATEWAY_STATUSES or (status >= 500 and not typed):
        _fail(
            f"Foundry Connect did not answer (HTTP {status}). {message}",
            "outcome_unknown",
            http_status=status,
        )


def _page_link(web_base: str) -> str:
    return f"{web_base}/apps/cluster-launches"


def _launch_answer(launch, web_base: str) -> dict:
    launch = launch if isinstance(launch, dict) else {}
    return {
        "launch": launch,
        "mounts": launch.get("mounts", []),
        "open_in_foundry_connect": _page_link(web_base),
    }


def _data_root_requests(data_roots) -> List[dict]:
    if data_roots is None:
        return []
    if not isinstance(data_roots, list):
        _fail("data_roots must be a list of data root names.", "invalid_argument")
    requests_out = []
    for entry in data_roots:
        if isinstance(entry, str) and entry.strip():
            requests_out.append({"name": entry.strip()})
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            requests_out.append(entry)
        else:
            _fail("Each data root is a name, or an object with a name.", "invalid_argument")
    return requests_out


def _bounded(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{name} must be a positive whole number.", "invalid_argument")
    return value


def launch(
    call: Call,
    web_base: str,
    app: str,
    run_environment_id: int,
    data_roots,
    cpus: int,
    memory_mb: int,
    wall_minutes: int,
) -> str:
    wanted_app = (app or "marimo").strip().lower()
    roots = _data_root_requests(data_roots)
    body = {
        "cpus": _bounded(cpus, "cpus"),
        "memoryMb": _bounded(memory_mb, "memory_mb"),
        "wallMinutes": _bounded(wall_minutes, "wall_minutes"),
        "dataRoots": roots,
    }

    status, options = _request(call, "GET", f"{LAUNCHES}/options", retry=True)
    if status != 200:
        _refused(options, status, "options_failed", "Could not read your cluster app options.")
    options = options if isinstance(options, dict) else {}
    environments = options.get("environments") or []
    apps = options.get("apps") or []

    chosen = None
    if run_environment_id:
        chosen = next((e for e in environments if e.get("profileClusterId") == run_environment_id), None)
        if chosen is None:
            _fail(
                "That run environment is not one you can launch apps on.",
                "run_environment_unavailable",
                environments=environments,
            )
    elif len(environments) == 1:
        chosen = environments[0]
    elif not environments:
        _fail(
            "You have no run environment with cluster apps turned on.",
            "no_run_environment",
        )
    else:
        # More than one: the assistant asks the person, then calls again with the id.
        return json.dumps(
            {
                "needs_choice": "run_environment_id",
                "message": "Ask the person which run environment to use, then call again with run_environment_id.",
                "environments": environments,
            },
            indent=2,
        )

    app_row = next(
        (a for a in apps if str(a.get("kind", "")).lower() == wanted_app or str(a.get("name", "")).lower() == wanted_app),
        None,
    )
    if app_row is None:
        _fail(
            f"No launchable app named {wanted_app}.",
            "app_not_available",
            apps=apps,
        )

    body["appId"] = app_row["appId"]
    body["profileClusterId"] = chosen["profileClusterId"]
    # Not retried: a launch has no key, so a retry could start a second job.
    status, created = _request(
        call,
        "POST",
        LAUNCHES,
        data=body,
        may_have_arrived=_LAUNCH_MAY_EXIST,
    )
    if status not in (200, 201):
        _outcome_unknown(created, status, _LAUNCH_MAY_EXIST)
        _refused(created, status, "launch_refused", "The app could not be launched.")
    return json.dumps(_launch_answer(created, web_base), indent=2)


def list_launches(call: Call, web_base: str) -> str:
    status, body = _request(call, "GET", LAUNCHES, retry=True)
    if status != 200:
        _refused(body, status, "list_failed", "Could not list your cluster apps.")
    return json.dumps(
        {"launches": body if isinstance(body, list) else [], "open_in_foundry_connect": _page_link(web_base)},
        indent=2,
    )


def _check_launch_id(launch_id: str) -> str:
    launch_id = launch_id.strip() if isinstance(launch_id, str) else ""
    if not _LAUNCH_ID.fullmatch(launch_id):
        _fail("launch_id must be a launch id as hpc_app_launch or hpc_app_list returned it.", "invalid_launch_id")
    return launch_id


def status_of(call: Call, web_base: str, launch_id: str) -> str:
    launch_id = _check_launch_id(launch_id)
    status, body = _request(call, "GET", f"{LAUNCHES}/{launch_id}", retry=True)
    if status != 200:
        _refused(body, status, "status_failed", "Could not read that launch.")
    return json.dumps(_launch_answer(body, web_base), indent=2)


def stop(call: Call, web_base: str, launch_id: str) -> str:
    launch_id = _check_launch_id(launch_id)
    status, body = _request(
        call, "POST", f"{LAUNCHES}/{launch_id}/stop", may_have_arrived=_STOP_MAY_HAVE_ARRIVED
    )
    if status not in (200, 202):
        _outcome_unknown(body, status, _STOP_MAY_HAVE_ARRIVED)
        _refused(body, status, "stop_failed", "Could not stop that launch.")
    # 202 answers with the launch as it is: stopping, or already ended.
    return json.dumps(_launch_answer(body, web_base), indent=2)
