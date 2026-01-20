#!/usr/bin/env python3
"""
Configuration for ViaFoundry MCP.

Credentials are passed via HTTP headers:
  - X-ViaFoundry-Hostname: Your ViaFoundry instance URL (required)
  - X-ViaFoundry-Token: Your Personal Access Token (required)

Configure in ~/.cursor/mcp.json:
{
  "viafoundry": {
    "url": "http://127.0.0.1:8000/mcp",
    "headers": {
      "X-ViaFoundry-Hostname": "https://your-viafoundry.com",
      "X-ViaFoundry-Token": "via_mcp_your-token-here"
    }
  }
}
"""

from contextvars import ContextVar
from typing import Optional, Tuple

from .utils import is_valid_mcp_token, MCP_TOKEN_PREFIX
from .log import get_logger

logger = get_logger(__name__)

# Context variables for request-scoped credentials
_hostname_var: ContextVar[Optional[str]] = ContextVar('viafoundry_hostname', default=None)
_token_var: ContextVar[Optional[str]] = ContextVar('viafoundry_token', default=None)

# Header names
HEADER_HOSTNAME = "x-viafoundry-hostname"
HEADER_TOKEN = "x-viafoundry-token"


def set_credentials(hostname: Optional[str], token: Optional[str]) -> None:
    """Set credentials in the current context (called by middleware)."""
    _hostname_var.set(hostname)
    _token_var.set(token)


def get_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Get credentials from the current context."""
    return _hostname_var.get(), _token_var.get()


def validate_credentials(hostname: Optional[str], token: Optional[str]) -> bool:
    """Validate that credentials are non-empty and properly formatted."""
    if not hostname or not token:
        return False
    
    # Check protocol and extract host part
    if hostname.startswith("https://"):
        host_part = hostname[8:]  # Remove "https://"
    elif hostname.startswith("http://"):
        host_part = hostname[7:]  # Remove "http://"
    else:
        logger.error(f"Invalid hostname: {hostname} (must start with http:// or https://)")
        return False
    
    # Ensure there's actual hostname content (not empty or just a path)
    if not host_part or host_part.startswith("/"):
        logger.error(f"Invalid hostname: {hostname} (missing host)")
        return False
    
    if not is_valid_mcp_token(token):
        logger.error(f"Invalid token: must start with '{MCP_TOKEN_PREFIX}'")
        return False
    
    return True
