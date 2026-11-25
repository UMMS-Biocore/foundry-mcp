"""
Compute session tools: list, launch, stop, start, extend, edit, terminate and
delete a Workbench cloud VM, terminate a shared server app instance, and build
the Foundry Connect link that opens any compute session in the person's browser.

Every tool that starts, stops, changes or deletes a session needs the
"Manage my compute sessions" permission on the assistant connection. Opening a
session never goes through this server: a VM login, a shared server cookie or a
cluster app ticket is a browser credential, so `compute_open` only builds the
page link and the page does the rest in the person's signed in browser.

The functions here take the HTTP call and the Foundry Connect web address as
arguments, so the server module stays the only place that knows how a client
is made.
"""

import json
import re
from typing import Callable, Optional, Tuple

from mcp.server.fastmcp.exceptions import ToolError

from .client import RequestFailed

WORKBENCH = "/api/v1/workbench"
APP_TERMINATE = "/api/v1/app/terminate"

# `call(method, endpoint, **kwargs)` returns (HTTP status, parsed body).
Call = Callable[..., Tuple[int, object]]

# The box exactly as the Foundry Connect consent page labels it.
COMPUTE_CONTROL_LABEL = "Manage my compute sessions"

_RECONNECT = (
    "This assistant connection may not start, stop, change or delete compute sessions. "
    f'Reconnect Foundry Connect to your assistant and tick "{COMPUTE_CONTROL_LABEL}", then try again.'
)

# Same shape as the session id Foundry Connect makes: `<kind>:<source id>`, a positive
# integer for Workbench and shared server apps, a cluster launch id for cluster apps.
_SESSION_ID = re.compile(r"(?:(?:workbench|shinyproxy-app):[1-9][0-9]{0,9}|cluster-app:[a-z2-7]{26})")

# The machine types the Workbench launch and edit forms offer.
INSTANCE_TYPES = (
    "n2-highmem-2",
    "n2-highmem-4",
    "n2-highmem-8",
    "n2-highmem-16",
    "n2-highmem-32",
    "n2-highmem-48",
    "n2-standard-4",
    "n2-standard-8",
    "n2-standard-16",
    "n2-standard-32",
    "n2-standard-48",
    "n2-standard-64",
    "n2-standard-80",
    "n2-standard-96",
)

# The auto stop may be moved at most 10 days ahead.
_MAX_EXTEND_MINUTES = 10 * 24 * 60

# Workbench status -> (compute state, actions), as the Compute pages show them.
_RUNNING_ACTIONS = ["open", "extend", "edit", "stop", "terminate"]
_STOPPED_ACTIONS = ["edit", "start", "terminate"]
_ENDED_ACTIONS = ["delete"]
_WORKBENCH_STATES = {
    "PROVISIONING": ("starting", []),
    "STAGING": ("starting", []),
    "RESUMING": ("starting", []),
    "REPAIRING": ("starting", []),
    "RUNNING": ("running", _RUNNING_ACTIONS),
    "PENDING_STOP": ("stopping", []),
    "STOPPING": ("stopping", []),
    "SUSPENDING": ("stopping", []),
    "TERMINATING": ("stopping", []),
    "SUSPENDED": ("stopped", _STOPPED_ACTIONS),
    "TERMINATED": ("stopped", _STOPPED_ACTIONS),
    "DELETED": ("ended", _ENDED_ACTIONS),
    "FAILED": ("ended", _ENDED_ACTIONS),
}

# A gateway answer with no verdict from the backend: the write may or may not have
# happened, so it is reported as unknown, never as a refusal.
_GATEWAY_STATUSES = (502, 503, 504)

