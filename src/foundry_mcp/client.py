#!/usr/bin/env python3
"""
Foundry Connect client management for MCP server.

Credentials are passed via HTTP headers from mcp.json.
"""

import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

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
    caller sends in `headers`. The last transport error is raised.
    """
    url = f"{via_client.auth.hostname}{endpoint}"
    merged = dict(via_client.auth.get_headers() or {})
    if headers:
        merged.update(headers)

    tries = attempts if retry else 1
    response = None
    for attempt in range(1, tries + 1):
        try:
            response = requests.request(
                method.upper(), url, params=params, json=data, headers=merged,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == tries:
                raise
            logger.warning(f"{method} {endpoint} attempt {attempt} failed ({type(e).__name__}), retrying")
            time.sleep(0.5 * attempt)
            continue
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
    return response.status_code, body


def reset_clients():
    """Reset all cached client instances."""
    with _clients_lock:
        _clients.clear()
