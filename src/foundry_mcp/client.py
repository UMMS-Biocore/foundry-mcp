#!/usr/bin/env python3
"""
Foundry Connect client management for MCP server.

Credentials are passed via HTTP headers from mcp.json.
"""

import os
import threading
from typing import Dict
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
            '      "X-ViaFoundry-Hostname": "https://your-viafoundry.com",\n'
            '      "X-ViaFoundry-Token": "your-token-here"\n'
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


def reset_clients():
    """Reset all cached client instances."""
    with _clients_lock:
        _clients.clear()
