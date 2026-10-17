#!/usr/bin/env python3
"""
Foundry Connect client management for MCP server.

Credentials are passed via HTTP headers from mcp.json.
"""

import os
import threading
import time
from typing import Any, Dict, NamedTuple, Optional, Tuple
from urllib.parse import urlparse

import requests
from viafoundry.client import ViaFoundryClient
from .config import get_credentials, validate_credentials
from .log import get_logger, mask_token

logger = get_logger(__name__)

# Cache clients by hostname to avoid re-creating
_clients: Dict[str, ViaFoundryClient] = {}
_clients_lock = threading.Lock()


def _resolve_hostname(hostname: str) -> str:
    """
    Resolve hostname for Docker environment.
    
    When running inside Docker, 'localhost' doesn't reach the host machine.
    This translates localhost to host.docker.internal for proper connectivity.
    """
    # Check if running inside Docker (/.dockerenv exists)
    if os.path.exists('/.dockerenv'):
        # Replace localhost with host.docker.internal
        if '://localhost' in hostname:
            resolved = hostname.replace('://localhost', '://host.docker.internal')
            logger.debug(f"Docker environment detected, resolved {hostname} -> {resolved}")
            return resolved
        if '://127.0.0.1' in hostname:
            resolved = hostname.replace('://127.0.0.1', '://host.docker.internal')
            logger.debug(f"Docker environment detected, resolved {hostname} -> {resolved}")
            return resolved
    return hostname


def get_client() -> ViaFoundryClient:
    """
    Get or initialize the Foundry Connect client using credentials from request headers.

    Returns:
        Initialized ViaFoundryClient instance

    Raises:
        ValueError: If credentials are missing or invalid
    """
    hostname, token = get_credentials()

    if not hostname or not token:
        raise ValueError(
            "Missing credentials. Configure in mcp.json:\n"
            '{\n'
            '  "foundry": {\n'
            '    "url": "http://127.0.0.1:8705/mcp",\n'
            '    "headers": {\n'
            '      "X-Foundry-Connect-Hostname": "https://your-viafoundry.com",\n'
            '      "X-Foundry-Connect-Token": "your-token-here"\n'
            '    }\n'
            '  }\n'
            '}'
        )

    if not validate_credentials(hostname, token):
        raise ValueError(
            "Invalid credentials.\n"
            "- Hostname must start with http:// or https://\n"
            "- Token must start with 'via_mcp_'"
        )
    
    # Resolve localhost to host.docker.internal when running in Docker
    hostname = _resolve_hostname(hostname)

    # Return cached client if exists for this hostname/token combination
    cache_key = (hostname, token)
    
    with _clients_lock:
        if cache_key in _clients:
            return _clients[cache_key]

        # Create new client
        masked = mask_token(token)
        logger.info(f"Initializing Foundry Connect client for {hostname} (token: {masked})")
        client = ViaFoundryClient()
        client.configure_auth_token(hostname=hostname, token=token)
        logger.info(f"Foundry Connect client authenticated for {hostname}")

        # Cache it
        _clients[cache_key] = client
        return client


# A gateway in front of Foundry Connect answers these when the answer was lost
# on the way back, not because the request was refused.
_RETRYABLE_STATUSES = frozenset({502, 504})
_REQUEST_TIMEOUT_SECONDS = 60

# The request may have reached the server and the answer was lost. Retried.
_TRANSPORT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
# What requests raises for an address it cannot use. Checked against the
# configured address before anything is sent; raised while sending, it can only
# come from a redirect Location, so the request had already been sent.
_ADDRESS_ERRORS = (
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
    requests.exceptions.InvalidURL,
)


class RequestFailed(Exception):
    """
    A request that got no usable HTTP answer.

    `reason` is "connection_failed" when the request may have reached the
    server (`may_have_arrived` is True), or "invalid_hostname" when the
    configured address is unusable and nothing was sent. An unusable address
    met while sending (a redirect to one) is "connection_failed", because
    the request had already been sent.
    """

    def __init__(self, reason: str, message: str, may_have_arrived: bool):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.may_have_arrived = may_have_arrived


