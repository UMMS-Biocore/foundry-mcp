#!/usr/bin/env python3
"""
Configuration for ViaFoundry MCP.

Hostname resolution based on how the MCP server is accessed:

Localhost URL (http://localhost:8000/mcp, http://127.0.0.1:8000/mcp):
  - X-ViaFoundry-Hostname header can be ANY value (development flexibility)
  - X-ViaFoundry-Token header is required

Production URL (https://mcp.example.com/mcp):
  - X-ViaFoundry-Hostname is LOCKED to the request URL (cannot be changed)
  - X-ViaFoundry-Token header is required

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

import logging
from contextvars import ContextVar
from typing import Optional, Tuple

from .utils import is_valid_mcp_token, MCP_TOKEN_PREFIX

logger = logging.getLogger('viafoundry-mcp')

# Context variables for request-scoped credentials
_hostname_var: ContextVar[Optional[str]] = ContextVar('viafoundry_hostname', default=None)
_token_var: ContextVar[Optional[str]] = ContextVar('viafoundry_token', default=None)

# Header names
HEADER_HOSTNAME = "x-viafoundry-hostname"
HEADER_TOKEN = "x-viafoundry-token"

# Localhost identifiers - requests from these hosts allow custom hostname header
LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0"})


def is_localhost_request(host: str) -> bool:
    """
    Check if the request is coming from a localhost URL.
    
    Args:
        host: The Host header value (may include port, e.g., "127.0.0.1:8000")
    
    Returns:
        True if localhost, False otherwise
    """
    if not host:
        return False
    # Strip port if present (e.g., "127.0.0.1:8000" -> "127.0.0.1")
    hostname = host.split(":")[0].lower()
    return hostname in LOCALHOST_HOSTS


def build_hostname_from_request(scheme: str, host: str, path: str = "") -> str:
    """
    Build ViaFoundry hostname from request components.
    Strips '/mcp' suffix from path if present.
    
    Args:
        scheme: "http" or "https"
        host: Host header value (may include port)
        path: Request path (e.g., "/mcp" or "/beta/mcp")
    
    Returns:
        Hostname URL with '/mcp' suffix trimmed.
        Examples:
          - "/mcp" -> "https://example.com"
          - "/beta/mcp" -> "https://example.com/beta"
    """
    # Strip '/mcp' suffix from path
    if path.endswith("/mcp"):
        path = path[:-4]  # Remove '/mcp' (4 chars)
    
    return f"{scheme}://{host}{path}"


def set_credentials(hostname: Optional[str], token: Optional[str]) -> None:
    """Set credentials in the current context (called by middleware)."""
    _hostname_var.set(hostname)
    _token_var.set(token)


def get_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Get credentials from the current context."""
    return _hostname_var.get(), _token_var.get()


def resolve_hostname(
    header_hostname: Optional[str],
    request_scheme: str,
    request_host: str,
    request_path: str = ""
) -> Tuple[Optional[str], bool]:
    """
    Resolve the ViaFoundry hostname based on the request URL.
    
    - Localhost requests: Allow any X-ViaFoundry-Hostname header value
    - Production requests: Lock hostname to request URL with '/mcp' suffix trimmed
    
    Args:
        header_hostname: Value from X-ViaFoundry-Hostname header
        request_scheme: "http" or "https"
        request_host: Host header value (e.g., "example.com:8000")
        request_path: Request path (e.g., "/mcp" or "/beta/mcp")
    
    Returns:
        Tuple of (resolved_hostname, is_localhost)
    
    Examples:
        - "https://mcp.example.com/mcp" -> "https://mcp.example.com"
        - "https://example.com/beta/mcp" -> "https://example.com/beta"
    """
    is_localhost = is_localhost_request(request_host)
    
    if is_localhost:
        # Development mode: allow custom hostname from header
        return header_hostname, True
    else:
        # Production mode: hostname is derived from request URL (with '/mcp' trimmed)
        derived_hostname = build_hostname_from_request(request_scheme, request_host, request_path)
        if header_hostname and header_hostname != derived_hostname:
            logger.warning(
                f"Ignoring X-ViaFoundry-Hostname header '{header_hostname}' - "
                f"production mode locks hostname to '{derived_hostname}'"
            )
        return derived_hostname, False


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
