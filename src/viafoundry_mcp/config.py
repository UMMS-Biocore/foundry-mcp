#!/usr/bin/env python3
"""
Configuration for ViaFoundry MCP.

Credentials are passed via HTTP headers:
  - X-ViaFoundry-Hostname: Your ViaFoundry instance URL (required in open mode)
  - X-ViaFoundry-Token: Your Personal Access Token (required)

Security Modes:
  - Open Mode (default): Client can specify X-ViaFoundry-Hostname header.
    Used for localhost/development deployments.
  
  - Fixed Hostname Mode: Server uses a configured hostname, ignoring client header.
    Enabled by setting FRONTEND_HOSTNAME environment variable (from ViaFoundry .env).
    Constructs URL from: FRONTEND_PROTOCOL (or https) + FRONTEND_HOSTNAME + FRONTEND_PATH_PREFIX
    Used for production deployments to prevent open proxy abuse.

Configure in ~/.cursor/mcp.json:
{
  "viafoundry": {
    "url": "http://127.0.0.1:8705/mcp",
    "headers": {
      "X-ViaFoundry-Hostname": "https://your-viafoundry.com",
      "X-ViaFoundry-Token": "via_mcp_your-token-here"
    }
  }
}
"""

import os
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


# Localhost-like hostnames that should trigger open mode (not fixed hostname mode)
LOCALHOST_HOSTNAMES = frozenset([
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "[::1]",
])


def _is_localhost(hostname: str) -> bool:
    """Check if hostname is a localhost-like address."""
    hostname_lower = hostname.lower().strip()
    return hostname_lower in LOCALHOST_HOSTNAMES


def get_fixed_hostname() -> Optional[str]:
    """
    Get fixed hostname from environment for production deployments.
    
    When a fixed hostname is configured, the server ignores client-provided
    X-ViaFoundry-Hostname headers, preventing open proxy abuse.
    
    Constructed from ViaFoundry environment variables:
      - FRONTEND_HOSTNAME: Required to enable fixed hostname mode
      - FRONTEND_PROTOCOL: Protocol (defaults to 'https' if not set)
      - FRONTEND_PATH_PREFIX: Optional path prefix (e.g., '/beta')
    
    Note: Localhost-like hostnames (localhost, 127.0.0.1, 0.0.0.0, etc.)
    are treated as open mode, since local deployments don't need the
    security restriction.
    
    Example .env configuration:
      FRONTEND_PROTOCOL=https
      FRONTEND_HOSTNAME="dev-playground.infra.viafoundry.net"
      FRONTEND_PATH_PREFIX="/beta"
    
    Results in: https://dev-playground.infra.viafoundry.net/beta
    
    Returns:
        The fixed hostname URL, or None if not configured or localhost (open mode).
    """
    # FRONTEND_HOSTNAME is the trigger for fixed hostname mode
    hostname = os.environ.get("FRONTEND_HOSTNAME")
    if not hostname:
        # No fixed hostname configured - open mode
        return None
    
    # Strip quotes if present (common in .env files)
    hostname = hostname.strip('"').strip("'")
    
    # Localhost-like hostnames don't need fixed hostname protection
    if _is_localhost(hostname):
        logger.debug(
            f"FRONTEND_HOSTNAME is localhost-like ({hostname}), using open mode"
        )
        return None
    
    # Protocol defaults to https if not specified
    protocol = os.environ.get("FRONTEND_PROTOCOL", "https")
    protocol = protocol.strip('"').strip("'")
    
    # Path prefix is optional
    path_prefix = os.environ.get("FRONTEND_PATH_PREFIX", "")
    path_prefix = path_prefix.strip('"').strip("'")
    
    # Construct the full URL
    constructed = f"{protocol}://{hostname}{path_prefix}"
    logger.debug(f"Constructed fixed hostname from FRONTEND_* vars: {constructed}")
    return constructed
