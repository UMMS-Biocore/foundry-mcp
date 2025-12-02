#!/usr/bin/env python3
"""
Configuration for ViaFoundry MCP.

Credentials are passed via HTTP headers from mcp.json:
  - X-ViaFoundry-Hostname: Your ViaFoundry instance URL
  - X-ViaFoundry-Token: Your Personal Access Token

Configure in ~/.cursor/mcp.json:
{
  "viafoundry": {
    "url": "http://127.0.0.1:8000/mcp",
    "headers": {
      "X-ViaFoundry-Hostname": "https://your-viafoundry.com",
      "X-ViaFoundry-Token": "your-token-here"
    }
  }
}
"""

import logging
from contextvars import ContextVar

logger = logging.getLogger('viafoundry-mcp')

# Context variables for request-scoped credentials
_hostname_var: ContextVar[str | None] = ContextVar('viafoundry_hostname', default=None)
_token_var: ContextVar[str | None] = ContextVar('viafoundry_token', default=None)

# Header names
HEADER_HOSTNAME = "x-viafoundry-hostname"
HEADER_TOKEN = "x-viafoundry-token"


def set_credentials(hostname: str | None, token: str | None) -> None:
    """Set credentials in the current context (called by middleware)."""
    _hostname_var.set(hostname)
    _token_var.set(token)


def get_credentials() -> tuple[str | None, str | None]:
    """Get credentials from the current context."""
    return _hostname_var.get(), _token_var.get()


def validate_credentials(hostname: str | None, token: str | None) -> bool:
    """Validate that credentials are non-empty and properly formatted."""
    if not hostname or not token:
        return False
    if not (hostname.startswith("http://") or hostname.startswith("https://")):
        logger.error(f"Invalid hostname: {hostname} (must start with http:// or https://)")
        return False
    return True
