#!/usr/bin/env python3
"""
ViaFoundry client management for MCP server.
"""

import logging
from typing import Optional
from viafoundry.client import ViaFoundryClient
from .config import get_credentials, validate_credentials

logger = logging.getLogger('viafoundry-mcp')

# Global client instance
_client: Optional[ViaFoundryClient] = None


def get_client(interactive: bool = False) -> ViaFoundryClient:
    """
    Get or initialize the ViaFoundry client.

    Args:
        interactive: If True, prompt user for credentials if not found

    Returns:
        Initialized ViaFoundryClient instance

    Raises:
        ValueError: If credentials are missing or invalid
    """
    global _client

    if _client is None:
        # Get credentials from environment or prompt user
        hostname, username, password = get_credentials(interactive=interactive)

        # Validate credentials
        if not all([hostname, username, password]):
            raise ValueError(
                "Missing required credentials. "
                "Run 'viafoundry-mcp-setup' to configure, or set environment variables:\n"
                "  VIAFOUNDRY_HOSTNAME, VIAFOUNDRY_USERNAME, VIAFOUNDRY_PASSWORD"
            )

        if not validate_credentials(hostname, username, password):
            raise ValueError(
                "Invalid credentials. "
                "Hostname must start with http:// or https://"
            )

        # Initialize client
        logger.info(f"Initializing ViaFoundry client for {hostname}")
        _client = ViaFoundryClient()

        # Configure authentication
        _client.configure_auth(
            hostname=hostname,
            username=username,
            password=password
        )
        logger.info("ViaFoundry client authenticated successfully")

    return _client


def reset_client():
    """Reset the global client instance. Useful for testing."""
    global _client
    _client = None