def request_json(
    via_client: ViaFoundryClient,
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    data: Optional[Dict] = None,
    headers: Optional[Dict[str, str]] = None,
    retry: bool = False,
    attempts: int = 3,
) -> Tuple[int, Any]:
    """
    Send one logical request and return (HTTP status, parsed body).

    The body is None when the response carries no JSON.

    Unlike the SDK's `call`, this sends extra headers (such as `Idempotency-Key`)
    and does not raise on an HTTP error status, so a caller can read the typed
    refusal the server put in the body.

    With `retry=True` a dropped connection, a timeout or a gateway 502/504 is
    retried up to `attempts` times with the same method, body and headers. Only
    ask for it on a read, or on a write the server deduplicates by a key the
    caller sends in `headers`.

    Raises RequestFailed when no usable HTTP answer came back: after the last
    transport error, at once when the configured address is not a usable URL,
    and at once for any other error of the requests library (an answer that
    could not be decoded, too many redirects), which is never retried.
    """
    answer = send_request(via_client, method, endpoint, params=params, data=data,
                          headers=headers, retry=retry, attempts=attempts)
    return answer.status, answer.body


class Answer(NamedTuple):
    """
    What `send_request` got back. `earlier_attempt_lost` is True when an
    earlier attempt of the same call was sent and its answer was lost (a
    transport error or a gateway 502/504), so that attempt may have reached
    the server even when the last answer is a refusal.
    """

    status: int
    body: Any
    earlier_attempt_lost: bool


def send_request(
    via_client: ViaFoundryClient,
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    data: Optional[Dict] = None,
    headers: Optional[Dict[str, str]] = None,
    retry: bool = False,
    attempts: int = 3,
) -> Answer:
    """
    `request_json`, also saying whether an earlier attempt was lost. A caller
    whose write must never be reported as refused when it may have landed
    (a job submit) reads `earlier_attempt_lost`.
    """
    url = f"{via_client.auth.hostname}{endpoint}"
    merged = dict(via_client.auth.get_headers() or {})
    if headers:
        merged.update(headers)

    _check_address(method, url, params)

    tries = attempts if retry else 1
    response = None
    for attempt in range(1, tries + 1):
        try:
            response = requests.request(
                method.upper(), url, params=params, json=data, headers=merged,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except _ADDRESS_ERRORS as e:
            # The configured address passed `_check_address`, so this comes from
            # a redirect Location (or an earlier attempt already used the
            # address): a request was sent and the server may have acted on it.
            # Not retried: the same redirect would fail the same way.
            raise RequestFailed(
                "connection_failed",
                f"Foundry Connect's answer could not be followed ({type(e).__name__}).",
                may_have_arrived=True,
            ) from e
        except _TRANSPORT_ERRORS as e:
            if attempt == tries:
                raise RequestFailed(
                    "connection_failed",
                    f"Foundry Connect could not be reached ({type(e).__name__}).",
                    may_have_arrived=True,
                ) from e
            logger.warning(f"{method} {endpoint} attempt {attempt} failed ({type(e).__name__}), retrying")
            time.sleep(0.5 * attempt)
            continue
        except requests.RequestException as e:
            # Raised after the request was sent (an answer whose body could not
            # be decoded, a redirect loop), so the server may have acted on it.
            # Not retried: the same request would very likely fail the same way.
            raise RequestFailed(
                "connection_failed",
                f"Foundry Connect's answer could not be read ({type(e).__name__}).",
                may_have_arrived=True,
            ) from e
        if response.status_code in _RETRYABLE_STATUSES and attempt < tries:
            logger.warning(f"{method} {endpoint} attempt {attempt} answered {response.status_code}, retrying")
            time.sleep(0.5 * attempt)
            continue
        break

    # A body that is not JSON (an HTML error page from a disabled route or a
    # gateway) is never shown to the caller as the server's message: it comes
    # back as None, so the caller falls back to its own plain sentence.
    try:
        body = response.json()
    except ValueError:
        logger.warning(f"{method} {endpoint} answered {response.status_code} with a body that is not JSON")
        body = None
    # Every attempt before the last one was retried because its answer was lost.
    return Answer(response.status_code, body, earlier_attempt_lost=attempt > 1)


def _check_address(method: str, url: str, params: Optional[Dict]) -> None:
    """
    Raise RequestFailed("invalid_hostname") when the configured address is not
    a URL requests can send to. Runs before anything is sent, so a failure here
    is the only address error that proves no request left this client.
    """
    try:
        prepared = requests.Request(method.upper(), url, params=params).prepare()
        scheme = urlparse(prepared.url).scheme.lower()
        if scheme not in ("http", "https"):
            raise requests.exceptions.InvalidSchema(f"No connection adapters were found for {scheme!r}")
    except _ADDRESS_ERRORS as e:
        raise RequestFailed(
            "invalid_hostname",
            "The Foundry Connect address this client is configured with is not a usable URL. "
            "Set the hostname to a full address such as https://foundry.example.org.",
            may_have_arrived=False,
        ) from e


def reset_clients():
    """Reset all cached client instances."""
    with _clients_lock:
        _clients.clear()
