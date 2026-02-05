"""
ViaFoundry MCP Server

A Model Context Protocol (MCP) server for ViaFoundry.
"""

__version__ = "1.2.0"

from .client import get_client, reset_clients
from .config import get_credentials, set_credentials

__all__ = [
    "get_client",
    "reset_clients",
    "get_credentials",
    "set_credentials",
]
