#!/usr/bin/env python3
"""
ViaFoundry client management for MCP server.

Credentials are passed via HTTP headers from mcp.json.
"""

import logging
import threading
from typing import Dict
from viafoundry.client import ViaFoundryClient
from .config import get_credentials, validate_credentials

logger = logging.getLogger('viafoundry-mcp')

# Cache clients by hostname to avoid re-creating
_clients: Dict[str, ViaFoundryClient] = {}
_clients_lock = threading.Lock()


def get_client() -> ViaFoundryClient:
    """
    Get or initialize the ViaFoundry client using credentials from request headers.

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
            '  "viafoundry": {\n'
            '    "url": "http://127.0.0.1:8000/mcp",\n'
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

    # Return cached client if exists for this hostname/token combination
    cache_key = (hostname, token)
    
    with _clients_lock:
        if cache_key in _clients:
            return _clients[cache_key]

        # Create new client
        logger.info(f"Initializing ViaFoundry client for {hostname}")
        client = ViaFoundryClient()
        client.configure_auth_token(hostname=hostname, token=token)
        logger.info("ViaFoundry client authenticated")

        # Cache it
        _clients[cache_key] = client
        return client


def reset_clients():
    """Reset all cached client instances."""
    with _clients_lock:
        _clients.clear()