_LAUNCH_MAY_EXIST = (
    "The machine may have been created before the answer was lost. Call workbench_list to "
    "check before launching again, so a second billed machine is not started."
)
_CHANGE_MAY_HAVE_ARRIVED = (
    "The change may have reached the server before the answer was lost. Call workbench_list "
    "to see the machine's state before trying again."
)
_APP_MAY_HAVE_ARRIVED = (
    "The stop may have reached the server before the answer was lost. Ask the person to check "
    "the Compute page before trying again."
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
    if status == 403 and error_code == "compute_control_not_granted":
        _fail(_RECONNECT, "compute_control_not_granted", http_status=status)
    reason = body.get("reason") or (error_code if isinstance(error_code, str) else None) or fallback_reason
    message = body.get("message") or body.get("detail") or fallback_message
    _fail(message, reason, http_status=status)


def _outcome_unknown(body, status: int, message: str):
    """Raise outcome_unknown when a write got a gateway answer instead of the backend's."""
    typed = isinstance(body, dict) and bool(body.get("reason") or body.get("message"))
    if status in _GATEWAY_STATUSES or (status >= 500 and not typed):
        _fail(
            f"Foundry Connect did not answer (HTTP {status}). {message}",
            "outcome_unknown",
            http_status=status,
        )


def _write(call: Call, method: str, endpoint: str, may_have_arrived: str, fallback_message: str, **kwargs):
    # Never retried: a lost answer is reported, so a launch or a stop is not sent twice.
    status, body = _request(call, method, endpoint, may_have_arrived=may_have_arrived, **kwargs)
    if status not in (200, 201, 202, 204):
        _outcome_unknown(body, status, may_have_arrived)
        _refused(body, status, "refused", fallback_message)
    return status, body


def _open_link(web_base: str, session_id: str) -> str:
    return f"{web_base}/compute/open/{session_id}"


def _positive(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{name} must be a positive whole number.", "invalid_argument")
    return value


def _source_id(value, kind: str, name: str) -> int:
    """A positive integer id, given as a number, as digits or as `<kind>:<id>`."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(f"{kind}:"):
            text = text[len(kind) + 1:]
        elif ":" in text:
            _fail(f"{name} must be a {kind} id or a {kind}:<id> session id.", "invalid_argument")
        if re.fullmatch(r"[1-9][0-9]{0,9}", text):
            return int(text)
        _fail(f"{name} must be a positive whole number.", "invalid_argument")
    return _positive(value, name)


def _instance_type(value) -> str:
    if value not in INSTANCE_TYPES:
        _fail(
            "instance_type must be one of the machine types the Workbench form offers.",
            "invalid_argument",
            instance_types=list(INSTANCE_TYPES),
        )
    return value


def _session_answer(web_base: str, kind: str, source_id: int, message: str, openable: bool = True, **extra) -> str:
    session_id = f"{kind}:{source_id}"
    answer = {"kind": kind, "session_id": session_id, "message": message, **extra}
    if openable:
        answer["open_in_foundry_connect"] = _open_link(web_base, session_id)
    return json.dumps(answer, indent=2)


# ---------------------------------------------------------------- open


def open_link(web_base: str, session_id: str) -> str:
    session_id = session_id.strip() if isinstance(session_id, str) else ""
    if not _SESSION_ID.fullmatch(session_id):
        _fail(
            "session_id must be <kind>:<id> as workbench_list returns it, shinyproxy-app:<instance id> "
            "or cluster-app:<launch id>.",
            "invalid_session_id",
        )
    kind = session_id.split(":", 1)[0]
    return json.dumps(
        {
            "kind": kind,
            "session_id": session_id,
            "open_in_foundry_connect": _open_link(web_base, session_id),
            "message": (
                "Give the person this link to open the session in their browser. It opens the "
                "existing session and never launches a new one."
            ),
        },
        indent=2,
    )


# ---------------------------------------------------------------- Workbench reads


def _workbench_row(row: dict) -> dict:
    status = row.get("status")
    state, actions = _WORKBENCH_STATES.get(status, ("unknown", []))
    instance_id = row.get("id")
    # accessUrl is left out on purpose: a session is opened through compute_open.
    return {
        "kind": "workbench",
        "session_id": f"workbench:{instance_id}",
        "instance_id": instance_id,
        "name": row.get("name") or "",
        "application": row.get("workbench"),
        "status": status,
        "state": state,
        "actions": list(actions),
        "owner": row.get("owner"),
        "uptime_seconds": row.get("uptimeSeconds"),
        "created_at": row.get("dateCreated"),
        "state_changed_at": row.get("vmLastStateChangedAt"),
    }


def list_workbench(call: Call, web_base: str, take: int, skip: int) -> str:
    take = max(1, min(take, 200)) if isinstance(take, int) and not isinstance(take, bool) else 50
    skip = max(0, skip) if isinstance(skip, int) and not isinstance(skip, bool) else 0
    status, body = _request(
        call, "GET", f"{WORKBENCH}/instance/list", params={"take": take, "skip": skip}, retry=True
    )
    if status != 200:
        _refused(body, status, "list_failed", "Could not list your Workbench machines.")
    body = body if isinstance(body, dict) else {}
    rows = body.get("data") if isinstance(body.get("data"), list) else []
    sessions = [_workbench_row(r) for r in rows if isinstance(r, dict)]
    totals = (body.get("metadata") or {}).get("totals") if isinstance(body.get("metadata"), dict) else None
    return json.dumps(
        {
            "sessions": sessions,
            "total": body.get("total", len(sessions)),
            "skip": skip,
            "take": take,
            "totals": totals,
            "open_in_foundry_connect": f"{web_base}/compute",
        },
        indent=2,
    )


def list_workbench_applications(call: Call, web_base: str) -> str:
    applications = []
    seen = set()
    # 1 lists the person's own applications, 2 those shared with them.
    for view in ("1", "2"):
        status, body = _request(
            call,
            "GET",
            f"{WORKBENCH}/application/list",
            params={"type": view, "take": 200, "skip": 0},
            retry=True,
        )
        if status != 200:
            _refused(body, status, "list_failed", "Could not list the Workbench applications.")
        rows = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), list) else []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            applications.append(
                {
                    "application_id": row.get("id"),
                    "name": row.get("name"),
                    "description": row.get("description") or "",
                    "template": (row.get("template") or {}).get("title") if isinstance(row.get("template"), dict) else None,
                    "own": row.get("own"),
                }
            )
    return json.dumps(
        {
            "applications": applications,
            "instance_types": list(INSTANCE_TYPES),
            "open_in_foundry_connect": f"{web_base}/compute",
        },
        indent=2,
    )


# ---------------------------------------------------------------- Workbench writes


def launch_workbench(
    call: Call, web_base: str, application_id, instance_type, disk_size_gb, name: Optional[str]
) -> str:
    application_id = _positive(application_id, "application_id")
    body = {
        "name": (name or "").strip(),
        "instanceType": _instance_type(instance_type),
        "diskSize": _positive(disk_size_gb, "disk_size_gb"),
        # Always empty: Foundry Connect makes the VM login itself, and only the person
        # reads it, in their browser. This server never sets or sees one.
        "password": "",
    }
    _, created = _write(
        call,
        "POST",
        f"{WORKBENCH}/instance/create/{application_id}",
        _LAUNCH_MAY_EXIST,
        "The Workbench machine could not be launched.",
        data=body,
    )
    created = created if isinstance(created, dict) else {}
    instance_id = created.get("id")
    if not isinstance(instance_id, int):
        _fail(
            f"Foundry Connect accepted the launch but did not say which machine it made. {_LAUNCH_MAY_EXIST}",
            "outcome_unknown",
        )
    return _session_answer(
        web_base,
        "workbench",
        instance_id,
        "The machine is being created and is billed from now on. Call workbench_list until its "
        "state is running, then give the person the open link.",
        name=created.get("name") or body["name"],
    )


def _workbench_action(
    call: Call, web_base: str, instance_id, method: str, path: str, message: str, fallback: str,
    openable: bool = True, **kwargs,
) -> str:
    instance_id = _source_id(instance_id, "workbench", "instance_id")
    _write(
        call,
        method,
        f"{WORKBENCH}/instance/{path.format(id=instance_id)}",
        _CHANGE_MAY_HAVE_ARRIVED,
        fallback,
        **kwargs,
    )
    return _session_answer(web_base, "workbench", instance_id, message, openable=openable)


def stop_workbench(call: Call, web_base: str, instance_id) -> str:
    return _workbench_action(
        call, web_base, instance_id, "GET", "stop/{id}",
        "The machine is stopping. Its disk is kept, and workbench_start resumes it.",
        "Could not stop that machine.",
    )


def start_workbench(call: Call, web_base: str, instance_id) -> str:
    return _workbench_action(
        call, web_base, instance_id, "GET", "start/{id}",
        "The machine is resuming and is billed again. Call workbench_list until its state is running.",
        "Could not start that machine.",
    )


def extend_workbench(call: Call, web_base: str, instance_id, minutes) -> str:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= _MAX_EXTEND_MINUTES:
        _fail(
            f"minutes must be a whole number from 1 to {_MAX_EXTEND_MINUTES} (10 days).",
            "invalid_argument",
        )
    return _workbench_action(
        call, web_base, instance_id, "POST", "{id}/set-timeout",
        f"The machine now stops on its own {minutes} minutes from now.",
        "Could not move that machine's auto stop.",
        data={"totalSeconds": minutes * 60},
    )


def edit_workbench(call: Call, web_base: str, instance_id, name, instance_type, disk_size_gb) -> str:
    body = {}
    if name is not None:
        body["name"] = name
    if instance_type is not None:
        body["instanceType"] = _instance_type(instance_type)
    if disk_size_gb is not None:
        body["diskSize"] = _positive(disk_size_gb, "disk_size_gb")
    if not body:
        _fail("Give at least one of name, instance_type or disk_size_gb.", "invalid_argument")
    return _workbench_action(
        call, web_base, instance_id, "PATCH", "{id}",
        "The machine was updated.",
        "Could not update that machine.",
        data=body,
    )


def _confirmed(flag, name: str, what: str):
    if flag is not True:
        _fail(
            f"{name} must be True. Show the person the session and what is lost, and get their "
            f"confirmation before you {what}.",
            "confirmation_required",
        )


def terminate_workbench(call: Call, web_base: str, instance_id, confirm_terminate) -> str:
    _confirmed(confirm_terminate, "confirm_terminate", "terminate it")
    return _workbench_action(
        call, web_base, instance_id, "GET", "terminate/{id}",
        "The machine was terminated: the cloud VM is deleted. Anything stored only on it is gone.",
        "Could not terminate that machine.",
        openable=False,
    )


def delete_workbench(call: Call, web_base: str, instance_id, confirm_delete) -> str:
    _confirmed(confirm_delete, "confirm_delete", "delete it")
    return _workbench_action(
        call, web_base, instance_id, "DELETE", "{id}",
        "The machine was deleted from your list.",
        "Could not delete that machine.",
        openable=False,
    )


# ---------------------------------------------------------------- shared server apps


def terminate_app(call: Call, web_base: str, instance_id, confirm_terminate) -> str:
    _confirmed(confirm_terminate, "confirm_terminate", "terminate it")
    instance_id = _source_id(instance_id, "shinyproxy-app", "instance_id")
    status, body = _write(
        call,
        "GET",
        f"{APP_TERMINATE}/{instance_id}",
        _APP_MAY_HAVE_ARRIVED,
        "Could not terminate that app.",
    )
    if status != 204:
        # The route answers 200 with a body when nothing was stopped.
        body = body if isinstance(body, dict) else {}
        outcome = str(body.get("status") or "").lower()
        if outcome == "warning":
            _fail(
                "No running app instance of yours has that id.",
                "not_found",
                http_status=status,
            )
        if outcome != "success":
            _fail(
                body.get("message") or "The shared server did not stop that app.",
                "terminate_failed",
                http_status=status,
            )
    return _session_answer(
        web_base,
        "shinyproxy-app",
        instance_id,
        "The app was terminated. Its unsaved work is gone; launch it again with launch_app if needed.",
        openable=False,
    )
