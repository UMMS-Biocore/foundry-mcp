"""
Cluster app launch tools: launch, list, check and stop an app (marimo by
default) on one of the caller's run environments.

The tools talk to the `/api/v1/hpc-app-launches` routes. The functions here
take the HTTP call and the Foundry Connect web address as arguments, so the
server module stays the only place that knows how a client is made.
"""

import base64
import binascii
import json
import re
import uuid
from typing import Callable, List, Tuple

from mcp.server.fastmcp import Image
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


# ---------------------------------------------------------------- notebook operations

# Same shapes as the backend validates (common/src/api/hpcApp.ts).
_OP_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_NOTEBOOK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.py")
OPERATIONS = (
    "execute", "cells", "create_cell", "edit_cell", "run_cell", "delete_cell", "status", "interrupt",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")

# One POST or GET waits this long on the server, under the 60 s gateway and client limits.
_SERVER_WAIT_SECONDS = 25
_MAX_STATUS_WAIT_SECONDS = 50
_MAX_TIMEOUT_SECONDS = 3600
# The whole app_execute call stays under the same 60 s limits; longer waits go
# through app_operation_status, one bounded call at a time.
_MAX_TOTAL_WAIT_SECONDS = 50

_OUTCOME_UNKNOWN_NEXT = (
    "The result of this operation was lost, so it may or may not have run. Do not run it again. "
    "Call app_execute with op=cells to read the notebook as it is now, check whether the change "
    "landed, and ask the person before doing anything else."
)
_CONFLICT_NEXT = (
    "The cell changed since you last read it, or the person is editing it. Nothing of yours was "
    "written. Show the person their cell from the answer, ask how to proceed, then read the cells "
    "again before editing."
)
_ANSWER_KEYS = (
    "op", "state", "reason", "truncated", "outputBytes", "events", "result", "replayed",
    "outputRetained", "sessionRestarted",
)


def _running_next(op_id: str) -> str:
    return (
        f"Still running in the background. Call app_operation_status with launch_id and "
        f"op_id {op_id} to wait for the result. Do not start the same operation again."
    )


def _op_fail(error: str, reason: str, op_id: str, **extra):
    _fail(error, reason, op_id=op_id, **extra)


def _op_refused(body, status: int, op_id: str, fallback_reason: str, fallback_message: str):
    body = body if isinstance(body, dict) else {}
    if status == 403 and body.get("error") == "execution_not_granted":
        _op_fail(_RECONNECT, "execution_not_granted", op_id, http_status=status)
    reason = body.get("reason") or body.get("error") or fallback_reason
    message = body.get("detail") or body.get("message") or fallback_message
    _op_fail(message, reason, op_id, http_status=status)


# The declared media type of image content must match its bytes, or a model API
# refuses the whole answer. The format is read from the bytes, never from the
# output's type: a cell thumbnail is always JPEG whatever the original figure was.
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _image_format(raw: bytes):
    for signature, name in _IMAGE_SIGNATURES:
        if raw.startswith(signature):
            return name
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


_DATA_URL = re.compile(r"data:[\w.+/-]*(?:;[\w.+=-]+)*;base64,", re.IGNORECASE)


def _decode(data):
    """Bytes of bare base64 or a base64 data URL, or None when it is neither."""
    if not isinstance(data, str) or not data:
        return None
    prefix = _DATA_URL.match(data[:256])
    if prefix:
        data = data[prefix.end():]
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return None


def _take_images(node, images: list):
    """Replace each image payload in the answer with a marker and collect it as Image content."""
    if isinstance(node, list):
        for item in node:
            _take_images(item, images)
        return
    if not isinstance(node, dict):
        return
    mimetype = node.get("mimetype") or node.get("type")
    is_image_event = node.get("t") == "image" or str(mimetype or "").startswith("image/")
    for field in ("data", "thumbnail"):
        if field in node and (is_image_event or field == "thumbnail"):
            raw = _decode(node[field])
            image_format = _image_format(raw) if raw is not None else None
            if image_format is not None:
                images.append(Image(data=raw, format=image_format))
                node[field] = "[image attached]"
    for value in node.values():
        _take_images(value, images)


def _operation_answer(body, op_id: str):
    """The tool answer for an ended or running operation: JSON text, plus images when there are any."""
    body = body if isinstance(body, dict) else {}
    state = body.get("state")
    answer = {"op_id": op_id}
    if state in ("reserved", "running", None):
        answer.update({"state": "running", "next_step": _running_next(op_id)})
        return json.dumps(answer, indent=2)
    answer.update({name: body[name] for name in _ANSWER_KEYS if name in body})
    if state == "outcome_unknown":
        answer["next_step"] = _OUTCOME_UNKNOWN_NEXT
    elif state == "conflict":
        answer["next_step"] = _CONFLICT_NEXT
    images: list = []
    _take_images(answer, images)
    text = json.dumps(answer, indent=2)
    return [text, *images] if images else text


def _operation_args(op, code, cell_id, expected_code_sha256, after, run) -> dict:
    def need(ok: bool, message: str):
        if not ok:
            _fail(message, "invalid_argument")

    has_code = isinstance(code, str) and code != ""
    has_cell = isinstance(cell_id, str) and cell_id.strip() != ""
    has_sha = isinstance(expected_code_sha256, str) and bool(_SHA256.fullmatch(expected_code_sha256))
    if op == "execute":
        need(has_code, "execute needs code.")
        return {"code": code}
    if op == "create_cell":
        need(has_code, "create_cell needs code.")
        args = {"code": code, "run": bool(run)}
        if isinstance(after, str) and after.strip():
            args["after"] = after.strip()
        return args
    if op == "edit_cell":
        need(has_cell and has_code, "edit_cell needs cell_id and code.")
        need(has_sha, "edit_cell needs expected_code_sha256, the codeSha256 from the last cells read.")
        return {"cellId": cell_id.strip(), "code": code, "expectedCodeSha256": expected_code_sha256, "run": bool(run)}
    if op == "run_cell":
        need(has_cell, "run_cell needs cell_id.")
        return {"cellId": cell_id.strip()}
    if op == "delete_cell":
        need(has_cell, "delete_cell needs cell_id.")
        need(has_sha, "delete_cell needs expected_code_sha256, the codeSha256 from the last cells read.")
        return {"cellId": cell_id.strip(), "expectedCodeSha256": expected_code_sha256}
    return {}


def _int_between(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or value > high:
        _fail(f"{name} must be a whole number from {low} to {high}.", "invalid_argument")
    return value


def execute(
    call: Call,
    launch_id: str,
    op: str,
    op_id: str,
    notebook: str,
    code: str,
    cell_id: str,
    expected_code_sha256: str,
    after: str,
    run: bool,
    timeout_seconds: int,
    wait_seconds: int,
):
    launch_id = _check_launch_id(launch_id)
    if op not in OPERATIONS:
        _fail(f"op must be one of: {', '.join(OPERATIONS)}.", "invalid_argument")
    op_id = op_id.strip() if isinstance(op_id, str) else ""
    if not op_id:
        op_id = uuid.uuid4().hex
    elif not _OP_ID.fullmatch(op_id):
        _fail("op_id must be 8 to 64 letters, digits, underscores or hyphens.", "invalid_argument")
    notebook = notebook or "notebook.py"
    if not isinstance(notebook, str) or not _NOTEBOOK.fullmatch(notebook):
        _fail("notebook must be a .py file name directly under /workspace.", "invalid_argument")
    timeout_seconds = _int_between(timeout_seconds, "timeout_seconds", 1, _MAX_TIMEOUT_SECONDS)
    wait_seconds = _int_between(wait_seconds, "wait_seconds", 0, _MAX_TOTAL_WAIT_SECONDS)
    args = _operation_args(op, code, cell_id, expected_code_sha256, after, run)

    body = {
        "opId": op_id,
        "op": op,
        "notebook": notebook,
        "args": args,
        "waitSeconds": min(_SERVER_WAIT_SECONDS, wait_seconds),
        "timeoutSeconds": timeout_seconds,
    }
    lost = (
        f"The answer was lost. The operation may have started. Call app_operation_status with "
        f"op_id {op_id} to read its result. Do not start it again."
    )
    endpoint = f"{LAUNCHES}/{launch_id}/operations"
    # Safe to retry: the server keys the operation on op_id and replays the stored record.
    try:
        status, answer = call("POST", endpoint, data=body, retry=True)
    except RequestFailed as e:
        if e.may_have_arrived:
            _op_fail(f"{e.message} {lost}", "outcome_unknown", op_id)
        _op_fail(e.message, e.reason, op_id)
    if status in _GATEWAY_STATUSES or (status >= 500 and not (isinstance(answer, dict) and answer.get("reason"))):
        _op_fail(f"Foundry Connect did not answer (HTTP {status}). {lost}", "outcome_unknown", op_id, http_status=status)
    if status == 200:
        return _operation_answer(answer, op_id)
    if status != 202:
        _op_refused(answer, status, op_id, "operation_refused", "The operation was refused.")

    waited = min(_SERVER_WAIT_SECONDS, wait_seconds)
    while waited < wait_seconds:
        step = min(_SERVER_WAIT_SECONDS, wait_seconds - waited)
        polled = _poll(call, launch_id, op_id, step)
        if polled is not None:
            return polled
        waited += step
    return _operation_answer({"state": "running"}, op_id)


def _status_unread_next(op_id: str) -> str:
    return (
        f"Its state could not be read, so it may still be running or may have ended. Call "
        f"app_operation_status with launch_id and op_id {op_id} to read it. Do not start the "
        f"same operation again."
    )


def _poll(call: Call, launch_id: str, op_id: str, wait: int):
    """One wait on an operation: its answer when it ended, None while it still runs.

    A lost answer on this read says nothing about the operation itself, which is
    keyed by op_id and may still run, so it is never reported as a refusal.
    """
    try:
        status, body = call(
            "GET", f"{LAUNCHES}/{launch_id}/operations/{op_id}", params={"waitSeconds": wait}, retry=True
        )
    except RequestFailed as e:
        _op_fail(f"{e.message} {_status_unread_next(op_id)}", e.reason, op_id)
    typed = isinstance(body, dict) and bool(body.get("reason"))
    if status in _GATEWAY_STATUSES or (status >= 500 and not typed):
        _op_fail(
            f"Foundry Connect did not answer (HTTP {status}). {_status_unread_next(op_id)}",
            "operation_status_unavailable",
            op_id,
            http_status=status,
        )
    if status == 202:
        return None
    if status == 200:
        return _operation_answer(body, op_id)
    _op_refused(body, status, op_id, "operation_status_failed", "Could not read that operation.")


def operation_status(call: Call, launch_id: str, op_id: str, wait_seconds: int):
    launch_id = _check_launch_id(launch_id)
    op_id = op_id.strip() if isinstance(op_id, str) else ""
    if not _OP_ID.fullmatch(op_id):
        _fail("op_id must be the op_id app_execute returned.", "invalid_argument")
    wait_seconds = _int_between(wait_seconds, "wait_seconds", 0, _MAX_STATUS_WAIT_SECONDS)
    polled = _poll(call, launch_id, op_id, wait_seconds)
    return polled if polled is not None else _operation_answer({"state": "running"}, op_id)
