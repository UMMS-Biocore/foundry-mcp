"""Turn an update_process request into a port list that keeps stored port ids.

The server updates a stored port in place only when the request names it by
id. A port without an id is added as a new row, and every stored port the
request leaves out is deleted. A caller that sends ports without ids therefore
deletes and recreates every port on each save, which changes their ids and
drops the stored publish pattern of output ports.

This module matches each requested port to a stored port, first by id and then
by display name, and sends matched ports back with their stored id and with
stored values for any field the caller did not set. Ports the caller does not
send for a side it names are removed. A side the caller does not name at all
is sent back unchanged.
"""

import html

SIDES = {
    "inputParameters": ("inputs", "removeAllInputParameters"),
    "outputParameters": ("outputs", "removeAllOutputParameters"),
}

# GET field name -> PUT field name, for callers that echo a fetched port back.
_GET_TO_PUT = {
    "parameter_id": "parameterId",
    "sname": "displayName",
    "closure": "operatorContent",
    "reg_ex": "regEx",
}

# Catalog fields the GET response carries but the server refuses on a PUT.
_READ_ONLY = ("name", "file_type", "qualifier")


class PortMergeError(ValueError):
    """The requested ports cannot be matched to the stored ports safely."""


def _as_bool(value) -> bool:
    # The GET response stores "optional" as the string "true" or "".
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def stored_port_to_request(row: dict) -> dict:
    """Map a port from GET /api/v1/process/{id} to the PUT port shape."""
    port = {
        "id": int(row["id"]),
        "parameterId": int(row["parameter_id"]),
        # The server HTML-encodes displayName on the way in, so the stored
        # value has to be decoded before it is sent back, or it is encoded twice.
        "displayName": html.unescape(row.get("sname") or ""),
        "operator": row.get("operator") or "",
        "operatorContent": row.get("closure") or "",
        "optional": _as_bool(row.get("optional")),
        # An update that leaves test out blanks the stored value.
        "test": row.get("test") or "",
    }
    if row.get("reg_ex") is not None:
        port["regEx"] = row["reg_ex"]
    return port


def _normalize_requested_port(port) -> dict:
    if not isinstance(port, dict):
        raise PortMergeError(f"Each port must be an object, got {type(port).__name__}.")
    out = {}
    for key, value in port.items():
        if key in _READ_ONLY:
            continue
        put_key = _GET_TO_PUT.get(key, key)
        # An explicit PUT-style key wins over its GET-style twin.
        if put_key != key and put_key in port:
            continue
        out[put_key] = value
    if "optional" in out:
        out["optional"] = _as_bool(out["optional"])
    if out.get("id") is not None:
        try:
            out["id"] = int(out["id"])
        except (TypeError, ValueError):
            raise PortMergeError(f"Port id {out['id']!r} is not a number.")
    else:
        out.pop("id", None)
    return out


def merge_side(side: str, stored_rows: list, requested: list) -> tuple:
    """Match one side's requested ports to its stored ports.

    Returns the port list to send and a summary of what changes, keyed
    kept/updated/added/removed, each a list of display names.
    """
    if not isinstance(requested, list):
        raise PortMergeError(f"{side} must be a list of ports.")
    stored = [stored_port_to_request(row) for row in stored_rows or []]
    by_id = {port["id"]: port for port in stored}
    wanted = [_normalize_requested_port(port) for port in requested]

    matched = [None] * len(wanted)
    claimed = set()

    # Ids first, so a name match never takes a port another entry names by id.
    for i, port in enumerate(wanted):
        port_id = port.get("id")
        if port_id is None:
            continue
        if port_id not in by_id:
            raise PortMergeError(
                f"{side} names port id {port_id}, which is not stored on this "
                f"side of the process. Call get_process_details for the current "
                f"port ids, or leave the id out to add a new port."
            )
        if port_id in claimed:
            raise PortMergeError(f"{side} names port id {port_id} more than once.")
        claimed.add(port_id)
        matched[i] = port_id

    for i, port in enumerate(wanted):
        if matched[i] is not None or "displayName" not in port:
            continue
        name = html.unescape(str(port["displayName"]))
        candidates = [p for p in stored if p["id"] not in claimed and p["displayName"] == name]
        if len(candidates) > 1:
            raise PortMergeError(
                f"{side} has {len(candidates)} stored ports named {name!r}. "
                f"Give the port's id to say which one you mean."
            )
        if candidates:
            matched[i] = candidates[0]["id"]
            claimed.add(matched[i])

    ports = []
    changes = {"kept": [], "updated": [], "added": [], "removed": []}
    for port, port_id in zip(wanted, matched):
        if port_id is None:
            ports.append(port)
            changes["added"].append(port.get("displayName"))
            continue
        current = by_id[port_id]
        merged = {**current, **port, "id": port_id}
        ports.append(merged)
        changes["updated" if merged != current else "kept"].append(merged["displayName"])

    changes["removed"] = [p["displayName"] for p in stored if p["id"] not in claimed]
    return ports, changes


def build_update_body(process_data: dict, stored_process: dict) -> tuple:
    """Return the PUT body with port ids resolved, and a per-side change summary.

    Raises PortMergeError when the request cannot be applied safely, before
    anything is sent to the server.
    """
    body = dict(process_data)
    summary = {}
    for side, (stored_key, remove_all_flag) in SIDES.items():
        stored_rows = stored_process.get(stored_key) or []
        if side not in body:
            # The caller did not touch this side, so send it back as stored.
            body[side] = [stored_port_to_request(row) for row in stored_rows]
            summary[side] = {
                "kept": [p["displayName"] for p in body[side]],
                "updated": [], "added": [], "removed": [],
            }
            continue
        ports, changes = merge_side(side, stored_rows, body[side])
        if not ports and stored_rows and body.get(remove_all_flag) is not True:
            raise PortMergeError(
                f"{side} is empty, which would delete every stored port on that "
                f"side ({', '.join(changes['removed'])}). Set {remove_all_flag}: "
                f"true to confirm, or leave {side} out to keep the ports as they are."
            )
        body[side] = ports
        summary[side] = changes
    return body, summary
