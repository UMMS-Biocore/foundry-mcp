"""
ViaFoundry MCP Server

A Model Context Protocol (MCP) server for ViaFoundry.
"""

__version__ = "1.0.0"

from .server import main, app
from .client import get_client, reset_client
from .config import setup_command, get_credentials

__all__ = [
    "main",
    "app",
    "get_client",
    "reset_client",
    "setup_command",
    "get_credentials",
]
