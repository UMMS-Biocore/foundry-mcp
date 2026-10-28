#!/usr/bin/env python3
"""
Foundry Connect MCP HTTP Server

Credentials are configured in mcp.json via headers:
{
  "foundry": {
    "url": "http://127.0.0.1:8705/mcp",
    "headers": {
      "X-Foundry-Connect-Hostname": "https://your-viafoundry.com",
      "X-Foundry-Connect-Token": "your-token-here"
    }
  }
}

Run with: python -m foundry_mcp.server --port 8705
"""

import json
import logging
import argparse
import base64
import html
import re
import shlex
import tempfile
import unicodedata
import os
import uuid
import requests
from typing import Annotated, Optional

from pydantic import Strict

from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

# Import from our modules
from .client import RequestFailed, get_client, request_json, send_request
from .config import (
    set_credentials, validate_credentials, get_fixed_hostname, get_credentials,
    HEADER_HOSTNAME, HEADER_TOKEN, HEADER_HOSTNAME_NEW, HEADER_TOKEN_NEW
)
from .utils import serialize_response, MCP_TOKEN_PREFIX, remove_none, envelope, tail_text
from .log import get_logger, get_uvicorn_log_config, mask_token
from .process_ports import PortMergeError, build_update_body
from . import accessions as _accessions


# Get logger for this module
logger = get_logger(__name__)

# Maximum file size for upload/download via base64 (200MB)
# Base64 encoding increases size by ~33%, so 200MB file becomes ~267MB in transport
MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024  # 200MB

# Largest file download_file returns inline as base64. The whole tool result
# has to fit in one MCP response and in the model's context, and clients fail
# well below MAX_FILE_SIZE_BYTES (seen at 15 to 20MB). Above this, the tool
# returns a download link instead of the bytes.
INLINE_DOWNLOAD_MAX_BYTES = 5 * 1024 * 1024  # 5MB


class CredentialsMiddleware:
    """
    Middleware that extracts Foundry Connect credentials from request headers,
    validates them, and stores them in context variables for use by tool handlers.
    
    Security Modes:
      - Fixed Hostname Mode (production): When fixed_hostname is set, the server
        uses that hostname for all requests, ignoring client X-Foundry-Connect-Hostname
        headers. This prevents the server from being used as an open proxy.
      
      - Open Mode (development): When fixed_hostname is None, the server accepts
        X-Foundry-Connect-Hostname from clients, allowing connection to any Foundry Connect
        instance. Only safe for localhost deployments.
    
    Returns 401 Unauthorized if credentials are missing or invalid.
    """
    
    def __init__(self, app: ASGIApp, fixed_hostname: str = None):
        self.app = app
        self.fixed_hostname = fixed_hostname
    
    def _get_client_ip(self, scope: Scope) -> str:
        """Extract client IP from scope, checking proxy headers first."""
        headers = dict(scope.get("headers", []))
        
        # Check X-Real-IP first (set by nginx)
        real_ip = headers.get(b"x-real-ip", b"").decode()
        if real_ip:
            return real_ip
        
        # Check X-Forwarded-For (first IP in chain is the client)
        forwarded_for = headers.get(b"x-forwarded-for", b"").decode()
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        # Fall back to direct client connection
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _get_host(self, scope: Scope) -> str:
        """Extract the request Host header from scope (used for OAuth discovery hints)."""
        headers = dict(scope.get("headers", []))
        return headers.get(b"host", b"").decode()

    def _log_with_context(self, level: int, message: str, scope: Scope, 
                          hostname: str = None, token: str = None):
        """Log message with request context (client IP, hostname, masked token)."""
        extra = {
            "client_ip": self._get_client_ip(scope),
        }
        if hostname:
            extra["hostname"] = hostname
        if token:
            extra["token"] = mask_token(token)
        logger.log(level, message, extra=extra)
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            # Skip credential check for OPTIONS requests (CORS preflight)
            method = scope.get("method", "")
            if method == "OPTIONS":
                await self.app(scope, receive, send)
                return
            
            # Extract headers (they're stored as list of tuples)
            headers = dict(scope.get("headers", []))

            # Get token from headers (always required from client).
            # Prefer the explicit X-Foundry-Connect-Token header (IDE/manual setup);
            # fall back to an OAuth-style "Authorization: Bearer <token>" header
            # so clients that discovered credentials via the OAuth flow work too.
            token = (headers.get(HEADER_TOKEN_NEW.encode(), b"").decode()
                     or headers.get(HEADER_TOKEN.encode(), b"").decode())
            if not token:
                auth_header = headers.get(b"authorization", b"").decode()
                if auth_header.lower().startswith("bearer "):
                    token = auth_header[len("bearer "):].strip()

            # Determine hostname based on security mode
            if self.fixed_hostname:
                # Fixed hostname mode (production): ignore client header
                hostname = self.fixed_hostname
                client_hostname = (headers.get(HEADER_HOSTNAME_NEW.encode(), b"").decode()
                                   or headers.get(HEADER_HOSTNAME.encode(), b"").decode())
                if client_hostname and client_hostname != self.fixed_hostname:
                    self._log_with_context(
                        logging.DEBUG,
                        f"Ignoring client hostname '{client_hostname}', using fixed: {hostname}",
                        scope,
                        hostname,
                        token
                    )
            else:
                # Open mode (development): use client-provided header, falling
                # back to a hostname derived from the request Host (+ X-Forwarded-Proto)
                # so OAuth clients that only send Authorization: Bearer still work.
                hostname = (headers.get(HEADER_HOSTNAME_NEW.encode(), b"").decode()
                            or headers.get(HEADER_HOSTNAME.encode(), b"").decode())
                if not hostname:
                    host = headers.get(b"host", b"").decode()
                    if host:
                        proto = headers.get(b"x-forwarded-proto", b"https").decode()
                        hostname = f"{proto}://{host}"
            
            # Validate credentials
            if not validate_credentials(hostname, token):
                # Return 401 Unauthorized response
                client_ip = self._get_client_ip(scope)
                masked = mask_token(token)
                self._log_with_context(
                    logging.WARNING,
                    f"Invalid or missing credentials from {client_ip} (token: {masked})",
                    scope,
                    hostname or "none",
                    token
                )
                await self._send_unauthorized_response(send, hostname, token, scope)
                return
            
            # Set validated credentials in context for this request
            set_credentials(hostname, token)
            masked = mask_token(token)
            self._log_with_context(
                logging.DEBUG,
                f"Credentials validated for {hostname} (token: {masked})",
                scope,
                hostname,
                token
            )
        
        await self.app(scope, receive, send)
    
    async def _send_unauthorized_response(self, send: Send, hostname: str, token: str, scope: Scope) -> None:
        """Send a 401 Unauthorized response with details about what's missing."""
        # In fixed hostname mode, we only need the token from client
        if self.fixed_hostname:
            if not token:
                detail = "Missing X-Foundry-Connect-Token header."
            elif not token.startswith(MCP_TOKEN_PREFIX):
                detail = f"Invalid token format: X-Foundry-Connect-Token must start with '{MCP_TOKEN_PREFIX}'"
            else:
                detail = "Invalid credentials."
            help_msg = "Configure X-Foundry-Connect-Token in mcp.json headers."
        else:
            # Open mode - need both hostname and token from client
            if not hostname and not token:
                detail = "Missing credentials. Provide X-Foundry-Connect-Hostname and X-Foundry-Connect-Token headers."
            elif not hostname:
                detail = "Missing X-Foundry-Connect-Hostname header."
            elif not token:
                detail = "Missing X-Foundry-Connect-Token header."
            elif not (hostname.startswith("http://") or hostname.startswith("https://")):
                detail = f"Invalid hostname format: '{hostname}'. Must start with http:// or https://"
            elif not token.startswith(MCP_TOKEN_PREFIX):
                detail = f"Invalid token format: X-Foundry-Connect-Token must start with '{MCP_TOKEN_PREFIX}'"
            else:
                detail = "Invalid credentials."
            help_msg = "Configure credentials in mcp.json headers: X-Foundry-Connect-Hostname and X-Foundry-Connect-Token"
        
        body = json.dumps({
            "error": "Unauthorized",
            "detail": detail,
            "help": help_msg
        }).encode("utf-8")

        response_headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]

        # Advertise the OAuth protected-resource metadata so clients (e.g. Claude)
        # can auto-discover the authorization flow. Best-effort: derived from the
        # request Host, and omitted gracefully if Host is unavailable.
        host = self._get_host(scope)
        if host:
            resource_metadata = f'Bearer resource_metadata="https://{host}/.well-known/oauth-protected-resource"'
            response_headers.append((b"www-authenticate", resource_metadata.encode()))

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": response_headers,
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


def create_mcp_server(stateless: bool = False) -> FastMCP:
    """
    Create and configure the FastMCP server.
    
    Args:
        stateless: If True, run in stateless mode (no session persistence).
                   Better for serverless/Lambda deployments.
    """
    # DNS rebinding protection is disabled because this server runs behind a
    # reverse proxy (nginx/Apache) and handles its own authentication via
    # CredentialsMiddleware (X-Foundry-Connect-Token). The proxy may forward any
    # external Host header (e.g. mcp.viafoundry.com, *.infra.viafoundry.net),
    # which would be rejected by the SDK's default allowed_hosts list.
    # See: https://github.com/modelcontextprotocol/python-sdk/issues/1798
    return FastMCP(
        "foundry-mcp",
        stateless_http=stateless,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )


# Initialize the FastMCP server (stateless mode for proxy/tunnel compatibility)
mcp = create_mcp_server(stateless=True)


# ============================================================================
# Report Management Tools
# ============================================================================

@mcp.tool()
def fetch_report(report_id: str) -> str:
    """
    Fetch report data by report ID (same as run ID). Returns JSON data containing
    all processes, files, and metadata for the specified report.
    You can get the report_id from list_runs or get_run tools.
    """
    try:
        via_client = get_client()
        logger.info(f"Fetching report {report_id}")
        report_data = via_client.reports.fetch_report_data(report_id)
        result = serialize_response(report_data)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error fetching report: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_processes(report_id: str) -> str:
    """
    List all unique processes in a report. Returns a list of process names
    that have generated output in the specified report.
    Note: report_id is the same as run_id.
    """
    try:
        via_client = get_client()
        logger.info(f"Listing processes for report {report_id}")
        report_data = via_client.reports.fetch_report_data(report_id)
        processes = via_client.reports.get_process_names(report_data)
        return json.dumps({"processes": processes}, indent=2)
    except Exception as e:
        logger.error(f"Error listing processes: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_files(report_id: str, process_name: str = None) -> str:
    """
    List files in a report. If process_name is provided, lists files for that
    specific process. Otherwise, lists all files across all processes in the report.
    Returns file metadata including file paths, sizes, and extensions.
    Note: report_id is the same as run_id.
    """
    try:
        via_client = get_client()
        logger.info(f"Listing files for report {report_id}" +
                   (f", process {process_name}" if process_name else " (all processes)"))
        
        report_data = via_client.reports.fetch_report_data(report_id)
        
        if process_name:
            files_df = via_client.reports.get_file_names(report_data, process_name)
        else:
            files_df = via_client.reports.get_all_files(report_data)
        
        files_dict = files_df.to_dict(orient='records')
        return json.dumps({"files": files_dict}, indent=2)
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return json.dumps({"error": str(e)})


_ROUTE_PATH_ATTEMPT = re.compile(
    r"^/report-resources/("
    r"[0-9a-fA-F]{32}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r")/pubweb/"
)
_DOWNLOAD_LINK_PATH = re.compile(r"^/api/v1/report-download/[A-Za-z0-9._-]+\Z")


def _signed_download_link(via_client, report_id, file_path, route_path, limit_mb) -> str:
    """Ask the server for a signed, expiring link to one file and format the answer.

    Never falls back to the plain report resource URL.
    """
    match = _ROUTE_PATH_ATTEMPT.match(str(route_path))
    if not match:
        return json.dumps({
            "error": "Could not determine the run attempt for this file, so a download link cannot be requested.",
            "hint": "The report data may be incomplete"
        }, indent=2)

    try:
        minted = via_client.call(
            method="POST",
            endpoint=f"/api/v1/run/{report_id}/download-link",
            data={"attemptUuid": match.group(1), "filePath": file_path},
        )
    except Exception as e:
        text = str(e)
        # The SDK reports HTTP status only inside the message text.
        if "Error 305" in text:
            # A 404 is ambiguous: no such route, unknown attempt, missing file or a symlink.
            return json.dumps({
                "error": (
                    f"File is larger than {limit_mb:.0f}MB, too large to return inline, "
                    "and the server did not issue a download link for it. "
                    "This server may not support download links, or the file cannot be linked."
                )
            }, indent=2)
        if "Status code: 405" in text:
            return json.dumps({
                "error": (
                    f"File is larger than {limit_mb:.0f}MB, too large to return inline, "
                    "and this server does not issue download links."
                )
            }, indent=2)
        logger.error(f"Could not create a download link: {e}")
        return json.dumps({"error": f"A download link could not be created: {text}"}, indent=2)

    link_path = minted.get("path") if isinstance(minted, dict) else None
    if not isinstance(link_path, str) or not _DOWNLOAD_LINK_PATH.match(link_path):
        return json.dumps({
            "error": "The server returned an unexpected download link response, so no link was created."
        }, indent=2)

    download_url = via_client.auth.hostname + link_path
    link_name = file_path.split("/")[-1]
    link_name = "".join(
        c for c in link_name
        if c not in "/\\" and unicodedata.category(c) not in ("Cc", "Cf")
    )
    link_name = link_name or "download"
    return json.dumps({
        "success": True,
        "delivery": "url",
        "file_name": link_name,
        "download_url": download_url,
        "download_command": f"curl -fL -o {shlex.quote('./' + link_name)} {shlex.quote(download_url)}",
        "expires_at": minted.get("expiresAt"),
        "message": (
            f"File is larger than {limit_mb:.0f}MB, too large to return inline. "
            "Run download_command to save it to disk, or open download_url in a browser. "
            "The link is for this one file and expires in about 15 minutes."
        )
    }, indent=2)


@mcp.tool()
def download_file(report_id: str, file_path: str) -> str:
    """
    Download a file from a report.
    A file up to 5MB comes back inline as base64 ("delivery": "inline").
    A larger file comes back as a link ("delivery": "url") with a ready
    curl command; run that command to save the file to disk, so the bytes
    never pass through the conversation. The link is for that one file and
    expires in about 15 minutes. It keeps working until then even if the
    token used to request it is revoked, so ask for a new link rather than
    reusing an old one.

    Args:
        report_id: The ID of the report containing the file.
        file_path: The path of the file within the report (from list_files output).

    Returns:
        Inline: JSON with file_name, file_content_base64, and file_size.
        The client should decode the base64 content and save to disk.
        Link: JSON with file_name, download_url, download_command, and expires_at.

    Example:
        # Get the file content (returns JSON string)
        result_json = download_file(report_id="12524", file_path="fastqc/control_rep1.R1_fastqc.html")
        
        # Client-side: parse JSON, decode base64, and save
        # import json, base64
        # result = json.loads(result_json)
        # content = base64.b64decode(result["file_content_base64"])
        # with open(result["file_name"], "wb") as f:
        #     f.write(content)
    """
    try:
        via_client = get_client()
        logger.info(f"Downloading file {file_path} from report {report_id}")
        
        # Get report data to find the file URL
        report_data = via_client.reports.fetch_report_data(report_id)
        
        # Get all files and find the matching one
        files_df = via_client.reports.get_all_files(report_data)
        file_details = files_df[files_df["file_path"] == file_path]
        
        if file_details.empty:
            return json.dumps({
                "error": f"File '{file_path}' not found in report",
                "hint": "Use list_files(report_id) to see available files"
            }, indent=2)
        
        # Validate routePath exists and is not empty
        if "routePath" not in file_details.columns:
            return json.dumps({
                "error": "File metadata missing routePath field",
                "hint": "The report data may be incomplete or corrupted"
            }, indent=2)
        
        route_path = file_details["routePath"].iloc[0]
        # Check for None, empty string, or NaN (NaN != NaN is True)
        if not route_path or route_path != route_path:
            return json.dumps({
                "error": f"File '{file_path}' has no download path available",
                "hint": "The file may not be accessible for download"
            }, indent=2)
        
        # Build the file URL
        file_url = via_client.auth.hostname + route_path
        file_name = os.path.basename(file_path)

        # Stream the body so a large file is never read whole into memory.
        # Timeout: (connect_timeout, read_timeout) in seconds
        # Probe errors never carry the plain report resource URL.
        try:
            response = requests.get(file_url, headers=via_client.auth.get_headers(), timeout=(20, 300), stream=True)
        except Exception as probe_error:
            logger.error(f"Size probe failed: {type(probe_error).__name__}")
            return json.dumps({
                "error": f"Could not reach the server to measure the file ({type(probe_error).__name__})"
            }, indent=2)
        if response.status_code != 200:
            reason = str(getattr(response, 'reason', 'Unknown'))
            if "report-resources" in reason:
                reason = "Unknown"
            response.close()
            return json.dumps({
                "error": f"Failed to download file: HTTP {response.status_code} {reason}",
                "status_code": response.status_code
            }, indent=2)
        
        # Read at most one byte past the inline limit, then stop.
        try:
            content = bytearray()
            too_large = False
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                content.extend(chunk)
                if len(content) > INLINE_DOWNLOAD_MAX_BYTES:
                    too_large = True
                    break
        finally:
            response.close()

        if too_large:
            limit_mb = INLINE_DOWNLOAD_MAX_BYTES / (1024 * 1024)
            logger.info(f"File '{file_name}' is over {limit_mb:.0f}MB, requesting a download link")
            return _signed_download_link(via_client, report_id, file_path, route_path, limit_mb)

        content_size = len(content)
        file_content_base64 = base64.b64encode(bytes(content)).decode('utf-8')

        logger.info(f"Downloaded file '{file_name}' ({content_size} bytes)")

        return json.dumps({
            "success": True,
            "delivery": "inline",
            "file_name": file_name,
            "file_size": content_size,
            "file_content_base64": file_content_base64,
            "message": f"File downloaded successfully. Decode base64 content and save to disk."
        }, indent=2)
        
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        message = str(e)
        if "report-resources" in message:
            message = f"Error downloading file ({type(e).__name__})"
        return json.dumps({"error": message})


@mcp.tool()
def load_file(report_id: str, file_path: str, separator: str = "\t") -> str:
    """
    Load and return the contents of a file from a report. For tabular files
    (CSV, TSV, TXT), returns a formatted table. For other files, returns raw content.
    Use this when you need to analyze file contents without downloading.
    """
    try:
        via_client = get_client()
        logger.info(f"Loading file {file_path} from report {report_id}")
        
        report_data = via_client.reports.fetch_report_data(report_id)
        content = via_client.reports.load_file(report_data, file_path, sep=separator)
        
        if hasattr(content, 'to_dict'):
            result = {
                "type": "dataframe",
                "data": content.to_dict(orient='records'),
                "shape": content.shape,
                "columns": list(content.columns)
            }
        else:
            result = {
                "type": "text",
                "content": str(content)
            }
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error loading file: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def upload_file(report_id: str, file_name: str, file_content_base64: str, remote_dir: str) -> str:
    """
    Upload a file to a report. The file content is provided as base64-encoded string.
    This allows uploading files from any client regardless of server filesystem access.
    
    Thread-safe: Each upload uses a unique temporary directory that is automatically
    cleaned up after the upload completes (success or failure).
    
    Args:
        report_id: The ID of the report to upload to.
        file_name: The name for the uploaded file (e.g., "data.tsv", "results.csv").
        file_content_base64: The file content encoded as a base64 string.
            To encode a file: base64.b64encode(file_bytes).decode('utf-8')
        remote_dir: Directory within the report to place the file. This should be
            a process name from the report (use get_report_dirs or list_processes
            to find available directories).
    
    Returns:
        Upload status and file information.
    
    Example:
        # First, get available directories:
        # get_report_dirs(report_id="12690")  # Returns: {"directories": ["FastQC", "STAR", ...]}
        #
        # Then upload to a specific process directory:
        upload_file(
            report_id="12690",
            file_name="data.tsv",
            file_content_base64="<base64 content>",
            remote_dir="FastQC"
        )
    """
    # Validate remote_dir is provided
    if not remote_dir or not remote_dir.strip():
        return json.dumps({
            "error": "remote_dir is required",
            "hint": "Use get_report_dirs(report_id) or list_processes(report_id) to find available directories"
        }, indent=2)
    
    # Validate file_name to prevent path traversal attacks
    safe_file_name = os.path.basename(file_name)
    if not safe_file_name or safe_file_name in (".", ".."):
        return json.dumps({
            "error": "Invalid file name",
            "hint": "File name cannot be empty or contain path traversal characters"
        }, indent=2)
    
    # Warn if path components were stripped (potential path traversal attempt)
    if file_name != safe_file_name:
        logger.warning(f"Path traversal detected in file_name: '{file_name}' -> using '{safe_file_name}'")
    
    # Decode base64 content first (before creating temp resources)
    try:
        file_bytes = base64.b64decode(file_content_base64)
    except Exception as e:
        return json.dumps({
            "error": f"Invalid base64 content: {e}",
            "hint": "Ensure file content is properly base64 encoded"
        }, indent=2)
    
    # Check file size limit
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        size_mb = len(file_bytes) / (1024 * 1024)
        max_mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
        return json.dumps({
            "error": f"File too large for upload: {size_mb:.1f}MB exceeds {max_mb:.0f}MB limit",
            "hint": "Use the SDK directly for large file uploads"
        }, indent=2)
    
    # Get client early to fail fast before allocating temp resources
    try:
        via_client = get_client()
    except Exception as e:
        return json.dumps({
            "error": f"Failed to initialize client: {e}",
            "hint": "Check your Foundry Connect credentials"
        }, indent=2)
    
    # Use TemporaryDirectory context manager for automatic cleanup
    # Each call gets a unique directory - safe for concurrent access
    try:
        with tempfile.TemporaryDirectory(prefix="foundry_upload_") as temp_dir:
            temp_file_path = os.path.join(temp_dir, safe_file_name)
            
            # Write decoded content to temp file
            with open(temp_file_path, "wb") as f:
                f.write(file_bytes)
            
            logger.info(f"Uploading file '{safe_file_name}' ({len(file_bytes)} bytes) to report {report_id}, dir '{remote_dir}'")
            
            # Upload using SDK
            response = via_client.reports.upload_report_file(
                report_id,
                temp_file_path,
                dir=remote_dir
            )
            
            result = serialize_response(response)
            return json.dumps(result, indent=2)
            
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_report_dirs(report_id: str) -> str:
    """
    Get all available directories in a report where files can be uploaded.
    Returns a list of directory names that can be used with upload_file.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting report directories for report {report_id}")
        directories = via_client.reports.get_report_dirs(report_id)
        return json.dumps({"directories": directories}, indent=2)
    except Exception as e:
        logger.error(f"Error getting report directories: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_all_report_paths(report_id: str) -> str:
    """
    Get all file paths (routePaths) for a specific report.
    Returns a comprehensive list of all accessible file paths in the report.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting all paths for report {report_id}")
        
        paths = via_client.reports.get_all_report_paths(report_id)
        result = serialize_response(paths)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting report paths: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Run Management Tools
# ============================================================================

# Valid run-list sort fields — must match the backend's runListAPIDbMap
# (backend/src/types/run.ts). Any other value is rejected by the backend.
# NOTE the route excludes pipelineId from the whitelist
# (`Object.keys(runListAPIDbMap).filter(key => key !== "pipelineId")`), so it is
# deliberately absent here and aliased to pipelineName below.
RUN_LIST_SORT_FIELDS = {
    "id", "name", "status", "username", "dateCreated",
    "pipelineName", "summary", "dateCreatedLastRun", "schedulerId",
}

# Common model-guessed sort aliases -> a valid field. Keys are normalized
# (lowercased, non-alphanumerics stripped) before lookup.
RUN_LIST_SORT_ALIASES = {
    "datemodified": "dateCreated",
    "modifiedat": "dateCreated",
    "updatedat": "dateCreated",
    "modified": "dateCreated",
    "updated": "dateCreated",
    "created": "dateCreated",
    "createdat": "dateCreated",
    "date": "dateCreated",
    "lastrun": "dateCreatedLastRun",
    "recent": "dateCreatedLastRun",
    # The backend rejects sorting by pipelineId; grouping by pipeline name is
    # the same intent and is accepted.
    "pipelineid": "pipelineName",
    "pipeline": "pipelineName",
}


def _normalize_run_sort(sort: str) -> str:
    """Map a requested sort field to a sort key the backend accepts.

    The backend only accepts the keys of runListAPIDbMap; anything else is
    rejected (e.g. a model guessing "dateModified"). Pass valid keys through,
    alias the common guesses, and fall back to "dateCreated" so listing never
    hard-fails on an unknown sort field.
    """
    if not sort:
        return "dateCreated"
    if sort in RUN_LIST_SORT_FIELDS:
        return sort
    normalized = "".join(ch for ch in sort.lower() if ch.isalnum())
    return RUN_LIST_SORT_ALIASES.get(normalized, "dateCreated")


def _resolve_run_tag_ids(via_client, tags: str):
    """Resolve comma-separated run tag NAMES to tag IDs for the run-list filter.

    The backend's run-list tag filter takes tag IDs (UUIDs), not names, so we
    look names up via GET /api/v1/tag?entityType=run (the same endpoint the web
    UI uses). Returns (tag_ids, unknown_names, available_names).
    """
    requested = [t.strip() for t in tags.split(",") if t.strip()]
    if not requested:
        return [], [], []
    resp = via_client.call(
        method="GET", endpoint="/api/v1/tag", params={"entityType": "run"}
    )
    available = resp.get("data", []) if isinstance(resp, dict) else []
    by_name = {}
    for t in available:
        name = (t.get("name") or "").strip()
        if name:
            by_name[name.lower()] = t.get("id")
    tag_ids, unknown = [], []
    for name in requested:
        tid = by_name.get(name.lower())
        if tid:
            tag_ids.append(tid)
        else:
            unknown.append(name)
    available_names = sorted(t.get("name") for t in available if t.get("name"))
    return tag_ids, unknown, available_names


@mcp.tool()
def list_runs(
    search_query: str = "",
    take: int = 10,
    skip: int = 0,
    sort: str = "dateCreated",
    order: str = "desc",
    tags: str = ""
) -> str:
    """
    List and search for runs/pipeline executions in Foundry Connect.
    Supports fuzzy search by name, pagination, sorting, and filtering by tag.
    Returns run details including ID (same as report_id), name, status, pipeline info, and dates.
    Use this to discover runs and get their IDs for use with other tools.

    Args:
        search_query: Fuzzy match on run name (optional).
        take: Page size (default 10).
        skip: Offset for pagination (default 0).
        sort: Sort field. Valid values: id, name, status, username, dateCreated,
            pipelineName, summary, dateCreatedLastRun, schedulerId.
            There is NO "pipelineId" sort — use "pipelineName".
            There is NO "dateModified"/"updatedAt" — use "dateCreated" for the most
            recent runs. Unrecognized values fall back to "dateCreated".
        order: "desc" (newest first, default) or "asc".
        tags: Comma-separated tag name(s) to filter by, e.g. "demo" or "demo,qc".
            A run matches if it carries ANY of the named tags (case-insensitive).
            Unknown names return an error listing the available run tags.
    """
    try:
        via_client = get_client()
        sort = _normalize_run_sort(sort)

        body = {"searchKey": search_query}
        if tags and tags.strip():
            tag_ids, unknown, available_names = _resolve_run_tag_ids(via_client, tags)
            if unknown:
                return json.dumps({
                    "error": f"Unknown run tag(s): {', '.join(unknown)}",
                    "available_run_tags": available_names,
                }, indent=2)
            if tag_ids:
                body["tagIds"] = tag_ids

        logger.info(
            f"Listing runs (search: '{search_query}', take: {take}, skip: {skip}, "
            f"sort: {sort}, tags: '{tags}')"
        )
        
        response = via_client.call(
            method="POST",
            endpoint="/api/v1/run/list",
            params={
                "take": take,
                "skip": skip,
                "sort": sort,
                "order": order
            },
            data=body
        )

        return json.dumps(response, indent=2)
    except Exception as e:
        logger.error(f"Error listing runs: {e}")
        return json.dumps({"error": str(e)})


# Bench-friendly labels for raw RunStatus values (mirrors the frontend's
# getRunStatusDisplayText, but says "Failed" instead of "Error" for clarity).
#
# Both vocabularies are listed. Backend 2.20.0 renamed the wire values, and this
# server releases on its own cadence, so it may be pointed at either version.
_RUN_STATUS_DISPLAY = {
    # Canonical vocabulary, backend 2.20.0 and later.
    "Failed": "Failed",
    "LaunchFailed": "Failed",
    "Completed": "Completed",
    "Running": "Running",
    "Initializing": "Initializing",
    "Submitted": "Initializing",
    "Terminated": "Terminated",
    "NotSubmitted": "Not submitted",
    "Unreachable": "Connecting",
    # Legacy vocabulary, backends before 2.20.0. Remove once the floor moves.
    "NextErr": "Failed",
    "Error": "Failed",
    "NextSuc": "Completed",
    "NextRun": "Running",
    "init": "Initializing",
    "Waiting": "Initializing",
    "Aborted": "Connecting",
}


def _human_run_status(status):
    """Map a raw RunStatus string to a plain-language label for scientists."""
    return _RUN_STATUS_DISPLAY.get(status, "Connecting")


@mcp.tool()
def get_run(run_id: str = None, run_name: str = None, include_reports: bool = False) -> str:
    """
    Get detailed information about a specific run by its ID or name.
    Supports fuzzy name matching - if exact match not found, returns similar matches.
    Returns run properties including ID (same as report_id), status, pipeline info, dates, and associated reports.
    The returned run ID can be used with report tools (e.g., fetch_report, list_files, download_file).
    If the run failed, call get_run_log(run_id) to see the error.
    """
    try:
        via_client = get_client()
        
        if not run_id and not run_name:
            return json.dumps({
                "error": "Either run_id or run_name must be provided"
            }, indent=2)
        
        # If run_name provided, search for it
        if run_name and not run_id:
            logger.info(f"Searching for run by name: {run_name}")
            search_response = via_client.call(
                method="POST",
                endpoint="/api/v1/run/list",
                params={"take": 100},
                data={"searchKey": run_name}
            )
            
            runs = search_response.get("data", [])
            
            # Try exact match first
            exact_match = next((run for run in runs if run.get("name") == run_name), None)
            
            if exact_match:
                logger.info(f"Found exact match for '{run_name}'")
                run_id = str(exact_match.get("id"))
                result = {
                    "match_type": "exact",
                    "run": exact_match
                }
            elif runs:
                # Fuzzy match - return the best matches
                logger.info(f"No exact match for '{run_name}', returning fuzzy matches")
                result = {
                    "match_type": "fuzzy",
                    "message": f"No exact match found for '{run_name}'. Showing similar runs:",
                    "matches": runs[:10]
                }
                return json.dumps(result, indent=2)
            else:
                result = {
                    "match_type": "none",
                    "error": f"No runs found matching '{run_name}'"
                }
                return json.dumps(result, indent=2)
        else:
            # run_id provided, fetch directly
            logger.info(f"Fetching run by ID: {run_id}")
            search_response = via_client.call(
                method="POST",
                endpoint="/api/v1/run/list",
                params={"take": 1, "filter": f"id:eq={run_id}"},
                data={"searchKey": ""}
            )
            
            runs = search_response.get("data", [])
            if runs:
                result = {
                    "match_type": "id",
                    "run": runs[0]
                }
            else:
                return json.dumps({
                    "error": f"No run found with ID: {run_id}"
                }, indent=2)
        
        # If include_reports is True, fetch reports for this run
        if include_reports and run_id:
            logger.info(f"Fetching reports for run ID: {run_id}")
            try:
                reports = via_client.call(
                    method="GET",
                    endpoint=f"/api/v1/run/{run_id}/reports"
                )
                result["reports"] = reports
            except Exception as e:
                logger.warning(f"Could not fetch reports for run {run_id}: {e}")
                result["reports"] = {"error": str(e)}

        # NOTE: summary/next_steps are merged FLAT into `result` here (not
        # nested in an envelope like get_run_log/get_run_details) so that
        # existing consumers of result["run"] keep working unchanged. Don't
        # "fix" this to use envelope() without also updating those consumers.
        run_obj = result.get("run")
        if isinstance(run_obj, dict):
            display = _human_run_status(run_obj.get("status"))
            result["status_display"] = display
            name = run_obj.get("name") or f"#{run_id}"
            if display == "Failed":
                result["summary"] = (
                    f"Run '{name}' ({run_id}) failed. Fetch the log to see why."
                )
                result["next_steps"] = [
                    f"get_run_log(run_id='{run_id}') to see the error."
                ]
            elif display == "Running":
                result["summary"] = f"Run '{name}' ({run_id}) is still running."
                result["next_steps"] = [
                    f"Check again later, or get_run_log(run_id='{run_id}') "
                    f"to watch progress."
                ]
            elif display == "Completed":
                result["summary"] = (
                    f"Run '{name}' ({run_id}) completed successfully."
                )
                result["next_steps"] = [
                    f"get_run(run_id='{run_id}', include_reports=True) to see "
                    f"the result files."
                ]
            else:
                result["summary"] = f"Run '{name}' ({run_id}) status: {display}."

        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting run: {e}")
        return json.dumps({"error": str(e)})


# Log file names, ordered most→least useful for diagnosing a failed run.
#
# Ordering follows what real cluster runs actually produce, not just what the
# backend's constants suggest. Verified against failed LSF runs on staging
# (12193/12194): those return NO `.command.*` files at all, and `err.log` is a
# fixed 42-byte job-starter stub ("JOB_STARTER: slots=1 ..."). The composite
# `log.txt` and the Nextflow debug log are what actually carry the failure, so
# they rank above `err.log` — the reverse of the original ordering, which
# handed the user the stub and answered nothing.
_LOG_PRIORITY = [
    ".command.err", ".command.out", ".command.log",
    "log.txt", ".nextflow.log",
    "err.log", "serverlog.txt",
]

# A log shorter than this is treated as a stub and only used when nothing
# substantive exists. Sized against the 42-byte `err.log` above; the real
# composite logs run to tens of KB, so there is no ambiguity in between.
_MIN_USEFUL_LOG_CHARS = 200

# Non-log artifacts the backend's log set can include (Nextflow HTML reports,
# trace files, the pipeline script itself) that should never be handed to the
# user as "the most relevant log" via the fallback picker.
_NON_LOG_SUFFIXES = (".html", ".nf", ".config")


def _pick_diagnostic_log(logs):
    """From a list of {"name","content"} dicts, return (name, content) of the
    most useful non-empty log for diagnosis, or (None, None) if all are empty.

    Prefers a *substantive* log: a near-empty stub never outranks a real log,
    however high it sits in the priority list. A stub is still returned if it
    is all that exists — some log beats "no logs available"."""
    by_name = {
        entry.get("name"): (entry.get("content") or "")
        for entry in logs
        if isinstance(entry, dict)
    }
    stub = None  # best near-empty candidate, used only as a last resort

    def consider(name, content):
        """Return (name, content) if substantive; otherwise remember as stub."""
        nonlocal stub
        body = content.strip()
        if not body:
            return None
        if len(body) >= _MIN_USEFUL_LOG_CHARS:
            return name, content
        if stub is None:
            stub = (name, content)
        return None

    for name in _LOG_PRIORITY:
        picked = consider(name, by_name.get(name, ""))
        if picked:
            return picked

    for name, content in by_name.items():
        if not name or name.endswith(_NON_LOG_SUFFIXES):
            continue
        picked = consider(name, content)
        if picked:
            return picked

    return stub if stub else (None, None)


@mcp.tool()
def get_run_log(run_id: str, attempt_id: int = None) -> str:
    """
    Show why a run is in its current state by returning its execution log.
    Use this whenever a run's status is Failed (Error/NextErr) or the user asks
    "why did it fail / what happened". Returns a plain-language summary plus the
    tail of the most relevant log (.command.err / Nextflow). Pair with get_run
    (status) and get_run_details (the settings that produced it).
    """
    try:
        via_client = get_client()
        params = {"attemptId": attempt_id} if attempt_id else None
        logger.info(
            f"Fetching logs for run {run_id}"
            + (f" attempt {attempt_id}" if attempt_id else "")
        )
        logs = via_client.call(
            method="GET", endpoint=f"/api/v1/run/{run_id}/logs", params=params
        )
        if isinstance(logs, dict) and "logs" in logs:
            logs = logs["logs"]
        if not isinstance(logs, list):
            logs = []

        name, content = _pick_diagnostic_log(logs)
        if not name:
            # Distinguish "not written yet" from "gone for good". Telling
            # someone whose run failed a month ago to try again shortly is
            # actively misleading — live on staging, 8 of 8 failed runs have a
            # 4-byte log.txt and nothing else, their run directories long since
            # cleaned up.
            status = _fetch_run_status(via_client, run_id)
            if status in _TERMINAL_RUN_STATUSES:
                result = envelope(
                    summary=(
                        f"Run {run_id} finished ({_human_run_status(status)}) "
                        f"but its logs are no longer on disk — the run "
                        f"directory has been cleaned up, so they will not "
                        f"appear later."
                    ),
                    data={"logs": [], "status": _human_run_status(status),
                          "logs_purged": True},
                    next_steps=[
                        f"The run's configuration is still available: "
                        f"get_run_details(run_id='{run_id}').",
                        "To investigate the failure, duplicate_run and launch a "
                        "fresh attempt — that writes fresh logs.",
                    ],
                )
                return json.dumps(result, indent=2)
            result = envelope(
                summary=(
                    f"No log output is available yet for run {run_id}. If it is "
                    f"still starting or running on a cluster, logs may not have "
                    f"synced — try again shortly."
                ),
                data={"logs": [], "logs_purged": False},
                next_steps=[f"Check status with get_run(run_id='{run_id}')."],
            )
            return json.dumps(result, indent=2)

        result = envelope(
            summary=(
                f"Showing the tail of '{name}' for run {run_id} (the most "
                f"relevant log). Read the last lines for the error or the "
                f"completion message."
            ),
            data={
                "log_name": name,
                "log_tail": tail_text(content),
                "available_logs": [
                    entry.get("name") for entry in logs
                    if isinstance(entry, dict) and entry.get("name")
                ],
            },
            next_steps=[
                f"If it failed, get_run_details(run_id='{run_id}') shows the "
                f"inputs/params that caused it.",
                f"Fix the cause, then — after confirming with the user — "
                f"re-launch with initiate_run(run_id='{run_id}', "
                f"run_type='resumerun') to reuse completed steps.",
            ],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error fetching logs for run {run_id}: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Run Execution Tools
# ============================================================================


def _iter_run_inputs(inputs):
    """Yield normalized {name, value, type, vmetaCollectionId} dicts from either
    run-input shape: the Foundry Connect list of {name,value,type,...}, or the
    external-pipeline (nf-core/Nextflow) dict of {name: {...}} that the backend
    returns instead."""
    if isinstance(inputs, dict):
        for name, val in inputs.items():
            if isinstance(val, dict) and "value" in val:
                yield {
                    "name": name,
                    "value": val.get("value"),
                    "type": val.get("type"),
                    "vmetaCollectionId": val.get("vmetaCollectionId"),
                }
            else:
                yield {
                    "name": name,
                    "value": val,
                    "type": None,
                    "vmetaCollectionId": None,
                }
        return
    for inp in inputs or []:
        if isinstance(inp, dict):
            yield {
                "name": inp.get("name"),
                "value": inp.get("value"),
                "type": inp.get("type"),
                "vmetaCollectionId": inp.get("vmetaCollectionId"),
            }


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text):
    """Reduce a run's stored HTML description to plain text for chat display."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_HTML_TAG_RE.sub(" ", text))).strip()


_RUN_ENV_FIELDS = ("id", "name", "transport", "executor", "hostname", "username", "defWorkdir", "defPublishdir")
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")
# A login never starts with a dot, so "." and ".." cannot climb out of /home.
_LOGIN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


def _compact_run_environment(env):
    """The run environment fields a user picks by, without scheduler internals."""
    return {key: env.get(key) for key in _RUN_ENV_FIELDS}


def _default_execution_directory(env, run_name):
    """Proposed execution directory for a new run, or None when the user must choose.

    Order: the environment's declared default (used as is, as the web UI does), then
    /home/<login>/foundry-connect-runs/<run name>. No usable login name means no default.
    """
    declared = (env.get("defWorkdir") or "").strip()
    if declared:
        return declared
    login = (env.get("username") or "").strip()
    if not _LOGIN_NAME.fullmatch(login):
        return None
    segment = _SAFE_SEGMENT.sub("_", (run_name or "").strip()).lstrip(".") or "run"
    return f"/home/{login}/foundry-connect-runs/{segment}"


def _summarize_run_details(details):
    """Distill a run's full details blob into a compact, plain-language summary
    a bench scientist can read without wading through processOptions."""
    pipeline = details.get("mainPipeline") or {}
    project = details.get("project") or {}
    inputs = details.get("inputs") or []
    proc_opts = details.get("processOptions") or {}

    sample_inputs, settings, reference_paths = [], [], []
    for inp in _iter_run_inputs(inputs):
        name, value = inp["name"], inp["value"]
        if inp["type"] == "vmetaCollection":
            entry = {"name": name, "dataset": value}
            # The dataset id is the one thing needed to re-point samples at a
            # new dataset, so keep it in the compact view.
            if inp.get("vmetaCollectionId"):
                entry["vmetaCollectionId"] = inp["vmetaCollectionId"]
            sample_inputs.append(entry)
        elif isinstance(value, str) and value.startswith("/"):
            reference_paths.append(name)
        else:
            settings.append({"name": name, "value": value})

    return {
        "run": {
            "name": details.get("name"),
            "description": _strip_html(details.get("summary")),
        },
        "pipeline": {
            "name": pipeline.get("name"),
            "version": pipeline.get("version"),
            "id": pipeline.get("id"),
        },
        "project": {"name": project.get("name"), "id": project.get("id")},
        "permission": details.get("permission"),
        "groupId": details.get("groupId"),
        "sample_inputs": sample_inputs,
        "settings": settings,
        "reference_paths": reference_paths,
        "process_option_groups": len(proc_opts),
    }


@mcp.tool()
def get_run_details(run_id: str, verbose: bool = False) -> str:
    """
    Show a run's configuration. By default returns a compact, plain-language
    summary (pipeline, samples, key settings, count of process-option groups).
    Pass verbose=True to get the FULL editable inputs[] and processOptions{}
    needed to build an update_run body — do this before duplicate_run/update_run.
    For a fresh run of a pipeline, create_run returns the same form directly.
    get_run shows a run's status; this shows the settings that produced it.
    """
    try:
        via_client = get_client()
        details = via_client.call(
            method="GET", endpoint=f"/api/v1/run/{run_id}/details"
        )
        if verbose:
            return json.dumps(details, indent=2)

        summary_data = _summarize_run_details(details)
        pipeline = summary_data["pipeline"]
        run_label = summary_data["run"]["name"] or f"#{run_id}"
        result = envelope(
            summary=(
                f"Run '{run_label}' ({run_id}) uses pipeline "
                f"'{pipeline['name']}' (v{pipeline['version']}) with "
                f"{len(summary_data['settings'])} settings and "
                f"{summary_data['process_option_groups']} process-option groups."
            ),
            data=summary_data,
            next_steps=[
                f"To edit or re-launch, call get_run_details(run_id='{run_id}', "
                f"verbose=True) for the full editable config, then update_run.",
                "Launching a run uses HPC compute — confirm with the user "
                "before initiate_run.",
            ],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting run details for {run_id}: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_vmeta_dataset(name: str) -> str:
    """
    Create an empty vmeta dataset (study-tracker dataset). Returns its `_id`,
    which is used as the `vmetaCollectionId` of a run's file input. Add file
    rows afterward with add_files_to_dataset. Name must be non-empty and unique
    in the project (lowercase letters, digits, '-' and '_' recommended).
    """
    try:
        if not name or not name.strip():
            raise ValueError("Dataset name must be a non-empty string")
        via_client = get_client()
        created = via_client.call(
            method="POST",
            endpoint="/api/v1/vmeta/dataset/create",
            data={"name": name.strip()},
        )
        return json.dumps(created, indent=2)
    except Exception as e:
        logger.error(f"Error creating vmeta dataset '{name}': {e}")
        return json.dumps({"error": str(e)})


def _input_type(item):
    """The input's kind, derived the way the run page derives it."""
    if item.get("file"):
        return "file"
    if item.get("singleFile"):
        return "singleFile"
    if item.get("type") == "input":
        return "val"
    return item.get("qualifier") or item.get("type")


def _input_definition(item, section=None):
    options = item.get("opt") or item.get("dropDown")
    if isinstance(options, str):
        options = [o.strip() for o in options.split(",") if o.strip()]
    default = item.get("defaultVal")
    if default is None:
        default = item.get("defVal")
    definition = {"name": item["varName"].split(".")[-1]}
    if section is not None:
        definition["section"] = section
    definition.update({
        "type": _input_type(item),
        "required": not item.get("optional"),
        "default": default,
        "description": item.get("desc") or item.get("inDescOpt"),
        "options": options or None,
    })
    return definition


def _input_definitions(parameter_list):
    """
    The inputs a run of this pipeline takes, from GET /run/{id}/parameter-list.
    A fresh run's details carry no inputs until one is saved, so this is the
    only place a client can learn what to fill. Required inputs come first.
    Foundry pipelines: main inputs are the `params.*` items of the pipeline
    schema, its process schemas and its input nodes. External (nf-core)
    pipelines: one schema per section, and `section` says where it is saved.
    """
    inputs = (parameter_list or {}).get("inputs")
    if not isinstance(inputs, dict):
        return []
    definitions, seen = [], set()

    def add(items, section=None):
        for item in items or []:
            var_name = item.get("varName") or ""
            if not var_name.startswith("params."):
                continue
            definition = _input_definition(item, section)
            ident = (section, definition["name"])
            if ident not in seen:
                seen.add(ident)
                definitions.append(definition)

    if isinstance(inputs.get("pipe"), dict):
        pipe = inputs["pipe"]
        add(pipe.get("schema"))
        for process in (inputs.get("processes") or {}).values():
            add((process or {}).get("schema"))
        add(pipe.get("inputNodes"))
    else:
        for section, block in inputs.items():
            add((block or {}).get("schema"), section)
    return [d for d in definitions if d["required"]] + [d for d in definitions if not d["required"]]


def _run_form(details, run_name):
    """Everything a user fills before launching a freshly created run."""
    env_block = details.get("runEnvironment") or {}
    envs = env_block.get("availableList") or []
    return {
        # As the server returns them (a list for Foundry pipelines, a dict for
        # external ones), so ids and collection fields round-trip into update_run.
        "inputs": details.get("inputs") or [],
        "process_options": details.get("processOptions") or {},
        "run_environments": [_compact_run_environment(e) for e in envs],
        "selected_run_environment_id": env_block.get("selectedId"),
        "proposed_execution_directory": {str(e.get("id")): _default_execution_directory(e, run_name) for e in envs},
        "permission": details.get("permission"),
        "groupId": details.get("groupId"),
    }


def _run_form_envelope(run_id, details, run_name, verb, via_client):
    form = _run_form(details, run_name)
    try:
        parameter_list = via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/parameter-list")
        form["input_definitions"] = _input_definitions(parameter_list)
    except Exception as e:
        # The rest of the form still helps; say plainly what is missing.
        form["input_definitions"] = None
        form["input_definitions_error"] = f"Could not read the pipeline's inputs: {e}"
    if form["run_environments"]:
        summary = f"{verb} run {run_id}. Choose a run environment and confirm its execution directory."
    else:
        summary = (
            f"{verb} run {run_id}, but you have no run environment you can launch on. "
            "Add one under your profile's run environments, or ask your organization to share one."
        )
    return envelope(
        summary=summary,
        data={"run_id": run_id, **form},
        next_steps=[
            (
                "Ask the user for each entry of input_definitions (required ones first; a default can be kept), "
                "the run environment, and whether the proposed execution directory is right "
                "(null means none could be proposed: ask for one)."
            ),
            (
                "Foundry pipeline: update_run inputs is a list of {name, type: 'input', value} (value a string), or "
                "type 'vmetaCollection' with vmetaCollectionId for a file input. External pipeline: a dict "
                "{section: {name: value}} holding every value, since it replaces the saved inputs whole."
            ),
            "update_run(run_id, inputs=..., run_environment_id=..., launch_directory=..., permission=...) after the user confirms.",
            "preflight_run(run_id), then initiate_run(run_id) after the user confirms the launch.",
        ],
    )


@mcp.tool()
def create_run(pipeline_id: int, project_id: int, name: str) -> str:
    """
    Create a new, unlaunched run of a pipeline in one of your projects and
    return its form: the pipeline's input definitions (name, type, required,
    default), process options, the run environments you can
    launch on, and a proposed execution directory per environment. This
    writes; confirm the pipeline, project and run name with the user first.
    Nothing is launched.
    """
    try:
        via_client = get_client()
        created = via_client.call(
            method="POST",
            endpoint="/api/v1/run/new-run-attempt",
            data={"pipelineId": pipeline_id, "projectId": project_id, "name": name},
        )
        run_id = (created or {}).get("id") or (created or {}).get("projectPipelineId")
        if not run_id:
            raise RuntimeError(f"The server did not return the new run's id: {created}")
        details = via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/details")
        return json.dumps(_run_form_envelope(run_id, details, name, "Created", via_client), indent=2)
    except Exception as e:
        logger.error(f"Error creating run of pipeline {pipeline_id}: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_run_environments(run_id: str) -> str:
    """
    Show the run environments you can launch this run on, with the proposed
    execution directory for each, plus the run's current form.
    """
    try:
        via_client = get_client()
        details = via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/details")
        return json.dumps(_run_form_envelope(run_id, details, details.get("name", ""), "Read", via_client), indent=2)
    except Exception as e:
        logger.error(f"Error listing run environments for run {run_id}: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def duplicate_run(run_id: str, project_id: int, pipeline_id: int) -> str:
    """
    Duplicate an existing run into a target project/pipeline and return the
    new run's `duplicatedRunId`. `project_id` and `pipeline_id` come from
    get_run_details(run_id, verbose=True) on the source run (its `project.id`
    and `mainPipeline.id`). Use `duplicatedRunId` from the response when
    wiring into update_run or initiate_run. NOTE: the duplicate may DROP the
    vmetaCollection input (e.g. `reads`) and copy processOptions with empty
    arrays — re-add/patch them with update_run before initiate_run. Path
    inputs (references, genomes) are copied verbatim and may point at the
    source project's paths.
    """
    try:
        via_client = get_client()
        duplicated = via_client.call(
            method="POST",
            endpoint=f"/api/v1/run/{run_id}/duplicate",
            data={"projectId": project_id, "pipelineId": pipeline_id},
        )
        return json.dumps(duplicated, indent=2)
    except Exception as e:
        logger.error(f"Error duplicating run {run_id}: {e}")
        return json.dumps({"error": str(e)})


# The types a saved Foundry run input may have. input_definitions report the
# pipeline's own kinds (val, dropdown, file, ...), which the server refuses.
_SAVED_INPUT_TYPES = ("input", "collection", "vmetaCollection")


def _saved_input_type(inp):
    if inp.get("type") in _SAVED_INPUT_TYPES:
        return inp["type"]
    if inp.get("vmetaCollectionId"):
        return "vmetaCollection"
    if inp.get("collectionId"):
        return "collection"
    return "input"


def _normalize_run_inputs(inputs):
    """Give every Foundry run input a type the server accepts; leave the
    external pipeline dict shape alone."""
    if not isinstance(inputs, list):
        return inputs
    return [{**inp, "type": _saved_input_type(inp)} if isinstance(inp, dict) else inp
            for inp in inputs]


def _saved_fields(data):
    """What update_run sent, named rather than repeated in full."""
    saved = {}
    for key, value in data.items():
        if value is None:
            continue
        if key == "inputs" and isinstance(value, list):
            value = [i.get("name") for i in value if isinstance(i, dict)]
        elif key == "inputs" and isinstance(value, dict):
            value = {section: sorted(v) if isinstance(v, dict) else v for section, v in value.items()}
        elif key in ("processOptions", "execEach"):
            value = sorted(value)
        saved[key] = value
    return saved


# Executor settings: get_run_details(verbose=True) reports them as
# advancedSettings.globalExec / processExec; PATCH /save takes execAll / execEach.
_GLOBAL_EXEC_KEYS = {"queue": "queue", "memory": "memory", "cpu": "cpu", "time": "time", "options": "options",
                     "job_queue": "queue", "job_memory": "memory", "job_cpu": "cpu", "job_time": "time",
                     "job_clu_opt": "options"}
_PROCESS_EXEC_KEYS = {"queue": "queue", "memory": "memory", "cpu": "cpu", "time": "time", "options": "options",
                      "opt": "options"}
_EXEC_FIELDS = ("queue", "memory", "cpu", "time", "options")
# A processExec key as the backend writes it: procGnum-<n> for a process in the
# pipeline itself, procGnum-<n>p<module>[_<child>...] for one inside a module.
_PROCESS_EXEC_KEY = re.compile(r"procGnum-\d+(?:p\d+(?:_\d+)*)?")


def _exec_values(options, keys, where):
    """Map one executor row onto the save field names, values as strings."""
    if not isinstance(options, dict):
        raise ValueError(f"{where} must be an object")
    row = {field: "" for field in _EXEC_FIELDS}
    for key, value in options.items():
        if key == "check":
            continue
        if key not in keys:
            raise ValueError(f"{where} has an unknown field '{key}'; use {', '.join(_EXEC_FIELDS)}")
        if value is not None and not isinstance(value, (str, int, float)):
            raise ValueError(f"{where}.{key} must be text or a number")
        row[keys[key]] = "" if value is None else str(value)
    return row


def _exec_enabled(block, where):
    enabled = block.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}.enabled must be true or false")
    return enabled


def _exec_settings_body(exec_settings):
    """Turn update_run's exec_settings into PATCH /save fields."""
    if not isinstance(exec_settings, dict):
        raise ValueError("exec_settings must be an object with globalExec and/or processExec")
    unknown = set(exec_settings) - {"globalExec", "processExec"}
    if unknown:
        raise ValueError(
            f"exec_settings takes only globalExec and processExec, not {', '.join(sorted(unknown))}; "
            "Singularity bind options go in singularity_options"
        )
    body = {}
    block = exec_settings.get("globalExec")
    if block is not None:
        if not isinstance(block, dict):
            raise ValueError("globalExec must be an object")
        body["execAllCheck"] = _exec_enabled(block, "globalExec")
        if block.get("options") is not None:
            body["execAll"] = _exec_values(block["options"], _GLOBAL_EXEC_KEYS, "globalExec.options")
    block = exec_settings.get("processExec")
    if block is not None:
        if not isinstance(block, dict):
            raise ValueError("processExec must be an object")
        body["execEachCheck"] = _exec_enabled(block, "processExec")
        options = block.get("options")
        if options is not None:
            if not isinstance(options, dict):
                raise ValueError("processExec.options must be an object keyed by procGnum-<n> or procGnum-<n>p<module>")
            each = {}
            for key, row in options.items():
                if not _PROCESS_EXEC_KEY.fullmatch(str(key)):
                    raise ValueError(
                        f"processExec.options key '{key}' must be procGnum-<n> or procGnum-<n>p<module>, "
                        "as get_run_details(verbose=True) reports it"
                    )
                where = f"processExec.options['{key}']"
                values = _exec_values(row, _PROCESS_EXEC_KEYS, where)
                check = row.get("check", True)
                check = check if isinstance(check, bool) else str(check).lower() in ("on", "true")
                each[key] = {"check": check, **values}
            body["execEach"] = each
    return body


def _singularity_body(via_client, run_id, singularity_options):
    """Save Singularity run options with the run's current image, which the
    server would otherwise overwrite."""
    details = via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/details")
    singularity = ((details or {}).get("advancedSettings") or {}).get("singularity") or {}
    image = singularity.get("image")
    if not singularity.get("enabled") or not image:
        raise ValueError(
            f"Run {run_id} does not use a Singularity image, so singularity_options has nothing to apply to. "
            "Save the run environment first; a Singularity environment fills in the pipeline's image."
        )
    return {"useSingularity": True, "singularityConfig": {"imagePath": image, "runOptions": singularity_options}}


def _saved_permission(via_client, run_id, group_id):
    """The run's saved permission and group, for a save that leaves them out.
    The save endpoint requires a permission on every call."""
    details = via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/details") or {}
    permission = details.get("permission")
    if permission is None:
        raise ValueError(
            f"Run {run_id} has no saved permission; pass permission "
            "(3 private, 15 group shared, 63 public)."
        )
    if group_id is None:
        group_id = details.get("groupId")
    if permission == 15 and group_id is None:
        raise ValueError("'groupId' is required when permission is GroupShared (15)")
    return permission, group_id


def _validate_update_run(inputs, process_options, permission, group_id, launch_directory=None):
    """Raise ValueError if the update_run body would be rejected by the server."""
    if launch_directory and not launch_directory.startswith("/"):
        raise ValueError(
            "launch_directory must be an absolute path on the run environment, "
            "for example /home/<login>/foundry-connect-runs/<run>."
        )
    if permission == 15 and group_id is None:
        raise ValueError("'groupId' is required when permission is GroupShared (15)")
    # An external pipeline's inputs are a {section: {name: value}} dict.
    for i, inp in enumerate(inputs if isinstance(inputs, list) else []):
        if not isinstance(inp, dict):
            raise ValueError(f"inputs[{i}] must be an object")
        if inp.get("value") == "":
            raise ValueError(
                f"inputs[{i}].value is not allowed to be empty; "
                f"use 'NA' or omit the input"
            )
    for key, entry in (process_options or {}).items():
        if not isinstance(entry, dict):
            continue
        lengths = {
            k: len(v) for k, v in entry.items() if isinstance(v, list)
        }
        if len(set(lengths.values())) > 1:
            raise ValueError(
                f"processOptions['{key}'] spreadsheet arrays must all have the "
                f"same length; got {lengths}"
            )


@mcp.tool()
def update_run(
    run_id: str,
    inputs: list = None,
    process_options: dict = None,
    permission: int = None,
    group_id: int = None,
    run_environment_id: int = None,
    launch_directory: str = None,
    publish_dir: str = None,
    name: str = None,
    exec_settings: dict = None,
    singularity_options: str = None,
    cleanup_delay_hours: int = None,
) -> str:
    """
    Save a run before launch (PATCH /save). Send only what changes. On a
    Foundry pipeline, inputs merge with the saved ones; on an external
    (nf-core) pipeline the inputs dict is REPLACED whole, so send the complete
    dict from create_run's form. process_options always REPLACES the whole
    set, so send it complete or leave it out. `permission` defaults to the
    run's saved permission (and `group_id` to its saved group); pass it only to
    change who can see the run. `group_id` is REQUIRED when `permission` is 15
    (GroupShared) and the run has no saved group. A new
    run cannot launch until run_environment_id and launch_directory are
    saved: take them from create_run or list_run_environments and show the
    proposed directory to the user first. A Foundry input is {name, value};
    its type defaults to 'input' ('vmetaCollection' when vmetaCollectionId is
    given, 'collection' when collectionId is), so the types in create_run's
    input_definitions are never copied. No input `value` may be an empty
    string; use "NA" or omit the input. Within one processOptions entry, all
    spreadsheet (list) columns must be equal length.

    exec_settings sets the resources each task asks the scheduler for (queue,
    memory in GB, cpu, time in minutes, extra scheduler options). Without them
    a batch scheduler applies its site default, often too little memory for
    aligners. Pass the shape get_run_details(run_id, verbose=True) reports
    under advancedSettings, for example from a run that worked:
    {"globalExec": {"enabled": true, "options": {"job_queue": "short",
    "job_memory": "32", "job_cpu": "1", "job_time": "240"}},
    "processExec": {"enabled": true, "options": {"procGnum-12": {"memory":
    "200", "cpu": "5"}}}}. globalExec applies to every task; processExec
    overrides it per process (keys procGnum-<n>, or procGnum-<n>p<module> for
    a process inside a module). Each block given replaces
    the saved one; "enabled": false switches it off.

    singularity_options replaces the Singularity run options (for example
    "-B /home:/home -B /share:/share" so tasks can read data under /share).
    The run must already use a Singularity image; save run_environment_id in
    an earlier call, then the options.

    cleanup_delay_hours turns on the cleanup of the run's intermediate files
    (its work, inputs and initial run folders, which hold downloaded raw
    reads) and sets how many hours after the run ends it happens. The default
    is 120 hours; a short delay such as 6 frees the space soon after the run.
    Results in the publish directory are kept. Must be a whole number of at
    least 1.

    This mutates the run; confirm with the user before calling.
    """
    try:
        _validate_update_run(inputs, process_options, permission, group_id, launch_directory)
        if cleanup_delay_hours is not None:
            # 0 would switch cleanup off: the cleanup task skips a run whose delay is 0.
            if isinstance(cleanup_delay_hours, bool) or not isinstance(cleanup_delay_hours, int) \
                    or cleanup_delay_hours < 1:
                raise ValueError("cleanup_delay_hours must be a whole number of hours, at least 1")
        exec_body = _exec_settings_body(exec_settings) if exec_settings is not None else {}
        if singularity_options is not None:
            if not singularity_options.strip():
                raise ValueError("singularity_options must not be empty")
            if run_environment_id is not None:
                raise ValueError(
                    "Save run_environment_id first, then singularity_options in a second update_run call: "
                    "the environment fills in the run's container image."
                )
        via_client = get_client()
        if permission is None:
            permission, group_id = _saved_permission(via_client, run_id, group_id)
        data = {}
        if inputs is not None:
            data["inputs"] = _normalize_run_inputs(inputs)
        if process_options is not None:
            data["processOptions"] = process_options
        data["permission"] = permission
        data["groupId"] = group_id
        optional = {
            "runEnvironmentId": run_environment_id,
            "launchDirectory": launch_directory,
            "publishDir": publish_dir,
            "name": name,
        }
        data.update({key: value for key, value in optional.items() if value is not None})
        data.update(exec_body)
        if cleanup_delay_hours is not None:
            data["deleteIntermediateCheck"] = True
            data["deleteIntermediateDelay"] = cleanup_delay_hours
        if singularity_options is not None:
            data.update(_singularity_body(via_client, run_id, singularity_options.strip()))
        saved = via_client.call(method="PATCH", endpoint=f"/api/v1/run/{run_id}/save", data=data)
        if saved:
            return json.dumps(saved, indent=2)
        # The server answers a successful save with an empty body.
        return json.dumps(envelope(
            summary=f"Saved run {run_id}.",
            data={"run_id": run_id, "saved": _saved_fields(data)},
            next_steps=["preflight_run(run_id) to check the run before launching it."],
        ), indent=2)
    except Exception as e:
        logger.error(f"Error updating run {run_id}: {e}")
        # Raised, not returned, so the MCP reply carries isError: a refused save
        # must never read as a successful one.
        raise ToolError(json.dumps({"error": str(e)})) from e


# ============================================================================
# Run advanced settings: container and executor settings
#
# GET /api/v1/run/{id}/details reports them under advancedSettings (docker,
# singularity, globalExec, processExec). PATCH /api/v1/run/{id}/save takes
# useDocker / useSingularity with dockerConfig {image, runOptions} and
# singularityConfig {imagePath, runOptions}, execAllCheck / execAll and
# execEachCheck / execEach. The server refuses useDocker and useSingularity
# both true, so a container change always sends both.
# ============================================================================

_CONTAINER_CHOICES = ("docker", "singularity", "off")
# Statuses a run has before it is launched (the database keeps null).
_ADVANCED_SETTINGS_EDITABLE = ("NotSubmitted", "", None)
_EXEC_NUMERIC_FIELDS = ("memory", "cpu", "time")


def _exec_row_view(options, keys):
    """One saved executor row as {queue, memory, cpu, time, options}."""
    row = {field: "" for field in _EXEC_FIELDS}
    for key, value in (options or {}).items():
        if key in keys and value is not None:
            row[keys[key]] = str(value)
    return row


def _advanced_settings_view(run_id, details, status):
    advanced = (details or {}).get("advancedSettings") or {}
    docker = advanced.get("docker") or {}
    singularity = advanced.get("singularity") or {}
    if docker.get("enabled"):
        container = "docker"
    elif singularity.get("enabled"):
        container = "singularity"
    else:
        container = "off"
    global_exec = advanced.get("globalExec") or {}
    process_exec = advanced.get("processExec") or {}
    processes = {key: _exec_row_view(row, _PROCESS_EXEC_KEYS)
                 for key, row in (process_exec.get("options") or {}).items() if isinstance(row, dict)}
    return {
        "run_id": run_id,
        "name": (details or {}).get("name"),
        "status": _human_run_status(status) if status not in _ADVANCED_SETTINGS_EDITABLE else "Not submitted",
        "editable": status in _ADVANCED_SETTINGS_EDITABLE,
        "container": {
            "type": container,
            "docker": {"enabled": bool(docker.get("enabled")), "image": docker.get("image") or "",
                       "options": docker.get("options") or ""},
            "singularity": {"enabled": bool(singularity.get("enabled")), "image": singularity.get("image") or "",
                            "options": singularity.get("options") or ""},
        },
        "exec_all": {"enabled": bool(global_exec.get("enabled")),
                     **_exec_row_view(global_exec.get("options"), _GLOBAL_EXEC_KEYS)},
        "process_exec": {"enabled": bool(process_exec.get("enabled")), "processes": processes},
    }


def _read_advanced_settings(via_client, run_id):
    details = via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/details") or {}
    if not details.get("advancedSettings"):
        raise ValueError(f"Run {run_id} has no advanced settings to read; check the run id with get_run.")
    status = _fetch_run_status(via_client, run_id, strict=True)
    return details, status


@mcp.tool()
def get_run_advanced_settings(run_id: str) -> str:
    """
    Read a run's advanced settings: the container (docker, singularity or
    off, with its image and run options), the executor settings for all
    processes (exec_all: queue, memory in GB, cpu, time in minutes, extra
    cluster options) and the per process overrides (process_exec, keyed
    procGnum-<n> or procGnum-<n>p<module>; get_run_details(run_id,
    verbose=True) shows which process each key is).

    `editable` is true only while the run has not been launched; that is the
    only time update_run_advanced_settings saves. Writes nothing. Call this
    before update_run_advanced_settings and show the user what will change.
    """
    try:
        via_client = get_client()
        details, status = _read_advanced_settings(via_client, run_id)
        view = _advanced_settings_view(run_id, details, status)
    except Exception as e:
        logger.error(f"Error reading advanced settings for run {run_id}: {e}")
        raise ToolError(json.dumps({"error": str(e)})) from e
    exec_all = view["exec_all"]
    summary = (
        f"Run {run_id}: container {view['container']['type']}; executor settings for all processes "
        + (f"on (queue {exec_all['queue'] or 'default'}, {exec_all['memory'] or '?'} GB, "
           f"{exec_all['cpu'] or '?'} CPU, {exec_all['time'] or '?'} min)" if exec_all["enabled"] else "off")
        + f"; {len(view['process_exec']['processes'])} per process override(s)"
        + ("" if view["process_exec"]["enabled"] or not view["process_exec"]["processes"] else " (switched off)")
        + "."
    )
    next_steps = ([f"update_run_advanced_settings(run_id='{run_id}', ..., confirm_update=true) after the user agrees."]
                  if view["editable"] else
                  [f"Run {run_id} is {view['status']}, so its settings cannot be changed. "
                   "duplicate_run makes a new run to edit."])
    return json.dumps(envelope(summary=summary, data=view, next_steps=next_steps), indent=2)


def _check_exec_values(row, where):
    for field in _EXEC_NUMERIC_FIELDS:
        value = row.get(field, "")
        if value != "" and not re.fullmatch(r"\d+(\.\d+)?", value):
            raise ValueError(f"{where}.{field} must be a number, not '{value}'")


def _exec_update(current, changes, keys, where):
    """Merge a partial executor row onto the saved one."""
    if not isinstance(changes, dict):
        raise ValueError(f"{where} must be an object with queue, memory, cpu, time and/or options")
    if not changes:
        raise ValueError(f"{where} is empty; leave it out to keep the saved values")
    given = _exec_values(changes, keys, where)
    merged = dict(current)
    merged.update({keys[k]: given[keys[k]] for k in changes if k != "check"})
    _check_exec_values(merged, where)
    return merged


def _validate_advanced_settings_args(container, container_image, container_options, exec_all, process_exec,
                                     exec_all_enabled, process_exec_enabled):
    if all(v is None for v in (container, container_image, container_options, exec_all, process_exec,
                               exec_all_enabled, process_exec_enabled)):
        raise ValueError("Nothing to change: pass container, exec_all, process_exec or one of the *_enabled switches.")
    if container is not None and container not in _CONTAINER_CHOICES:
        raise ValueError(f"container must be one of {', '.join(_CONTAINER_CHOICES)}, not '{container}'")
    if container == "off" and (container_image is not None or container_options is not None):
        raise ValueError("container 'off' takes no container_image or container_options")
    if container_image is not None and not container_image.strip():
        raise ValueError("container_image must not be empty")
    if exec_all is not None:
        _exec_update({f: "" for f in _EXEC_FIELDS}, exec_all, _GLOBAL_EXEC_KEYS, "exec_all")
    if process_exec is not None:
        if not isinstance(process_exec, dict) or not process_exec:
            raise ValueError("process_exec must be an object keyed by procGnum-<n> or procGnum-<n>p<module>")
        for key, row in process_exec.items():
            if not _PROCESS_EXEC_KEY.fullmatch(str(key)):
                raise ValueError(
                    f"process_exec key '{key}' must be procGnum-<n> or procGnum-<n>p<module>, "
                    "as get_run_advanced_settings reports it"
                )
            if row is not None:
                _exec_update({f: "" for f in _EXEC_FIELDS}, row, _PROCESS_EXEC_KEYS, f"process_exec['{key}']")


def _container_body(view, container, container_image, container_options):
    if container is None:
        container = view["container"]["type"]
        if container == "off":
            raise ValueError("The run uses no container, so container_image and container_options have "
                             "nothing to apply to; pass container 'docker' or 'singularity' too.")
    if container == "off":
        return {"useDocker": False, "useSingularity": False}
    saved = view["container"][container]
    image = (container_image if container_image is not None else saved["image"]).strip()
    if not image:
        raise ValueError(f"The run has no saved {container} image; pass container_image.")
    options = container_options if container_options is not None else saved["options"]
    if container == "docker":
        return {"useDocker": True, "useSingularity": False,
                "dockerConfig": {"image": image, "runOptions": options}}
    if container_options == "" and saved["options"]:
        # The server keeps the saved Singularity options when sent an empty string.
        raise ValueError("The server cannot clear Singularity run options; pass the options to keep instead.")
    return {"useDocker": False, "useSingularity": True,
            "singularityConfig": {"imagePath": image, "runOptions": options}}


@mcp.tool()
def update_run_advanced_settings(
    run_id: str,
    confirm_update: Annotated[bool, Strict()],
    container: str = None,
    container_image: str = None,
    container_options: str = None,
    exec_all: dict = None,
    exec_all_enabled: Optional[Annotated[bool, Strict()]] = None,
    process_exec: dict = None,
    process_exec_enabled: Optional[Annotated[bool, Strict()]] = None,
) -> str:
    """
    Change a run's container and executor settings before it is launched.

    WARNING: This edits the run in place on the Foundry Connect server. Call
    get_run_advanced_settings(run_id) first, show the user the current and
    new values, and set `confirm_update` to true only after they agree. It
    refuses a run that has been launched (any status other than not
    submitted). Foundry status lags behind the cluster, so do not call this
    right after initiate_run; use duplicate_run to change a launched run.

    Pass only what changes; everything else keeps its saved value.
    - container: "docker", "singularity" or "off". container_image and
      container_options set the image and run options (for example
      "-B /share:/share" for Singularity); left out, the saved ones are kept.
      Without `container` they apply to the container the run already uses.
    - exec_all: executor settings for all processes, any of queue, memory
      (GB), cpu, time (minutes) and options (extra cluster options), for
      example {"queue": "gpu", "memory": "8", "cpu": "1", "time": "30"}.
      Fields left out keep their saved values. Giving exec_all switches it on
      unless exec_all_enabled is false.
    - process_exec: per process overrides keyed procGnum-<n> or
      procGnum-<n>p<module>, each a row like exec_all; a row merges onto the
      saved one, and null removes that override. Giving process_exec switches
      overrides on while any remain, unless process_exec_enabled says otherwise.
    - exec_all_enabled and process_exec_enabled switch either block on or off
      without changing its values.

    Returns the settings before the save (`previous`) and as read back after
    it (`current`).
    """
    if confirm_update is not True:
        raise ToolError(json.dumps({
            "error": "confirm_update must be true. Show the user get_run_advanced_settings and the new "
                     "values, and save only after they agree."
        }))
    try:
        _validate_advanced_settings_args(container, container_image, container_options, exec_all, process_exec,
                                         exec_all_enabled, process_exec_enabled)
        via_client = get_client()
        details, status = _read_advanced_settings(via_client, run_id)
        previous = _advanced_settings_view(run_id, details, status)
        if not previous["editable"]:
            raise ValueError(
                f"Run {run_id} is {previous['status']}, so its settings were not changed. "
                "A save would rewrite a launched run's record; use duplicate_run and edit the new run."
            )
        permission = details.get("permission")
        if permission is None:
            raise ValueError(f"Run {run_id} has no saved permission, which the save endpoint requires.")
        data = {"permission": permission, "groupId": details.get("groupId")}
        if container is not None or container_image is not None or container_options is not None:
            data.update(_container_body(previous, container, container_image, container_options))
        if exec_all is not None:
            saved_all = {f: previous["exec_all"][f] for f in _EXEC_FIELDS}
            data["execAll"] = _exec_update(saved_all, exec_all, _GLOBAL_EXEC_KEYS, "exec_all")
            data["execAllCheck"] = True if exec_all_enabled is None else exec_all_enabled
        elif exec_all_enabled is not None:
            data["execAllCheck"] = exec_all_enabled
        if process_exec is not None:
            rows = {key: dict(row) for key, row in previous["process_exec"]["processes"].items()}
            removed = {}
            for key, row in process_exec.items():
                if row is None:
                    removed[key] = rows.pop(key, {f: "" for f in _EXEC_FIELDS})
                else:
                    rows[key] = _exec_update(rows.get(key, {f: "" for f in _EXEC_FIELDS}), row,
                                             _PROCESS_EXEC_KEYS, f"process_exec['{key}']")
            # The server drops a row sent with check false, and ignores an empty execEach,
            # so a removed override is sent switched off rather than left out.
            data["execEach"] = {**{key: {"check": False, **row} for key, row in removed.items()},
                                **{key: {"check": True, **row} for key, row in rows.items()}}
            data["execEachCheck"] = bool(rows) if process_exec_enabled is None else process_exec_enabled
        elif process_exec_enabled is not None:
            data["execEachCheck"] = process_exec_enabled
        logger.info(f"Saving advanced settings for run {run_id}: {sorted(data)}")
        via_client.call(method="PATCH", endpoint=f"/api/v1/run/{run_id}/save", data=data)
        after = via_client.call(method="GET", endpoint=f"/api/v1/run/{run_id}/details") or {}
        current = _advanced_settings_view(run_id, after, status)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error saving advanced settings for run {run_id}: {e}")
        raise ToolError(json.dumps({"error": str(e)})) from e
    return json.dumps(envelope(
        summary=f"Saved advanced settings for run {run_id}.",
        data={"run_id": run_id, "saved_fields": sorted(k for k in data if k not in ("permission", "groupId")),
              "previous": previous, "current": current},
        next_steps=["preflight_run(run_id) to check the run before launching it."],
    ), indent=2)


_VALID_RUN_TYPES = ("newrun", "resumerun", "rerun")


@mcp.tool()
def initiate_run(run_id: str, run_type: str = "newrun") -> str:
    """
    Start execution of a prepared run. run_type: 'newrun' (fresh), 'resumerun'
    (Nextflow -resume, reuses work dir/cache), or 'rerun' (new attempt, same
    params). Returns status, runUUID, localRunDir. When the run environment's
    cloud head node is stopped, it returns status "waking" without a runUUID:
    the node is starting and the run launches in the background once it is
    ready (check it with get_run). This LAUNCHES real HPC compute
    (it can take minutes to hours and consumes cluster time) — always confirm
    with the user before calling.
    """
    try:
        if run_type not in _VALID_RUN_TYPES:
            raise ValueError(
                f"runType must be one of {', '.join(_VALID_RUN_TYPES)}, "
                f"got '{run_type}'"
            )
        via_client = get_client()
        started = via_client.call(
            method="POST",
            endpoint="/api/v1/run/initiate-run",
            data={"runId": int(run_id), "runType": run_type},
        )
        return json.dumps(started, indent=2)
    except Exception as e:
        logger.error(f"Error initiating run {run_id}: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Pipeline Discovery Tools
# ============================================================================

_PIPELINE_LIST_ENDPOINT = "/api/v1/pipeline/"

# PipelineViewType.Released. The backend turns this into
# `pin = 'true' AND perms = Public` and orders pinned-first, then pinOrder, then
# newest — i.e. the admin-curated catalog, already ranked. NOTE the other view
# types (2 = MyPipelines, 3 = SharedWithMe) are ANDed together server-side, so
# passing "1,2" matches nothing; the parameter is single-valued in practice
# despite its "comma separated" description.
_PIPELINE_VIEW_RELEASED = "1"

# The route's Joi schema is take: min(1).max(100); out of range is a 400.
_PIPELINE_TAKE_MIN = 1
_PIPELINE_TAKE_MAX = 100

_PIPELINE_SUMMARY_CHARS = 300


def _truncate_summary(text, limit: int = _PIPELINE_SUMMARY_CHARS) -> str:
    """Shorten a pipeline blurb to `limit` chars, cutting between words so a
    scientist never reads a half-word."""
    if not text or len(text) <= limit:
        return text or ""
    clipped = text[: limit - 1]
    spaced = clipped.rsplit(" ", 1)[0]
    return (spaced or clipped).rstrip() + "…"


def _compact_pipeline(row):
    """Reduce a raw pipeline-list row to what a scientist needs to choose.

    Drops the window-function `totalCount` leak, the `pin`/`pinOrder` curation
    machinery, and `aiEntity`. Summaries arrive HTML-encoded from this endpoint
    (unlike GET /v1/pipeline/{id}, which decodes), so decode them here.
    """
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "summary": _truncate_summary(_strip_html(row.get("summary"))),
        "version": row.get("version"),
        "tags": [t.get("name") for t in (row.get("tags") or []) if t.get("name")],
    }


def _fetch_featured_pipelines(via_client, search: str = "", limit: int = 20):
    """Fetch the curated (Released) pipeline catalog. Returns (rows, total)."""
    take = max(_PIPELINE_TAKE_MIN, min(int(limit), _PIPELINE_TAKE_MAX))
    params = {"type": _PIPELINE_VIEW_RELEASED, "take": take, "skip": 0}
    if search and search.strip():
        params["searchKeyword"] = search.strip()
    response = via_client.call(
        method="GET", endpoint=_PIPELINE_LIST_ENDPOINT, params=params
    )
    if isinstance(response, list):
        return response, len(response)
    rows = response.get("data", []) if isinstance(response, dict) else []
    total = response.get("total", len(rows)) if isinstance(response, dict) else len(rows)
    return rows, total


@mcp.tool()
def list_projects(name: str = "", limit: int = 20) -> str:
    """
    List the projects you own, newest first. Use a project id with create_run.
    If none fits, offer create_project.

    Args:
        name: Optional text matched against the project name.
        limit: How many to return (1-100, default 20).
    """
    try:
        via_client = get_client()
        params = {"type": "1", "take": max(1, min(int(limit), 100))}
        if name.strip():
            params["name"] = name.strip()
        page = via_client.call(method="GET", endpoint="/api/v1/project/", params=params)
        projects = [{"id": p.get("id"), "name": p.get("name"), "owner": p.get("owner")} for p in page.get("data", [])]
        result = envelope(
            summary=f"{len(projects)} of {page.get('total', len(projects))} projects.",
            data={"projects": projects, "total": page.get("total")},
            next_steps=["Pick a project id for create_run, or create_project(name) after the user confirms."],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing projects: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_project(name: str, summary: str = "") -> str:
    """
    Create a new private project owned by you. This writes; confirm the name
    with the user before calling.

    Args:
        name: Project name (required).
        summary: Optional one line description.
    """
    try:
        if not name or not name.strip():
            raise ValueError("A project name is required.")
        via_client = get_client()
        data = {"name": name.strip()}
        if summary:
            data["summary"] = summary
        project = via_client.call(method="POST", endpoint="/api/v1/project/", data=data)
        # The server may answer with the id alone; the name is the one we sent.
        project_name = project.get("name") or data["name"]
        result = envelope(
            summary=f"Created project {project_name} (id {project.get('id')}).",
            data={"project": {"id": project.get("id"), "name": project_name}},
            next_steps=["list_pipelines(search) to choose a pipeline, then create_run(pipeline_id, project_id, name)."],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating project: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_pipelines(search: str = "", limit: int = 20) -> str:
    """
    Search every pipeline you can read (released, public, your own and shared
    with your groups). For the short curated list use list_featured_pipelines.

    Args:
        search: Optional keyword matched against pipeline names and content.
        limit: How many to return (1-100, default 20).
    """
    try:
        via_client = get_client()
        take = max(1, min(int(limit), 100))
        # The server ANDs the views in a multi value `type`, so a user who owns
        # nothing gets an empty list for "1,2,3". Ask each view alone and merge:
        # 1 released, 2 my pipelines, 3 shared with me.
        pipelines, seen, total = [], set(), 0
        for view_type in ("1", "2", "3"):
            params = {"type": view_type, "take": take}
            if search.strip():
                params["searchKeyword"] = search.strip()
            page = via_client.call(method="GET", endpoint="/api/v1/pipeline/", params=params)
            total += page.get("total") or 0
            for p in page.get("data", []):
                if p.get("id") in seen:
                    continue
                seen.add(p.get("id"))
                pipelines.append(
                    {"id": p.get("id"), "name": p.get("name"), "version": p.get("version"), "summary": p.get("summary")}
                )
        pipelines = pipelines[:take]
        result = envelope(
            summary=f"{len(pipelines)} of about {max(total, len(pipelines))} pipelines.",
            data={"pipelines": pipelines, "total": max(total, len(pipelines))},
            next_steps=["create_run(pipeline_id, project_id, name) after the user confirms the pipeline and project."],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing pipelines: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_featured_pipelines(search: str = "", limit: int = 20) -> str:
    """
    Show the curated, ready-to-run pipelines — the right starting point when a
    scientist does not already know which pipeline they want. This is a short
    admin-blessed catalog (RNA-seq, ATAC-seq, ChIP-seq, single-cell, variant
    calling, ...), NOT the full pipeline list, and it is already ranked.

    Prefer this over list_all_processes for discovery. If the user describes a
    GOAL rather than a pipeline name ("I want differential expression from mouse
    RNA-seq"), call recommend_pipeline(goal) instead.

    Args:
        search: Optional keyword matched against pipeline name and description.
        limit: How many to return (1-100, default 20).
    """
    try:
        via_client = get_client()
        logger.info(f"Listing featured pipelines (search: '{search}', limit: {limit})")
        rows, total = _fetch_featured_pipelines(via_client, search, limit)
        pipelines = [_compact_pipeline(row) for row in rows]

        if search and not pipelines:
            result = envelope(
                summary=(
                    f"No curated pipeline matches '{search}'. The catalog is "
                    "searched by name and description only."
                ),
                data={"pipelines": [], "total": total},
                next_steps=[
                    "Call list_featured_pipelines() without a search term to "
                    "browse the whole curated catalog.",
                    "Describe the experiment instead and call "
                    "recommend_pipeline(goal) — it matches on intent, not spelling.",
                ],
            )
            return json.dumps(result, indent=2)

        scope = f" matching '{search}'" if search else ""
        shown = (
            f"Showing {len(pipelines)} of {total}"
            if total > len(pipelines)
            else f"{len(pipelines)}"
        )
        result = envelope(
            summary=f"{shown} curated pipelines{scope}.",
            data={"pipelines": pipelines, "total": total},
            next_steps=[
                "Not sure which one fits? Describe the experiment and call "
                "recommend_pipeline(goal).",
                "Once a pipeline is chosen, call plan_run(pipeline_id) to see "
                "the handful of decisions that actually need answering.",
            ],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing featured pipelines: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Goal -> pipeline matching
# ---------------------------------------------------------------------------

# Words that carry no signal because they appear in almost every goal or almost
# every pipeline blurb. "seq" is here because every entry in the catalog is
# some flavour of -seq.
_GOAL_STOPWORDS = frozenset("""
a about all also an analyse analysis analyze analyzing am and any anything are
as at be before between but by check could data dataset datasets did do does
doing done else find for from get got had has have how i if in into is it its
just like look make me my need no of on or our out over please pipeline
pipelines ran run running runs sample samples seq sequencing set should show so
some study than that the their them then there these they this to up us use
used using want was we well what when where whether which while will with
within would you your workflow workflows experiment experiments
""".split())

# Phrases a bench scientist actually says, mapped to terms that really occur in
# the curated catalog's names and blurbs. This is the domain knowledge the
# backend's substring search does not have: nothing in the catalog contains the
# words "chromatin accessibility", yet that is exactly what ATAC-seq measures.
_GOAL_HINTS = (
    (("differential expression", "differentially expressed", "differential gene",
      "deseq", "edger", "de genes", "degs", "upregulated", "downregulated",
      "fold change"),
     ("rna seq", "differential", "expression", "deseq")),
    (("single cell", "scrna", "10x", "cell ranger", "cellranger", "cell atlas"),
     ("single cell", "cell ranger", "10x", "scrna")),
    (("chromatin accessibility", "open chromatin", "accessible chromatin", "atac"),
     ("atac", "open chromatin")),
    (("transcription factor", "histone", "peak calling", "binding site", "bind",
      "chip seq", "chipseq", "cut and tag", "cut and run", "occupancy"),
     ("chip seq", "peaks", "cut and tag")),
    (("variant", "snps", "snp", "mutation", "germline", "somatic", "indel",
      "genotyp"),
     ("variant calling", "variants", "gatk")),
    (("methylation", "bisulfite", "methylome"), ("methyl",)),
    (("quality", "qc", "fastq", "contamination", "adapter"),
     ("fastqc", "quality control")),
    (("crispr", "knockout screen", "guide rna", "sgrna", "mageck"),
     ("crispr", "mageck", "screen")),
    (("de novo", "transcriptome assembly", "assemble"), ("trinity", "de novo")),
    (("ribosome profiling", "ribo seq", "riboseq", "translation"),
     ("riboseq", "ribo seq")),
    (("alternative splicing", "splicing", "isoform"), ("splic", "isoform")),
    (("microbiome", "16s", "metagenom", "taxonomic"), ("metagenom", "16s")),
    (("bulk rna", "rna seq", "rnaseq", "transcriptom", "gene expression"),
     ("rna seq", "expression")),
)

# A hint term found in the NAME is near-proof; in the blurb it is suggestive.
# Literal words from the user's own goal rank below curated domain knowledge.
_SCORE_HINT_IN_NAME = 8
_SCORE_HINT_IN_SUMMARY = 3
_SCORE_TOKEN_IN_NAME = 4
_SCORE_TOKEN_IN_TAG = 2
_SCORE_TOKEN_IN_SUMMARY = 1

# Below this, a "match" is one incidental word in a blurb. Saying "nothing fits"
# is more useful to a scientist than confidently naming the wrong pipeline.
_RECOMMEND_MIN_SCORE = 4
_RECOMMEND_MAX = 5


def _normalize_words(text) -> str:
    """Reduce text to space-delimited lowercase words, padded with spaces so
    callers can test for a leading word boundary."""
    return " " + " ".join(re.findall(r"[a-z0-9]+", (text or "").lower())) + " "


# Below this length a term must match a whole word. Live regression: the
# 3-letter token "ran" (from "I ran a CRISPR screen") prefix-matched "Ranger"
# and put Cell Ranger in the results.
_SUFFIX_MATCH_MIN_LEN = 4


def _matches_word(padded_haystack: str, term: str) -> bool:
    """True when `term` occurs in `padded_haystack` starting on a word boundary.

    Longer terms may match a prefix ("variant" hits "variants", "methyl" hits
    "methylation") — that suffix tolerance is what makes plain-language goals
    work at all. Nothing may match a SUFFIX, and that asymmetry is load-bearing:
    plain substring matching ranks "tRNA-Seq Pipeline" as a top hit for an
    RNA-seq goal, because "rna-seq" is literally inside "tRNA-Seq".
    """
    normalized = _normalize_words(term)
    needle = (
        normalized.rstrip()
        if len(term) >= _SUFFIX_MATCH_MIN_LEN
        else normalized
    )
    return needle in padded_haystack


def _goal_terms(goal: str):
    """Split a stated goal into (hint terms, literal tokens).

    Hint terms come from the domain map above and encode what an assay actually
    measures; tokens are the user's own words minus filler.
    """
    padded_goal = _normalize_words(goal)
    hints = []
    for triggers, terms in _GOAL_HINTS:
        if any(_matches_word(padded_goal, t) for t in triggers):
            hints.extend(t for t in terms if t not in hints)
    tokens = [
        w for w in dict.fromkeys(padded_goal.split())
        if w not in _GOAL_STOPWORDS and len(w) > 1
    ]
    return hints, tokens


def _score_pipeline(row, hints, tokens):
    """Score one pipeline against a goal. Returns (score, reason).

    Deliberately transparent: every point traces to a named term, so the model
    can tell the scientist WHY a pipeline was suggested instead of asserting it.
    """
    name = _normalize_words(row.get("name"))
    summary = _normalize_words(_strip_html(row.get("summary")))
    tags = _normalize_words(" ".join(
        t.get("name") or "" for t in (row.get("tags") or [])))

    score = 0
    name_hits, summary_hits = [], []

    for term in hints:
        if _matches_word(name, term):
            score += _SCORE_HINT_IN_NAME
            name_hits.append(term)
        elif _matches_word(summary, term):
            score += _SCORE_HINT_IN_SUMMARY
            summary_hits.append(term)

    for token in tokens:
        if _matches_word(name, token):
            score += _SCORE_TOKEN_IN_NAME
            if token not in name_hits:
                name_hits.append(token)
        elif _matches_word(summary, token):
            score += _SCORE_TOKEN_IN_SUMMARY
            if token not in summary_hits:
                summary_hits.append(token)
        if _matches_word(tags, token):
            score += _SCORE_TOKEN_IN_TAG

    parts = []
    if name_hits:
        parts.append("name matches " + ", ".join(name_hits[:4]))
    if summary_hits:
        parts.append("description mentions " + ", ".join(summary_hits[:4]))
    return score, "; ".join(parts) or "keyword overlap"


def _find_example_run(via_client, pipeline_id, strict: bool = False):
    """The most recent SUCCESSFUL run on a pipeline — the thing worth cloning.

    Best-effort by default: a scientist is better served by a recommendation
    with no example than by no recommendation at all, so failures degrade to
    None. Pass strict=True where the lookup IS the input rather than a garnish
    — swallowing an outage there would report "this pipeline has no successful
    run", blaming the data for an infrastructure failure.
    """
    try:
        response = via_client.call(
            method="POST",
            endpoint="/api/v1/run/list",
            params={
                "take": 1, "skip": 0, "sort": "dateCreated", "order": "desc",
                # Pipe-grouped so one request matches either backend vocabulary.
                "filter": f"pipelineId:eq={pipeline_id},status:eq=Completed|NextSuc",
            },
            data={},
        )
        rows = response.get("data", []) if isinstance(response, dict) else response
        if not rows:
            return None
        run = rows[0]
        return {"id": run.get("id"), "name": run.get("name"),
                "dateCreated": run.get("dateCreated")}
    except Exception as e:
        if strict:
            raise
        logger.warning(f"No example run for pipeline {pipeline_id}: {e}")
        return None


@mcp.tool()
def recommend_pipeline(goal: str, limit: int = 3) -> str:
    """
    Suggest which curated pipeline fits a stated scientific GOAL, with a reason
    for each and a working past run to clone. Use this whenever the user
    describes what they want to LEARN rather than naming a pipeline — e.g.
    "I have mouse RNA-seq and want differential expression", "where does my
    transcription factor bind", "I want to look at chromatin accessibility".

    Understands assay language the catalog never spells out (chromatin
    accessibility -> ATAC-seq, SNPs -> variant calling). If nothing plausibly
    matches it says so rather than guessing.

    Args:
        goal: The experiment or question, in the scientist's own words.
        limit: How many suggestions to return (1-5, default 3).
    """
    try:
        via_client = get_client()
        logger.info(f"Recommending a pipeline for goal: '{goal}'")
        hints, tokens = _goal_terms(goal)

        # Score the whole curated catalog locally. Server-side search cannot do
        # this job: it is substring-only and orders by pin position, not
        # relevance (live, searchKeyword=atac ranks Cell Ranger Count above
        # ATAC-seq Pipeline).
        rows, _total = _fetch_featured_pipelines(via_client, limit=_PIPELINE_TAKE_MAX)

        scored = []
        if hints or tokens:
            for row in rows:
                score, reason = _score_pipeline(row, hints, tokens)
                if score >= _RECOMMEND_MIN_SCORE:
                    scored.append((score, row, reason))
        scored.sort(key=lambda s: s[0], reverse=True)
        top = scored[: max(1, min(int(limit), _RECOMMEND_MAX))]

        if not top:
            result = envelope(
                summary=(
                    f"No curated pipeline clearly matches '{goal}', so here is "
                    "nothing rather than a bad guess."
                ),
                data={"recommendations": [], "goal": goal},
                next_steps=[
                    "Call list_featured_pipelines() to browse the curated "
                    "catalog and pick by eye.",
                    "Or restate the goal in terms of the assay or measurement "
                    "— e.g. 'differential expression from bulk RNA-seq', "
                    "'open chromatin', 'where a transcription factor binds'.",
                ],
            )
            return json.dumps(result, indent=2)

        recommendations = []
        for score, row, reason in top:
            entry = _compact_pipeline(row)
            entry["score"] = score
            entry["reason"] = reason
            entry["example_run"] = _find_example_run(via_client, row.get("id"))
            recommendations.append(entry)

        best = recommendations[0]
        next_steps = [
            f"Call plan_run(pipeline_id={best['id']}) to see the handful of "
            "decisions that actually need answering."
        ]
        if best["example_run"]:
            next_steps.append(
                f"Run {best['example_run']['id']} is a working example on this "
                f"pipeline — get_run_details(run_id='{best['example_run']['id']}', "
                "verbose=True), then duplicate_run + update_run to adapt it, or "
                "create_run for a fresh run of the same pipeline."
            )
        next_steps.append(
            "Launching a run uses HPC compute — confirm with the user before "
            "initiate_run."
        )

        result = envelope(
            summary=(
                f"{len(recommendations)} pipeline(s) fit '{goal}'. "
                f"Best match: '{best['name']}' — {best['reason']}."
            ),
            data={"recommendations": recommendations, "goal": goal},
            next_steps=next_steps,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error recommending a pipeline for '{goal}': {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Watching a run in flight
# ---------------------------------------------------------------------------

# Statuses from which nothing further will happen. Used to tell "logs have not
# synced yet" apart from "logs are gone".
_TERMINAL_RUN_STATUSES = (
    # Canonical vocabulary, backend 2.20.0 and later.
    "Completed",
    "Failed",
    "LaunchFailed",
    "Terminated",
    # Legacy vocabulary, backends before 2.20.0.
    "NextSuc",
    "NextErr",
    "Error",
)

_TRACE_FILE = "trace.txt"

# Nextflow task states. CACHED means the task was reused from a previous run —
# it is DONE, and counting it as outstanding would badly understate progress.
_TRACE_DONE = ("COMPLETED", "CACHED")
_TRACE_FAILED = ("FAILED", "ABORTED")
_TRACE_RUNNING = ("RUNNING", "SUBMITTED", "PENDING")

_WATCH_MAX_LISTED = 5


def _fetch_run_status(via_client, run_id, strict: bool = False):
    """The raw RunStatus string, or None if it cannot be read.

    strict=True where the status IS the answer rather than a refinement of one:
    swallowing an outage there reports "Connecting, no progress available",
    which blames the run for an infrastructure failure.
    """
    try:
        response = via_client.call(
            method="GET", endpoint=f"/api/v1/run/{run_id}/status")
        if isinstance(response, dict):
            return response.get("runStatus")
    except Exception as e:
        if strict:
            raise
        logger.info(f"Could not read status for run {run_id}: {e}")
    return None


def _parse_trace(text):
    """Parse a Nextflow trace.txt into per-task progress.

    The status endpoint only returns a flat label, so this is the only source
    of "which step is it on". Returns None when there is no usable trace.
    """
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    header = lines[0].split("\t")
    try:
        i_name, i_status = header.index("name"), header.index("status")
    except ValueError:
        return None
    i_exit = header.index("exit") if "exit" in header else None

    done = failed = running = 0
    failed_tasks, running_now = [], []
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) <= i_status:
            continue
        name, status = cells[i_name], cells[i_status].strip().upper()
        if status in _TRACE_DONE:
            done += 1
        elif status in _TRACE_FAILED:
            failed += 1
            entry = {"name": name, "status": status}
            if i_exit is not None and len(cells) > i_exit:
                entry["exit"] = cells[i_exit]
            failed_tasks.append(entry)
        elif status in _TRACE_RUNNING:
            running += 1
            running_now.append(name)

    total = done + failed + running
    if not total:
        return None
    return {
        "total_tasks": total,
        "finished": done,
        "failed": failed,
        "running": running,
        "percent_complete": round((done / total) * 100) if total else 0,
        "running_now": running_now[:_WATCH_MAX_LISTED],
        "failed_tasks": failed_tasks[:_WATCH_MAX_LISTED],
    }


@mcp.tool()
def watch_run(run_id: str) -> str:
    """
    Report how far a run has actually got — how many steps are done, which step
    is running right now, and which have failed — not just its overall status
    label.

    Use this when a user asks "is it done yet", "how far along is it", or "what
    is it doing". MCP tools cannot poll on their own, so when a run is still
    going this returns check_again=true; call it again after a while rather
    than assuming nothing changed.

    Args:
        run_id: The run to check.
    """
    try:
        via_client = get_client()
        logger.info(f"Watching run {run_id}")
        status = _fetch_run_status(via_client, run_id, strict=True)
        human = _human_run_status(status)

        progress = None
        try:
            logs = via_client.call(
                method="GET", endpoint=f"/api/v1/run/{run_id}/logs", params=None)
            if isinstance(logs, dict) and "logs" in logs:
                logs = logs["logs"]
            trace = next((l for l in (logs or [])
                          if isinstance(l, dict) and l.get("name") == _TRACE_FILE),
                         None)
            if trace:
                progress = _parse_trace(trace.get("content"))
        except Exception as e:
            logger.info(f"No trace available for run {run_id}: {e}")

        finished = status in _TERMINAL_RUN_STATUSES
        check_again = not finished

        if progress:
            summary = (
                f"Run {run_id} is {human}: {progress['finished']} of "
                f"{progress['total_tasks']} steps done "
                f"({progress['percent_complete']}%)")
            if progress["failed_tasks"]:
                names = ", ".join(t["name"] for t in progress["failed_tasks"])
                summary += f". Failed at {names}"
            elif progress["running_now"]:
                summary += f". Currently running {', '.join(progress['running_now'])}"
            summary += "."
        else:
            summary = (f"Run {run_id} is {human}. No step-level progress is "
                       f"available yet.")

        if progress and progress["failed_tasks"]:
            next_steps = [
                f"See the error: get_run_log(run_id='{run_id}').",
                f"Check the configuration for a cause: "
                f"preflight_run(run_id='{run_id}').",
            ]
        elif check_again:
            next_steps = [
                f"Still going — call watch_run(run_id='{run_id}') again in a "
                f"few minutes rather than assuming it is stuck.",
                "Nothing needs doing while it runs.",
            ]
        elif status in ("Completed", "NextSuc"):
            next_steps = [
                f"Finished — call summarize_results(run_id='{run_id}') for what "
                f"it found.",
            ]
        else:
            next_steps = [f"See what happened: get_run_log(run_id='{run_id}')."]

        result = envelope(
            summary=summary,
            data={"run_id": run_id, "status": human, "raw_status": status,
                  "check_again": check_again, "progress": progress},
            next_steps=next_steps,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error watching run {run_id}: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

# A run produces one DESeq2 directory per quantifier (RSEM, Kallisto, Salmon,
# STAR_Salmon, STAR_featurecounts). Reporting five near-identical answers is its
# own noise problem, so results are summarised across them.
_DE_SIG_SUFFIX = "_sig_deseq2_results.tsv"
_DE_DIR_PREFIX = "DESeq2_"
_QC_SUMMARY_PATH = "summary/overall_summary.tsv"

_TOP_GENES = 3

# Reading these into a chat context would be slow and useless: the DESeq2 HTML
# is ~1.4 MB and the MultiQC report ~4.8 MB.
_RESULT_SKIP_EXTENSIONS = ("html", "rmd", "pdf", "png", "jpg", "jpeg", "svg")

# A sample this far below the median read count is worth mentioning.
_QC_LOW_READ_FRACTION = 0.25


def _report_files(via_client, run_id):
    """Every file in a run's report, as plain dict rows.

    Always use each row's `file_path` verbatim — real outputs live under
    `<dir>/outputs/` and inputs under `<dir>/inputs/`, so building a path from
    the directory and file name produces something `load_file` cannot resolve.
    """
    report = via_client.reports.fetch_report_data(run_id)
    files = via_client.reports.get_all_files(report)
    rows = files.to_dict(orient="records") if hasattr(files, "to_dict") else list(files)
    return report, rows


def _load_table(via_client, report, file_path):
    """Load a tabular result file as a list of dict rows, or None."""
    try:
        content = via_client.reports.load_file(report, file_path, sep="\t")
    except Exception as e:
        logger.info(f"Could not load {file_path}: {e}")
        return None
    if hasattr(content, "to_dict"):
        return content.to_dict(orient="records")
    return None


def _quantifier_from_path(file_path: str) -> str:
    """`DESeq2_RSEM/outputs/x.tsv` -> `RSEM`."""
    directory = (file_path or "").split("/", 1)[0]
    if directory.startswith(_DE_DIR_PREFIX):
        return directory[len(_DE_DIR_PREFIX):] or directory
    return directory


def _comparison_from_name(name: str) -> str:
    """`control_vs_exper_sig_deseq2_results.tsv` -> `control_vs_exper`."""
    base = (name or "").rsplit("/", 1)[-1]
    return base[: -len(_DE_SIG_SUFFIX)] if base.endswith(_DE_SIG_SUFFIX) else base


def _summarize_de_table(rows):
    """Count significant genes and pick the strongest movers in each direction."""
    genes = [r for r in rows or [] if r.get("gene") is not None]

    def _fc(row):
        try:
            return float(row.get("log2FoldChange") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _trim(row):
        entry = {"gene": row.get("gene"), "log2FoldChange": _fc(row)}
        if row.get("padj") is not None:
            entry["padj"] = row["padj"]
        return entry

    up = sorted((g for g in genes if _fc(g) > 0), key=_fc, reverse=True)
    down = sorted((g for g in genes if _fc(g) < 0), key=_fc)
    return {
        "significant_genes": len(genes),
        "top_up": [_trim(g) for g in up[:_TOP_GENES]],
        "top_down": [_trim(g) for g in down[:_TOP_GENES]],
        "all_genes": [g.get("gene") for g in genes],
    }


def _summarize_qc(rows):
    """Turn the per-sample alignment table into a one-line verdict."""
    if not rows:
        return None

    def _num(row, *keys):
        for key in keys:
            value = row.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return 0.0

    totals, aligned, per_sample = 0.0, 0.0, []
    for row in rows:
        total = _num(row, "Total Reads")
        # Any aligner column will do for a headline rate; STAR is the default.
        unique = _num(row, "Unique Reads Aligned (STAR)",
                      "Unique Reads Aligned (HISAT2)", "Unique Aligned Reads (RSEM)")
        totals += total
        aligned += unique
        per_sample.append({"sample": row.get("Sample"), "total_reads": int(total),
                           "aligned_reads": int(unique)})

    warnings = []
    read_counts = sorted(s["total_reads"] for s in per_sample)
    if read_counts:
        median = read_counts[len(read_counts) // 2]
        thin = [s["sample"] for s in per_sample
                if median and s["total_reads"] < median * _QC_LOW_READ_FRACTION]
        if thin:
            warnings.append(
                f"Unusually few reads compared with the other samples: "
                f"{', '.join(str(t) for t in thin)}.")

    rate = round((aligned / totals) * 100, 1) if totals else 0.0
    if totals and rate < 50:
        warnings.append(f"Overall alignment rate is low ({rate}%).")

    return {"samples": len(per_sample), "total_reads": int(totals),
            "aligned_reads": int(aligned), "alignment_rate_pct": rate,
            "per_sample": per_sample, "warnings": warnings}


@mcp.tool()
def summarize_results(run_id: str) -> str:
    """
    Report what a finished run actually FOUND, in a scientist's terms: how many
    genes came out differentially expressed for each comparison, the strongest
    movers in each direction, whether the quantifiers agree, and an alignment/QC
    verdict.

    Use this the moment a run completes, instead of listing report files. It
    reads only the small result tables — never the multi-megabyte HTML reports.

    Args:
        run_id: The finished run (same as the report id).
    """
    try:
        via_client = get_client()
        logger.info(f"Summarizing results for run {run_id}")
        report, rows = _report_files(via_client, run_id)

        de_results = []
        for row in rows:
            path = row.get("file_path") or ""
            if not path.endswith(_DE_SIG_SUFFIX) or "/inputs/" in path:
                continue
            table = _load_table(via_client, report, path)
            if table is None:
                continue
            entry = {"quantifier": _quantifier_from_path(path),
                     "comparison": _comparison_from_name(path),
                     "file_path": path}
            entry.update(_summarize_de_table(table))
            de_results.append(entry)

        qc_row = next((r for r in rows
                       if (r.get("file_path") or "") == _QC_SUMMARY_PATH), None)
        qc = _summarize_qc(_load_table(via_client, report, _QC_SUMMARY_PATH)) if qc_row else None

        # Where do the quantifiers agree? Genes found by all of them are the
        # ones worth leading with.
        gene_sets = [set(d["all_genes"]) for d in de_results if d["all_genes"]]
        shared = sorted(set.intersection(*gene_sets)) if gene_sets else []
        agreement = {
            "counts_by_quantifier": {d["quantifier"]: d["significant_genes"]
                                     for d in de_results},
            "genes_in_all_quantifiers": shared,
        }
        for entry in de_results:
            entry.pop("all_genes", None)

        if not de_results:
            summary = (
                f"Run {run_id} produced no differential-expression results to "
                f"summarize.")
        else:
            best = max(de_results, key=lambda d: d["significant_genes"])
            if best["significant_genes"] == 0:
                summary = (
                    f"Run {run_id} completed and found no genes significantly "
                    f"differentially expressed in {best['comparison']}.")
            else:
                lead = (best["top_up"] or best["top_down"])[0]
                summary = (
                    f"Run {run_id}: {best['significant_genes']} significant "
                    f"gene(s) in {best['comparison']} ({best['quantifier']}), "
                    f"led by {lead['gene']} "
                    f"(log2FC {lead['log2FoldChange']:+.2f}).")
        if qc:
            summary += (f" {qc['samples']} sample(s), "
                        f"{qc['alignment_rate_pct']}% aligned.")

        next_steps = [
            f"To open these results in a viewer, call suggest_apps(run_id='{run_id}').",
            f"For the full file inventory, call list_results(run_id='{run_id}').",
        ]
        if qc and qc["warnings"]:
            next_steps.insert(0, "Check the QC warnings above before "
                                 "interpreting the results.")

        result = envelope(
            summary=summary,
            data={"run_id": run_id, "differential_expression": de_results,
                  "agreement": agreement, "qc": qc},
            next_steps=next_steps,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error summarizing results for run {run_id}: {e}")
        return json.dumps({"error": str(e)})


# --- Grouping a run's outputs into something readable ----------------------

# Ordered: the first rule whose test matches wins, so the specific ones lead.
_RESULT_GROUPS = (
    ("Differential expression", ("deseq2_results", "_des.html", "limmavoom",
                                 "sig_deseq2", "all_deseq2")),
    ("Quality control", ("multiqc", "overall_summary", "fastqc", "rseqc",
                         "_qc.", "metrics")),
    ("Quantification", ("expression", "counts", "count.tsv", "abundance",
                        "quant", "tpm", "fpkm")),
    ("Alignments and coverage", (".bam", ".bai", ".bw", ".bigwig", ".tdf",
                                 ".cram")),
    ("Single-cell objects", (".h5ad", ".rds", ".loom", ".h5")),
)
_RESULT_GROUP_OTHER = "Other outputs"

# Intermediates a scientist never asks for: the notebook source a report was
# rendered from, and the inputs directory (what went IN, not a result).
_RESULT_HIDE_EXTENSIONS = ("rmd", "nf", "config", "log", "yaml", "yml")

_APP_RULES = (
    # (app name fragment, matching file suffixes, why)
    ("gsea", ("deseq2_results.tsv",),
     "differential-expression results can be explored as gene sets"),
    ("cellxgene", (".h5ad",), "single-cell objects open directly"),
    ("igv", (".bam", ".bw", ".bigwig", ".tdf", ".cram"),
     "alignments and coverage tracks can be browsed against the genome"),
)


def _human_size(num_bytes) -> str:
    try:
        size = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _is_intermediate(row) -> bool:
    path = (row.get("file_path") or "").lower()
    extension = (row.get("extension") or "").lower()
    return "/inputs/" in path or extension in _RESULT_HIDE_EXTENSIONS


def _classify_result(row) -> str:
    haystack = (row.get("file_path") or "").lower()
    for group, needles in _RESULT_GROUPS:
        if any(needle in haystack for needle in needles):
            return group
    return _RESULT_GROUP_OTHER


@mcp.tool()
def list_results(run_id: str) -> str:
    """
    List a finished run's outputs grouped by what they ARE — differential
    expression, quality control, quantification, alignments — with readable
    sizes, instead of a flat list of route-paths.

    Each entry carries the exact `file_path` to hand to load_file or
    download_file. Intermediates (notebook sources, the inputs directory) are
    hidden and counted.

    Args:
        run_id: The finished run (same as the report id).
    """
    try:
        via_client = get_client()
        logger.info(f"Listing results for run {run_id}")
        _report, rows = _report_files(via_client, run_id)

        grouped, hidden = {}, 0
        for row in rows:
            if _is_intermediate(row):
                hidden += 1
                continue
            grouped.setdefault(_classify_result(row), []).append({
                "name": row.get("name"),
                "file_path": row.get("file_path"),
                "size": _human_size(row.get("fileSize")),
            })

        order = [g for g, _ in _RESULT_GROUPS] + [_RESULT_GROUP_OTHER]
        groups = [{"group": g, "files": grouped[g]} for g in order if g in grouped]
        shown = sum(len(g["files"]) for g in groups)

        result = envelope(
            summary=(
                f"Run {run_id} produced {shown} output file(s) across "
                f"{len(groups)} group(s); {hidden} intermediate file(s) hidden."
            ),
            data={"run_id": run_id, "groups": groups,
                  "hidden_intermediate_files": hidden},
            next_steps=[
                f"For the actual findings rather than the file list, call "
                f"summarize_results(run_id='{run_id}').",
                f"To open results in a viewer, call suggest_apps(run_id='{run_id}').",
                "Use a file's `file_path` verbatim with load_file or "
                "download_file.",
            ],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing results for run {run_id}: {e}")
        return json.dumps({"error": str(e)})


def _installed_apps(via_client):
    """The apps this instance actually has, as [{id, name}]."""
    response = via_client.call(method="GET", endpoint="/api/v1/app")
    if isinstance(response, dict):
        response = response.get("data", [])
    return [a for a in (response or []) if isinstance(a, dict) and a.get("id")]


@mcp.tool()
def suggest_apps(run_id: str) -> str:
    """
    Recommend which interactive app to open a run's results in, and with which
    file — differential expression into GSEA Explorer, single-cell objects into
    Cellxgene, alignments and coverage into IGV.

    Only apps actually installed on this instance are suggested, so a
    recommendation never points at an app that is not there. Launching is a
    separate, explicit step via launch_app.

    Args:
        run_id: The finished run (same as the report id).
    """
    try:
        via_client = get_client()
        logger.info(f"Suggesting apps for run {run_id}")
        _report, rows = _report_files(via_client, run_id)
        apps = _installed_apps(via_client)

        suggestions = []
        for fragment, suffixes, reason in _APP_RULES:
            app = next((a for a in apps
                        if fragment in (a.get("name") or "").lower()), None)
            if not app:
                continue
            matches = [r.get("file_path") for r in rows
                       if not _is_intermediate(r)
                       and (r.get("file_path") or "").lower().endswith(suffixes)]
            if not matches:
                continue
            suggestions.append({
                "app_id": app["id"], "app_name": app.get("name"),
                "reason": reason, "files": matches,
            })

        if not suggestions:
            summary = (
                f"Run {run_id} has no outputs matching an app installed on this "
                f"instance, so there is no viewer to suggest.")
            next_steps = [
                f"Call list_results(run_id='{run_id}') to see what the run "
                f"produced, and list_apps() for what is installed.",
            ]
        else:
            names = ", ".join(s["app_name"] for s in suggestions)
            summary = (f"Run {run_id} has results that open in: {names}.")
            next_steps = [
                f"To open one, call launch_app(app_id='{suggestions[0]['app_id']}') "
                f"and point it at the file listed above.",
                "Confirm with the user first — launching an app starts a "
                "container.",
            ]

        result = envelope(
            summary=summary,
            data={"run_id": run_id, "suggestions": suggestions},
            next_steps=next_steps,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error suggesting apps for run {run_id}: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Getting sample data in
# ---------------------------------------------------------------------------

# Extensions stripped when deriving a sample name, longest first so that
# ".fastq.gz" wins over ".gz".
_FASTQ_EXTENSIONS = (".fastq.gz", ".fq.gz", ".fastq", ".fq", ".gz")

# The mate markers seen in the wild. Each pattern must capture the sample stem
# in group 1 and the mate number in group 2. `.1.` / `_1.` are last because
# they are the loosest and would otherwise swallow "_R1_".
_MATE_PATTERNS = (
    re.compile(r"^(.*)_R([12])_\d+$"),   # sampleA_R1_001
    re.compile(r"^(.*)_R([12])$"),       # sampleA_R1
    re.compile(r"^(.*)\.R([12])$"),      # sampleA.R1
    re.compile(r"^(.*)_([12])$"),        # sampleB_1
    re.compile(r"^(.*)\.([12])$"),       # exper_rep3.1
)


def _strip_fastq_extension(basename: str) -> str:
    lowered = basename.lower()
    for ext in _FASTQ_EXTENSIONS:
        if lowered.endswith(ext):
            return basename[: -len(ext)]
    return basename


def _split_mate(path: str):
    """Return (directory, sample_stem, mate_number|None) for a fastq path."""
    directory, _, basename = path.rpartition("/")
    stem = _strip_fastq_extension(basename)
    for pattern in _MATE_PATTERNS:
        match = pattern.match(stem)
        if match:
            return directory, match.group(1), match.group(2)
    return directory, stem, None


def _pair_fastqs(files):
    """Group fastq paths into dataset rows, pairing R1/R2 automatically.

    Returns (rows, unpaired_sample_names). A file whose mate is absent still
    becomes a row — dropping data silently would be worse — but its name is
    reported, because a lone R1 is much more often a forgotten file than a
    deliberate single-end run.
    """
    grouped = {}
    for path in files:
        path = (path or "").strip()
        if not path:
            continue
        directory, stem, mate = _split_mate(path)
        # Key on the directory too: the same basename in two directories is two
        # samples, not a pair.
        entry = grouped.setdefault((directory, stem), {})
        entry[mate or "single"] = path

    rows, unpaired = [], []
    for (_directory, stem), mates in sorted(grouped.items(), key=lambda kv: kv[0][1]):
        first = mates.get("1") or mates.get("single")
        second = mates.get("2")
        if second and not mates.get("1"):
            # An R2 with no R1 — treat the file we have as the primary read.
            first, second = second, None
        if first and second:
            rows.append({"name": stem, "file1": first, "file2": second,
                         "file_layout": "pair"})
        else:
            if mates.get("1") or mates.get("2"):
                unpaired.append(stem)
            rows.append({"name": stem, "file1": first, "file2": "",
                         "file_layout": "single"})
    return rows, unpaired


def _resolve_canvas_id(via_client):
    """Pick a study-tracker canvas to attach dataset rows to."""
    response = via_client.call(
        method="POST", endpoint="/api/v1/vmeta/canvas/search", data={}
    )
    rows = response.get("data", []) if isinstance(response, dict) else response
    for row in rows or []:
        if row.get("_id"):
            return row["_id"]
    return None


@mcp.tool()
def prepare_samples(name: str, files: list, canvas_id: str = "") -> str:
    """
    Build a sample dataset from a list of fastq paths in ONE call, pairing
    R1/R2 automatically. Returns the dataset id as `vmetaCollectionId`, ready
    to set as a run's sample input.

    Handles the naming conventions that occur in practice — `_R1_001`, `_R1`,
    `_1`, `.1.` — derives the sample name by stripping the mate marker and
    extension, and reports any read whose mate is missing rather than silently
    treating it as single-end.

    Args:
        name: Dataset name (must be non-empty).
        files: Absolute paths to the fastq files, R1 and R2 together.
        canvas_id: Optional study-tracker canvas; resolved automatically if
            omitted.
    """
    try:
        if not name or not name.strip():
            raise ValueError("Dataset name must be a non-empty string")
        if not files:
            raise ValueError(
                "No files given — pass the absolute paths of the fastq files")

        rows, unpaired = _pair_fastqs(files)
        if not rows:
            raise ValueError("No usable file paths were found in `files`")

        # Invariant: every path handed in must end up in exactly one row. This
        # is what catches ambiguous naming — e.g. a_R1/a_R2 alongside a bare
        # a.fastq.gz all reduce to the sample "a", and without this check the
        # odd one out is silently dropped and the run quietly analyses less
        # data than the scientist believes.
        given = [f.strip() for f in files if f and f.strip()]
        used = {r["file1"] for r in rows} | {r["file2"] for r in rows if r["file2"]}
        dropped = [f for f in given if f not in used]
        if dropped:
            raise ValueError(
                f"These files could not be assigned to a distinct sample: "
                f"{', '.join(dropped)}. They collapse to the same sample name as "
                f"another file. Rename them so each sample is unambiguous — "
                f"otherwise they would be silently left out of the analysis."
            )

        via_client = get_client()
        resolved_canvas = canvas_id.strip() if canvas_id else _resolve_canvas_id(via_client)
        if not resolved_canvas:
            raise ValueError(
                "No study-tracker canvas is available to attach samples to. "
                "Pass canvas_id explicitly (search_canvas lists them).")

        logger.info(f"Creating dataset '{name}' with {len(rows)} sample(s)")
        created = via_client.call(
            method="POST", endpoint="/api/v1/vmeta/dataset/create",
            data={"name": name.strip()},
        )
        payload = created.get("data", created) if isinstance(created, dict) else {}
        dataset_id = payload.get("_id") or payload.get("id")
        if not dataset_id:
            raise ValueError(f"Dataset creation returned no id: {created}")

        added, failed = [], []
        for row in rows:
            file_row = {"name": row["name"], "file1": row["file1"],
                        "file2": row["file2"], "file3": "", "file4": "",
                        "file_layout": row["file_layout"]}
            try:
                via_client.call(
                    method="POST",
                    endpoint=f"/api/v1/vmeta/dataset/{dataset_id}/addFile",
                    data={"canvasId": resolved_canvas, "file": file_row},
                )
                added.append(row["name"])
            except Exception as e:
                logger.error(f"Could not add sample '{row['name']}': {e}")
                failed.append(row["name"])

        if failed:
            raise ValueError(
                f"Dataset '{name}' was created ({dataset_id}) but "
                f"{len(failed)} sample(s) could not be attached: "
                f"{', '.join(failed)}. The dataset is NOT ready to use; add the "
                f"missing samples or delete it and retry."
            )

        paired = sum(1 for r in rows if r["file_layout"] == "pair")
        next_steps = [
            f"Set this dataset on a run: update_run(...) with the sample "
            f"input's vmetaCollectionId = '{dataset_id}'.",
            "Then preflight_run(run_id=...) before launching.",
        ]
        if unpaired:
            next_steps.insert(0, (
                f"Check {', '.join(unpaired)} — a mate file was expected but "
                f"not found, so it was added as single-end. If the R2 exists, "
                f"re-run prepare_samples with it included."))

        result = envelope(
            summary=(
                f"Dataset '{name}' created with {len(added)} sample(s) "
                f"({paired} paired-end, {len(added) - paired} single-end)."
            ),
            data={"vmetaCollectionId": dataset_id, "name": name.strip(),
                  "sample_count": len(added), "paired": paired,
                  "unpaired": unpaired,
                  "samples": [r["name"] for r in rows]},
            next_steps=next_steps,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error preparing samples for '{name}': {e}")
        return json.dumps({"error": str(e)})


# fastq-dump writes uncompressed fastq and gzips it afterwards, so a run's
# inputs folder peaks well above the archive's fastq size. Measured on a
# staging run: 22 GB peak for 3.7 GB of ENA fastq_bytes, about 6x.
_PEAK_INPUTS_FACTOR = 6


def _dataset_layout(plan):
    """("single" | "paired" | "mixed", run `mate` value or None, breakdown)."""
    record_layouts = {r["file_layout"] for r in plan["records"]}
    if record_layouts == {"single"}:
        layout, mate = "single", "single"
    elif record_layouts == {"pair"}:
        layout, mate = "paired", "pair"
    else:
        layout, mate = "mixed", None
    breakdown = {}
    for sample in plan["samples"]:
        group = {"pair": "paired"}.get(sample["layout"], sample["layout"])
        breakdown.setdefault(group, []).append(sample["name"])
    return layout, mate, breakdown


def _layout_steps(layout, mate):
    if mate is None:
        return [
            "The samples mix paired and single end reads, and one run takes one layout: "
            "split the accessions into a paired dataset and a single end dataset "
            "(layout_breakdown lists which sample is which) and run each separately.",
        ]
    return [
        f"The reads are {layout} end. On the run, set the `mate` input to '{mate}' with "
        f"update_run(inputs=[{{'name': 'mate', 'value': '{mate}'}}]) and set any process "
        f"option that names the layout to match (for example Kallisto's "
        f"single_or_paired_end_reads). A copied run keeps its old `mate`, and a mismatch "
        f"fails in the first read step; preflight_run checks it.",
    ]


@mcp.tool()
def create_dataset_from_accessions(
    name: str,
    accessions: list,
    canvas_id: str = "",
    dry_run: bool = False,
) -> str:
    """
    Build a sample dataset from public sequencing accessions, ready to set as
    a run's sample input (`vmetaCollectionId`). The run downloads the reads
    itself from SRA, so nothing is uploaded.

    Accepts GEO series and samples (GSE, GSM), SRA/ENA/DDBJ studies (SRP, ERP,
    DRP, PRJNA, PRJEB), experiments (SRX, ERX), runs (SRR, ERR, DRR),
    BioSamples (SAMN, SAMEA) and ArrayExpress studies (E-MTAB-n). Each is
    resolved to its sequencing runs through ENA (GEO via NCBI, ArrayExpress
    via BioStudies). Anything that cannot be resolved is listed in
    `unresolved` with the reason; nothing is guessed. Runs, experiments and
    GEO samples are looked up in batches, so pass a long list in one call
    rather than one accession per call.

    Runs are grouped by sample. The sample name comes from the GEO sample or
    BioSample title. Each run becomes one record (a sample with several runs
    gets one record per run, and a warning). Layout (pair or single) comes
    from ENA. Mixed layouts, paired runs with one fastq file and non RNA
    library strategies are reported as warnings: show them to the user.

    Layout must reach the run: the result's `layout` is "single", "paired" or
    "mixed", and `mate` is the value the run's `mate` input must carry
    ("single" or "pair"). Set it with update_run, together with any process
    option that names the layout (for example Kallisto's
    single_or_paired_end_reads). A copied run keeps its old `mate`, and a
    mismatch fails the run. A "mixed" dataset cannot go on one run: split it
    by `layout_breakdown`.

    ALWAYS call with dry_run=True first and show the user the plan: samples,
    runs, layouts, total raw size (`total_raw_bytes`), unresolved and
    warnings. dry_run writes nothing. Then call again without dry_run once
    the user agrees; that creates the dataset.

    Storage: the reads take space on the run environment only while a run is
    active. They live in the run's inputs and work folders, which are deleted
    after the run ends once its cleanup delay passes (set it with
    update_run(cleanup_delay_hours=...), for example 6). `total_raw_bytes` is
    the archive's compressed fastq size and understates the space used: the
    download writes uncompressed fastq before gzipping it, so the inputs
    folder peaks far higher. `peak_inputs_bytes_estimate` (6x the raw size,
    an estimate taken from a measured staging run) is the number to plan
    with. For a large study, split the accessions into batches so that, for
    all runs in flight at once, the peak estimate plus the alignment working
    space (roughly the size of the uncompressed reads again, more for large
    genomes) stays within the user's storage budget; ask the user for that
    budget if you do not know it. Never delete runs to free space: deleting a
    run does not remove its files from the cluster, and it destroys the
    user's results.

    Args:
        name: Dataset name (non-empty, unique in the project).
        accessions: Accessions to include, any mix of the types above.
        canvas_id: Optional study-tracker canvas; resolved automatically if
            omitted.
        dry_run: True returns the plan and writes nothing.
    """
    try:
        if not name or not name.strip():
            raise ValueError("Dataset name must be a non-empty string")
        wanted = [a.strip() for a in (accessions or []) if isinstance(a, str) and a.strip()]
        if not wanted:
            raise ValueError("No accessions given: pass a list such as ['GSE36552']")

        plan = _accessions.build_plan(wanted)
        layout, mate, breakdown = _dataset_layout(plan)
        peak = plan["total_raw_bytes"] * _PEAK_INPUTS_FACTOR
        data = {
            "dry_run": bool(dry_run),
            "vmetaCollectionId": None,
            "name": name.strip(),
            "sample_count": plan["sample_count"],
            "run_count": plan["run_count"],
            "total_raw_bytes": plan["total_raw_bytes"],
            "total_raw_size": _human_size(plan["total_raw_bytes"]),
            "peak_inputs_bytes_estimate": peak,
            "peak_inputs_size_estimate": _human_size(peak),
            "layout": layout,
            "mate": mate,
            "layout_breakdown": breakdown,
            "samples": plan["samples"],
            "unresolved": plan["unresolved"],
            "warnings": plan["warnings"],
        }
        if not plan["records"]:
            raise ValueError(
                "None of the accessions resolved to sequencing runs, so no dataset was "
                f"created. Unresolved: {json.dumps(plan['unresolved'])}")

        size = _human_size(plan["total_raw_bytes"])
        summary = (f"{plan['sample_count']} sample(s), {plan['run_count']} run(s), "
                   f"{size} of raw reads")
        if dry_run:
            return json.dumps(envelope(
                summary=f"Plan only, nothing written: {summary}.",
                data=data,
                next_steps=[
                    "Show the user the samples, layout, total raw size, peak inputs estimate, "
                    "unresolved and warnings.",
                    "If they agree, call again with dry_run=False to create the dataset.",
                ] + _layout_steps(layout, mate),
            ), indent=2)

        via_client = get_client()
        resolved_canvas = canvas_id.strip() if canvas_id else _resolve_canvas_id(via_client)
        if not resolved_canvas:
            raise ValueError(
                "No study-tracker canvas is available to attach samples to. "
                "Pass canvas_id explicitly (search_canvas lists them).")
        created = via_client.call(
            method="POST", endpoint="/api/v1/vmeta/dataset/create",
            data={"name": name.strip()},
        )
        payload = created.get("data", created) if isinstance(created, dict) else {}
        dataset_id = payload.get("_id") or payload.get("id")
        if not dataset_id:
            raise ValueError(f"Dataset creation returned no id: {created}")

        failed = []
        for record in plan["records"]:
            try:
                via_client.call(
                    method="POST",
                    endpoint=f"/api/v1/vmeta/dataset/{dataset_id}/addFile",
                    data={"canvasId": resolved_canvas, "file": dict(record)},
                )
            except Exception as e:
                logger.error(f"Could not add record '{record['name']}': {e}")
                failed.append(f"{record['name']} ({record['geo_id']}): {e}")
        if failed:
            raise ValueError(
                f"Dataset '{name.strip()}' was created ({dataset_id}) but {len(failed)} of "
                f"{len(plan['records'])} record(s) could not be attached: {'; '.join(failed)}. "
                f"The dataset is NOT ready to use; add the missing records or delete it and retry.")

        data["vmetaCollectionId"] = dataset_id
        return json.dumps(envelope(
            summary=f"Dataset '{name.strip()}' created: {summary}.",
            data=data,
            next_steps=[
                f"Set it on a run: update_run(...) with the sample input's vmetaCollectionId = '{dataset_id}'.",
            ] + _layout_steps(layout, mate) + [
                "Set a short cleanup delay on that run, for example update_run(cleanup_delay_hours=6).",
                "Then preflight_run(run_id=...) before launching.",
            ],
        ), indent=2)
    except Exception as e:
        logger.error(f"Error creating dataset from accessions for '{name}': {e}")
        raise ToolError(json.dumps({"error": str(e)})) from e


# ---------------------------------------------------------------------------
# Sample sheets
# ---------------------------------------------------------------------------

# Both layouts were read off the cluster from live run 12194, not guessed.
_GROUPS_SHEET_NAME = "metadata.tsv"
_COMPARISONS_SHEET_NAME = "comparisons.tsv"
_GROUPS_HEADER = "sample_name\tgroup"
_COMPARISONS_HEADER = "controls\ttreats\tnames"


def _normalize_groups(groups):
    """Accept {sample: group} or [[sample, group], ...] and return an ordered
    dict. Insertion order is preserved so a scientist gets their own ordering
    back rather than an alphabetised one."""
    if isinstance(groups, dict):
        pairs = list(groups.items())
    else:
        pairs = []
        for row in groups or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                pairs.append((row[0], row[1]))
            elif isinstance(row, dict) and "sample" in row:
                pairs.append((row["sample"], row.get("group")))
    return {str(s).strip(): str(g).strip() for s, g in pairs if str(s).strip()}


def _render_groups_tsv(groups) -> str:
    mapping = _normalize_groups(groups)
    lines = [_GROUPS_HEADER]
    lines.extend(f"{sample}\t{group}" for sample, group in mapping.items())
    return "\n".join(lines) + "\n"


def _render_comparisons_tsv(comparisons) -> str:
    lines = [_COMPARISONS_HEADER]
    for row in comparisons or []:
        control, treat = str(row[0]).strip(), str(row[1]).strip()
        name = (str(row[2]).strip() if len(row) > 2 and row[2]
                else f"{control}_vs_{treat}")
        lines.append(f"{control}\t{treat}\t{name}")
    return "\n".join(lines) + "\n"


def _upload_run_file(run_id, run_uuid, file_name, content, remote_dir):
    """POST a small text file to a run's upload area.

    Deliberately a raw multipart request rather than the SDK's
    upload_report_file: that derives its attempt id from the run's existing
    report paths, which a run that has never launched does not have — and a
    never-launched run is exactly when a sample sheet is needed.
    """
    hostname, token = get_credentials()
    url = f"{hostname.rstrip('/')}/api/v1/run/{run_id}/reports/upload/{run_uuid}"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (file_name, content.encode("utf-8"), "text/tab-separated-values")},
        data={"dir": remote_dir, "target": "runInput"},
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Upload of {file_name} failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    return True


def _upload_report_enabled(via_client) -> bool:
    """The upload route sits behind the per-user UPLOAD_REPORT feature flag."""
    try:
        flags = via_client.call(
            method="GET", endpoint="/api/v1/configurations/user")
        return bool((flags or {}).get("UPLOAD_REPORT"))
    except Exception as e:
        logger.info(f"Could not read feature flags, assuming upload is off: {e}")
        return False


@mcp.tool()
def make_sample_sheet(
    run_id: str,
    groups: dict,
    comparisons: list = None,
    allow_single_replicate: bool = False,
) -> str:
    """
    Build the experimental-design sheets a differential-expression run needs,
    and place them where the run expects them.

    `groups` says which sample belongs to which condition; `comparisons` says
    which conditions to contrast. Returns the absolute paths to set as the run's
    `groups_file` and `compare_file` inputs, and always returns the sheet text
    itself so nothing is a black box.

    Args:
        run_id: The run these sheets belong to.
        groups: {sample_name: group}, or [[sample_name, group], ...].
        comparisons: [[control, treated], ...]; a third element names the
            comparison, otherwise it is "<control>_vs_<treated>".
        allow_single_replicate: Permit a group with only one sample. Off by
            default because differential expression cannot test such a group.
    """
    try:
        mapping = _normalize_groups(groups)
        if not mapping:
            raise ValueError(
                "No groups given — pass {sample_name: group} naming which "
                "sample belongs to which condition.")

        counts = {}
        for group in mapping.values():
            counts[group] = counts.get(group, 0) + 1
        thin = sorted(g for g, n in counts.items() if n < _PREFLIGHT_MIN_REPLICATES)
        if thin and not allow_single_replicate:
            raise ValueError(
                f"Group(s) with only one sample: {', '.join(thin)}. Differential "
                f"expression cannot test a group without replicates. Add more "
                f"samples, merge the group, or pass allow_single_replicate=True "
                f"if you really mean it."
            )

        for row in comparisons or []:
            unknown = [str(g).strip() for g in row[:2]
                       if str(g).strip() not in counts]
            if unknown:
                raise ValueError(
                    f"Comparison names a group that is not in `groups`: "
                    f"{', '.join(unknown)}. Known groups: "
                    f"{', '.join(sorted(counts))}."
                )

        via_client = get_client()
        details = via_client.call(
            method="GET", endpoint=f"/api/v1/run/{run_id}/details")
        launch_dir = (details.get("launchDirectory") or "").rstrip("/")
        if not launch_dir:
            raise ValueError(
                f"Run {run_id} has no launch directory, so there is nowhere to "
                f"put the sheets. Check the run's compute environment.")
        run_uuid = details.get("runUUID")

        remote_dir = f"foundryUploads/run{run_id}"
        base = f"{launch_dir}/{remote_dir}"
        groups_tsv = _render_groups_tsv(mapping)
        comparisons_tsv = _render_comparisons_tsv(comparisons) if comparisons else None

        sheets = [(_GROUPS_SHEET_NAME, groups_tsv)]
        if comparisons_tsv:
            sheets.append((_COMPARISONS_SHEET_NAME, comparisons_tsv))

        uploaded = _upload_report_enabled(via_client)
        if uploaded:
            for file_name, content in sheets:
                _upload_run_file(
                    run_id=run_id, run_uuid=run_uuid, file_name=file_name,
                    content=content, remote_dir=remote_dir,
                )

        groups_path = f"{base}/{_GROUPS_SHEET_NAME}"
        compare_path = f"{base}/{_COMPARISONS_SHEET_NAME}" if comparisons_tsv else None

        data = {
            "uploaded": uploaded,
            "groups_file": groups_path,
            "compare_file": compare_path,
            "groups_tsv": groups_tsv,
            "comparisons_tsv": comparisons_tsv,
            "group_counts": counts,
        }

        if uploaded:
            summary = (
                f"Wrote {len(sheets)} sheet(s) for run {run_id}: "
                f"{len(mapping)} samples across {len(counts)} group(s)."
            )
            next_steps = [
                f"Set the paths on the run: update_run(...) with "
                f"groups_file='{groups_path}'"
                + (f" and compare_file='{compare_path}'" if compare_path else "")
                + ".",
                f"Then preflight_run(run_id='{run_id}') before launching.",
            ]
        else:
            summary = (
                f"Generated {len(sheets)} sheet(s) for run {run_id} but did NOT "
                f"upload them — the UPLOAD_REPORT feature is not enabled for "
                f"this user."
            )
            next_steps = [
                "Save the `groups_tsv` (and `comparisons_tsv`) content below "
                f"and upload it to the run's Uploads area as "
                f"{_GROUPS_SHEET_NAME}"
                + (f" / {_COMPARISONS_SHEET_NAME}" if compare_path else "")
                + ", or ask an admin to enable UPLOAD_REPORT.",
                f"Then set groups_file='{groups_path}'"
                + (f" and compare_file='{compare_path}'" if compare_path else "")
                + " with update_run, and preflight_run before launching.",
            ]

        result = envelope(summary=summary, data=data, next_steps=next_steps)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error making sample sheets for run {run_id}: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Pre-flight: catch what would otherwise waste cluster time
# ---------------------------------------------------------------------------

# Values that mean "nothing here" once they reach the pipeline.
_PREFLIGHT_PLACEHOLDERS = ("", "NO_FILE", "NA", "None", "null")

# Statuses that mean the run has never shipped its upload tarball to the
# cluster, so a path under its own foundryUploads/ legitimately does not exist
# yet. Failing there would cry wolf on every freshly duplicated run.
_PREFLIGHT_UNLAUNCHED = ("init", "Waiting", "NotSubmitted", "")

_PREFLIGHT_MIN_REPLICATES = 2


# Input definition types that carry the run's data files.
_DATA_INPUT_TYPES = ("file", "set", "singleFile")


def _preflight_definitions(via_client, run_id):
    """The pipeline's input definitions, or None when they cannot be read."""
    try:
        return _input_definitions(via_client.call(
            method="GET", endpoint=f"/api/v1/run/{run_id}/parameter-list"))
    except Exception as e:
        logger.info(f"Pre-flight could not read input definitions: {e}")
        return None


def _filled(value):
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in _PREFLIGHT_PLACEHOLDERS
    return True


def _filled_input_names(inputs):
    """Names of saved inputs that hold a value, from either run-input shape."""
    names = set()
    if isinstance(inputs, dict):
        for key, val in inputs.items():
            if isinstance(val, dict) and "value" not in val:
                # External pipeline section: {name: value}.
                names.update(n for n, v in val.items() if _filled(v))
            elif _filled(val.get("value") if isinstance(val, dict) else val):
                names.add(key)
        return names
    for inp in inputs or []:
        if isinstance(inp, dict) and (inp.get("vmetaCollectionId") or inp.get("collectionId")
                                      or _filled(inp.get("value"))):
            names.add(inp.get("name"))
    return names


def _preflight_check(check_id, status, detail, fix="", **extra):
    check = {"id": check_id, "status": status, "detail": detail, "fix": fix}
    check.update(extra)
    return check


def _read_cluster_file(via_client, run_id, path, cluster_id, run_uuid):
    """Read a file from the run's compute environment. Returns None if absent."""
    try:
        body = {"path": path, "profileClusterId": cluster_id}
        if run_uuid:
            body["runUUID"] = run_uuid
        response = via_client.call(
            method="POST", endpoint=f"/api/v1/run/{run_id}/uploaded-file",
            data=body,
        )
        if isinstance(response, dict):
            # A JSON body here is the error envelope, not file contents.
            return None
        return response
    except Exception as e:
        logger.info(f"Pre-flight could not read {path}: {e}")
        return None


def _parse_groups_sheet(text):
    """Parse a `sample_name<TAB>group` sheet into {sample: group}.

    Tolerates comma separation and a missing header — a scientist pasting a
    sheet should not be defeated by punctuation.
    """
    mapping = {}
    for i, line in enumerate((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[\t,]", line)
        if len(parts) < 2:
            continue
        sample, group = parts[0].strip(), parts[1].strip()
        if i == 0 and sample.lower() in ("sample_name", "sample", "name"):
            continue
        if sample:
            mapping[sample] = group
    return mapping


def _row_layout(row):
    """A dataset row's read layout, stored or derived from its files."""
    stored = row.get("file_layout") or row.get("File Layout")
    if stored:
        return str(stored).strip().lower()
    if row.get("file2"):
        return "pair"
    if row.get("file1"):
        return "single"
    return None


# A process option that declares the read layout, such as Kallisto's
# single_or_paired_end_reads. Process options are stored on the run as
# {"<process>_<gnum>": {option: value}}; the option name is matched, and only
# a value that reads as a layout counts (an empty one defers to `mate`).
_LAYOUT_OPTION_NAME = re.compile(r"single.*pair|pair.*single|layout|read_?type|^mate$", re.IGNORECASE)
_LAYOUT_VALUES = {
    "single": "single", "single-end": "single", "single_end": "single", "se": "single",
    "pair": "pair", "paired": "pair", "paired-end": "pair", "paired_end": "pair", "pe": "pair",
}


def _layout_options(process_options):
    """[(where, value, layout)] for process options that declare a layout."""
    found = []
    for group, options in (process_options or {}).items():
        if not isinstance(options, dict):
            continue
        for name, value in options.items():
            if not isinstance(value, str) or not _LAYOUT_OPTION_NAME.search(str(name)):
                continue
            layout = _LAYOUT_VALUES.get(value.strip().lower())
            if layout:
                found.append((f"{group}.{name}", value.strip(), layout))
    return found


def _preflight_layout(inputs, layouts, process_options=None):
    """Fail when the attached datasets' layout disagrees with the run's `mate`
    input (staging run 12450 failed with "No such variable: file1" for a single
    end dataset on a run whose `mate` stayed `pair`) or with a process option
    that declares a layout (staging run 12451: Kallisto's
    single_or_paired_end_reads stayed `pair`). None when not applicable."""
    mate = next((str(i.get("value") or "").strip().lower() for i in inputs
                 if i.get("name") == "mate"), None)
    options = _layout_options(process_options)
    if (not mate and not options) or not layouts:
        return None
    found = sorted(layouts)
    if len(found) > 1:
        detail = "The sample dataset mixes layouts: " + "; ".join(
            f"{lay}: {', '.join(sorted(layouts[lay])[:5])}" for lay in found) + "."
        return _preflight_check(
            "layout", "fail", detail,
            "One run takes one layout. Split the dataset into one per layout and run each "
            "with the matching `mate` input.")
    dataset = found[0]
    wrong = []
    if mate and _LAYOUT_VALUES.get(mate, mate) != dataset:
        wrong.append(f"the `mate` input is '{mate}'")
    wrong += [f"process option {where} is '{value}'" for where, value, layout in options if layout != dataset]
    if wrong:
        fixes = []
        if mate and _LAYOUT_VALUES.get(mate, mate) != dataset:
            fixes.append(f"set the `mate` input to '{dataset}' with update_run(inputs=...)")
        bad_options = [where for where, _, layout in options if layout != dataset]
        if bad_options:
            fixes.append(f"set {', '.join(bad_options)} to '{dataset}' in update_run(process_options=...), "
                         f"sending the complete process options")
        return _preflight_check(
            "layout", "fail",
            f"The sample dataset is {dataset} end but {'; '.join(wrong)}.",
            "Make them agree: " + "; ".join(fixes) + ".")
    checked = (["`mate`"] if mate else []) + [where for where, _, _ in options]
    return _preflight_check("layout", "pass", f"Dataset layout ({dataset}) agrees with {', '.join(checked)}.")


def _preflight_dataset(via_client, sample_inputs, layouts=None):
    """Resolve each sample input's dataset and return (check, sample_names).
    When `layouts` is a dict, it is filled with {layout: [sample names]}."""
    layouts = {} if layouts is None else layouts
    if not sample_inputs:
        return _preflight_check(
            "samples", "fail", "No sample dataset is attached to this run.",
            "Build one with prepare_samples(...), then set it with update_run.",
        ), set()

    names, empty = set(), []
    for inp in sample_inputs:
        dataset_id = inp.get("vmetaCollectionId")
        if not dataset_id:
            empty.append(inp["name"])
            continue
        try:
            response = via_client.call(
                method="POST",
                endpoint=f"/api/v1/vmeta/dataset/{dataset_id}/files/search",
                data={},
            )
            rows = response.get("data", []) if isinstance(response, dict) else []
        except Exception as e:
            logger.warning(f"Pre-flight could not read dataset {dataset_id}: {e}")
            rows = []
        if not rows:
            empty.append(inp["name"])
        names.update(r.get("name") for r in rows if r.get("name"))
        for r in rows:
            row_layout = _row_layout(r)
            if row_layout:
                layouts.setdefault(row_layout, []).append(r.get("name") or "?")

    if empty:
        return _preflight_check(
            "samples", "fail",
            f"Sample dataset for {', '.join(empty)} has no files.",
            "Add samples with prepare_samples(...) or pick a dataset that has "
            "them, then update_run.",
        ), names
    return _preflight_check(
        "samples", "pass", f"{len(names)} sample(s) attached."), names


# Task executors that submit each task to a batch scheduler, which applies its
# own site default memory when a task asks for none.
_BATCH_SCHEDULERS = ("lsf", "slurm", "sge", "uge", "pbs", "pbspro", "moab", "condor", "oar", "hq")


def _preflight_executor_resources(details, env):
    """Warn when tasks go to a batch scheduler and nothing sets their memory.
    Returns None when the check does not apply."""
    executor = ((env or {}).get("executorJob") or "").strip().lower()
    if executor not in _BATCH_SCHEDULERS:
        return None
    advanced = details.get("advancedSettings") or {}
    global_exec = advanced.get("globalExec") or {}
    process_exec = advanced.get("processExec") or {}
    if global_exec.get("enabled") and _filled((global_exec.get("options") or {}).get("job_memory")):
        return _preflight_check("executor_resources", "pass", "The run sets memory for every task.")
    if process_exec.get("enabled") and any(
            isinstance(row, dict) and _filled(row.get("memory"))
            for row in (process_exec.get("options") or {}).values()):
        return _preflight_check("executor_resources", "pass", "The run sets memory for some processes.")
    # The environment's defaults apply only to a run that has saved executor
    # settings and switched them off; a run with none saved emits no directive.
    uses_env_defaults = not global_exec.get("enabled") and global_exec.get("options") is not None
    if uses_env_defaults and _filled(env.get("jobMemory")):
        return _preflight_check(
            "executor_resources", "pass", f"The run environment asks {env['jobMemory']} GB per task.")
    return _preflight_check(
        "executor_resources", "warn",
        f"Tasks are submitted to {executor}, but neither the run nor its environment sets memory, "
        "so the scheduler applies its site default and large tasks may be killed for exceeding it.",
        "Set resources with update_run(exec_settings=...), for example copied from a run that worked "
        "(get_run_details(run_id, verbose=True), advancedSettings.globalExec and processExec).",
    )


@mcp.tool()
def preflight_run(run_id: str) -> str:
    """
    Check a run for the mistakes that waste cluster time, BEFORE launching it.

    Verifies the sample dataset has files, that every sample-sheet path really
    exists on the compute environment, that the sheet's sample names match the
    dataset, that each group has replicates, and that no required input is
    empty. Call this between update_run and initiate_run — every problem it
    finds is one that would otherwise surface as a failed or silently-skipped
    job after the queue time has already been spent.

    Args:
        run_id: The run to check.
    """
    try:
        via_client = get_client()
        logger.info(f"Pre-flighting run {run_id}")
        details = via_client.call(
            method="GET", endpoint=f"/api/v1/run/{run_id}/details"
        )

        run_env = details.get("runEnvironment") or {}
        cluster_id = run_env.get("selectedId")
        run_uuid = details.get("runUUID")
        launch_dir = (details.get("launchDirectory") or "").rstrip("/")
        own_uploads = f"{launch_dir}/foundryUploads/run{run_id}" if launch_dir else None

        # Has this run ever shipped its uploads to the cluster?
        launched = True
        try:
            listing = via_client.call(
                method="POST", endpoint="/api/v1/run/list",
                params={"take": 1, "skip": 0, "sort": "dateCreated",
                        "order": "desc", "filter": f"id:eq={run_id}"},
                data={},
            )
            rows = listing.get("data", []) if isinstance(listing, dict) else []
            if rows:
                launched = rows[0].get("status") not in _PREFLIGHT_UNLAUNCHED
        except Exception as e:
            logger.info(f"Pre-flight could not read run status: {e}")

        inputs = list(_iter_run_inputs(details.get("inputs") or []))
        sample_inputs = [i for i in inputs if i["type"] == "vmetaCollection"]

        checks = []
        dataset_layouts = {}
        dataset_check, dataset_names = _preflight_dataset(via_client, sample_inputs, dataset_layouts)
        checks.append(dataset_check)
        layout_check = _preflight_layout(inputs, dataset_layouts, details.get("processOptions"))
        if layout_check:
            checks.append(layout_check)

        # Empty / placeholder values, and required inputs never saved at all.
        blanks = [i["name"] for i in inputs
                  if isinstance(i["value"], str)
                  and i["value"].strip() in _PREFLIGHT_PLACEHOLDERS]
        definitions = _preflight_definitions(via_client, run_id)
        filled = _filled_input_names(details.get("inputs"))
        unset = [d["name"] for d in definitions or []
                 if d["required"] and d["default"] in (None, "")
                 and d["name"] not in filled and d["name"] not in blanks]
        problems = []
        if blanks:
            problems.append(f"{len(blanks)} input(s) have no value: {', '.join(blanks)}.")
        if unset:
            problems.append(f"{len(unset)} required input(s) are not set: {', '.join(unset)}.")
        checks.append(
            _preflight_check(
                "empty_inputs", "fail", " ".join(problems),
                "Set them with update_run — the pipeline treats these as "
                "missing and may skip steps rather than fail loudly.",
            ) if problems else
            _preflight_check("empty_inputs", "pass", "All inputs have values.")
        )

        # A pipeline may take its data from one of several optional inputs
        # (reads or bam_files), so none is required, yet one must be set.
        data_names = [d["name"] for d in definitions or [] if d["type"] in _DATA_INPUT_TYPES]
        if data_names:
            given = [n for n in data_names if n in filled]
            checks.append(
                _preflight_check("data_inputs", "pass", f"Data input set: {', '.join(given)}.")
                if given else
                _preflight_check(
                    "data_inputs", "fail",
                    f"No data input is set (one of: {', '.join(data_names)}).",
                    "Set one with update_run, for example a sample dataset as "
                    "{name, vmetaCollectionId} from search_datasets.",
                )
            )

        # A run created from scratch launches only once both are saved.
        usable_ids = {e.get("id") for e in run_env.get("availableList") or []}
        if not usable_ids:
            checks.append(_preflight_check(
                "run_environment", "fail", "You have no run environment you can launch on.",
                "Add one under your profile's run environments, or ask your "
                "organization to share one.",
            ))
        elif not cluster_id:
            checks.append(_preflight_check(
                "run_environment", "fail", "No run environment is selected.",
                "Pick one from list_run_environments(run_id) and save it with "
                "update_run(run_environment_id=...).",
            ))
        elif cluster_id not in usable_ids:
            checks.append(_preflight_check(
                "run_environment", "fail",
                f"The selected run environment {cluster_id} is not one you can launch on.",
                "A copied run keeps its source's environment. Pick one of yours from "
                "list_run_environments(run_id) and save it with update_run(run_environment_id=...).",
            ))
        else:
            checks.append(_preflight_check("run_environment", "pass", f"Run environment {cluster_id} is selected."))
        checks.append(
            _preflight_check("execution_directory", "pass", f"Execution directory is {launch_dir}.")
            if launch_dir else
            _preflight_check(
                "execution_directory", "fail", "No execution directory is set.",
                "Confirm the proposed directory from list_run_environments(run_id) "
                "with the user and save it with update_run(launch_directory=...).",
            )
        )
        resources = _preflight_executor_resources(
            details, next((e for e in run_env.get("availableList") or [] if e.get("id") == cluster_id), None))
        if resources:
            checks.append(resources)

        # Genome build.
        build = next((i for i in inputs
                      if "genome_build" in (i["name"] or "").lower()), None)
        checks.append(
            _preflight_check("genome", "pass",
                             f"Genome build is '{build['value']}'.")
            if build and str(build["value"]).strip()
            else _preflight_check(
                "genome", "warn", "No genome build is set on this run.",
                "Confirm the pipeline does not need one, or set it with "
                "update_run.")
        )

        # Design-file paths, read from the run's own compute environment.
        sheets = {}
        for inp in inputs:
            value = inp["value"]
            if not _is_design_input(inp["name"]) or not isinstance(value, str):
                continue
            if value.strip() in _PREFLIGHT_PLACEHOLDERS:
                continue
            contents = _read_cluster_file(
                via_client, run_id, value, cluster_id, run_uuid)
            if contents is not None:
                sheets[inp["name"]] = contents
                checks.append(_preflight_check(
                    "design_file", "pass", f"{inp['name']} exists.",
                    input=inp["name"]))
                continue
            pending = (not launched and own_uploads
                       and value.startswith(own_uploads))
            if pending:
                checks.append(_preflight_check(
                    "design_file", "warn",
                    f"{inp['name']} is not on the cluster yet ({value}).",
                    "Expected — this run has not launched, so its uploads have "
                    "not shipped. It will be sent with the run.",
                    input=inp["name"]))
            else:
                checks.append(_preflight_check(
                    "design_file", "fail",
                    f"{inp['name']} points at a path that does not exist: "
                    f"{value}.",
                    "Re-supply the file with make_sample_sheet(...) — this is "
                    "what happens when a run is duplicated after its original "
                    "run directory was cleaned up, and it makes downstream "
                    "steps silently skip.",
                    input=inp["name"]))

        # Sample names + replicates, from whichever sheet carries groups.
        groups = {}
        for name, text in sheets.items():
            if "group" in name.lower():
                groups = _parse_groups_sheet(text)
                break

        if groups and dataset_names:
            unknown = sorted(set(groups) - dataset_names)
            ungrouped = sorted(dataset_names - set(groups))
            if unknown:
                checks.append(_preflight_check(
                    "sample_names_match", "fail",
                    f"{len(unknown)} sample(s) in the sheet are not in the "
                    f"dataset: {', '.join(unknown)}.",
                    "Fix the names so they match the dataset exactly — the "
                    "pipeline matches on the name string."))
            elif ungrouped:
                checks.append(_preflight_check(
                    "sample_names_match", "warn",
                    f"{len(ungrouped)} sample(s) have no group and will be "
                    f"left out: {', '.join(ungrouped)}.",
                    "Add them to the groups sheet, or confirm you meant to "
                    "exclude them."))
            else:
                checks.append(_preflight_check(
                    "sample_names_match", "pass",
                    "Sheet and dataset sample names match."))

        if groups:
            counts = {}
            for group in groups.values():
                counts[group] = counts.get(group, 0) + 1
            thin = sorted(g for g, n in counts.items()
                          if n < _PREFLIGHT_MIN_REPLICATES)
            checks.append(
                _preflight_check(
                    "replicates", "warn",
                    f"Group(s) with only one sample: {', '.join(thin)}.",
                    "Differential expression needs replicates; with one sample "
                    "a group cannot be tested.")
                if thin else
                _preflight_check("replicates", "pass",
                                 f"{len(counts)} group(s), all replicated.")
            )

        failures = sum(1 for c in checks if c["status"] == "fail")
        warnings = sum(1 for c in checks if c["status"] == "warn")
        ok = failures == 0

        if ok:
            summary = (f"Run {run_id} looks ready to launch "
                       f"({len(checks)} checks, {warnings} warning(s)).")
            next_steps = [
                "Review any warnings with the user.",
                f"Then initiate_run(run_id='{run_id}') — this uses HPC "
                "compute, so confirm first.",
            ]
        else:
            summary = (f"Run {run_id} is NOT ready: {failures} problem(s) that "
                       f"would waste cluster time.")
            next_steps = [
                "Fix the failing checks above before launching — each one "
                "lists how.",
                "Then call preflight_run again; only launch once it passes.",
            ]

        result = envelope(
            summary=summary,
            data={"ok": ok, "failures": failures, "warnings": warnings,
                  "run": {"id": run_id, "name": details.get("name"),
                          "launched": launched},
                  "checks": checks},
            next_steps=next_steps,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error pre-flighting run {run_id}: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------

# These were specced as MCP `prompts`. FastMCP serves prompts and nginx cannot
# block them (prompts/list is a JSON-RPC method inside the same POST body as
# tools/list, which demonstrably works), but whether the claude.ai connector UI
# *renders* prompts to a user is unconfirmed — and a recipe nobody can see helps
# nobody. Tools are surfaced for certain, so the recipes live in one. If prompts
# turn out to be surfaced, these same recipes can also be registered with
# @mcp.prompt() without changing anything here.
_RECIPES = [
    {
        "id": "start_an_analysis",
        "name": "Start a new analysis from a scientific goal",
        "when": "The user describes an experiment or a question rather than "
                "naming a pipeline — e.g. 'I have mouse RNA-seq and want "
                "differential expression'.",
        "steps": [
            "recommend_pipeline(goal='<the user's own words>') — returns the "
            "best-fitting pipelines, why each was suggested, and a working past "
            "run to clone.",
            "plan_run(pipeline_id=<chosen>) — shows only the handful of "
            "decisions that matter. Ask the user about THOSE, not about "
            "reference paths or process options.",
            "list_projects() or create_project(name): pick where the run lives.",
            "create_run(pipeline_id=<chosen>, project_id=..., name=...): a new "
            "run of the pipeline; returns its form, your run environments and a "
            "proposed execution directory. Works with no earlier run.",
            "update_run(run_id='<new run>', inputs=..., run_environment_id=..., "
            "launch_directory=..., permission=...) to apply the user's answers.",
            "preflight_run(run_id='<new run>'): fix every failed check.",
            "initiate_run(run_id='<new run>') — launching uses HPC compute, so "
            "confirm with the user before this step.",
            "To copy a working example's settings instead: duplicate_run(run_id="
            "'<example run>', project_id=..., pipeline_id=...), then update_run "
            "on the NEW run (set run_environment_id if the example's is not yours).",
        ],
    },
    {
        "id": "diagnose_a_failure",
        "name": "Work out why a run failed",
        "when": "A run shows as Failed, or the user says a run did not work.",
        "steps": [
            "get_run(run_id='<id>') — confirm the status and which attempt is "
            "current.",
            "watch_run(run_id='<id>') — how far it got and which step failed, "
            "with the exit code.",
            "get_run_log(run_id='<id>') — returns the log that actually carries "
            "the failure, already tailed to the end where errors live. If it "
            "reports the logs were cleaned up, they are gone for good — say so "
            "rather than suggesting the user wait.",
            "Read the error to the user in plain language, and say which step "
            "of the pipeline it came from.",
            "If a setting caused it: plan_run(pipeline_id=...) to see the "
            "changeable decisions, then update_run and initiate_run to retry.",
        ],
    },
    {
        "id": "find_my_results",
        "name": "Show me my recent results",
        "when": "The user asks what finished, what came out of a run, or wants "
                "to see outputs.",
        "steps": [
            "list_runs(take=10) — most recent first; status tells you what "
            "completed.",
            "summarize_results(run_id='<id>') — what the run FOUND: significant "
            "genes per comparison, the strongest movers, and a QC verdict. "
            "Lead with this, not with a file list.",
            "list_results(run_id='<id>') — the outputs grouped by what they "
            "are, if the user wants the files themselves. Pass a file's "
            "`file_path` verbatim to load_file or download_file.",
            "suggest_apps(run_id='<id>') — which viewer opens these results, "
            "then launch_app to open it (confirm with the user first).",
        ],
    },
    {
        "id": "chain_workspace_steps",
        "name": "Run a chain of steps without keeping a session open",
        "when": "The user wants several commands run one after another in a "
                "workspace (for example a job, then a check on its output, "
                "then the next step only if the check passed). Offer this "
                "BEFORE asking the user to keep this session open between "
                "steps.",
        "steps": [
            "When no step needs a person's decision, the whole chain runs as "
            "ONE workspace job: submit it once and the user can close the "
            "laptop.",
            "Write one script that runs the steps in order under `set -e`, so "
            "the first failing step stops the job. A gate step ends with "
            "`exit 1` when its check fails, so later steps never run on bad "
            "output.",
            "workspace_run(workspace_id=..., command='<the script>', cpus=..., "
            "memory_mb=..., wall_minutes=...): CPUs and memory are those of "
            "the largest step, wall time covers all steps together. It uses "
            "cluster compute, so confirm with the user first.",
            "Tell the user the trade-off: every step gets the largest step's "
            "resources, and nobody can review a result between steps. If a "
            "person must look before the next step, submit those steps as "
            "separate jobs.",
            "workspace_job_status(workspace_id=..., submission_key=...) "
            "later shows how the job ended.",
        ],
    },
]

_RECIPE_TOPICS = {
    "chain_workspace_steps": ("chain", "workspace", "several steps", "unattended",
                              "overnight", "close the laptop", "session open"),
    "start_an_analysis": ("start", "new", "begin", "analys", "launch", "run a",
                          "pipeline", "goal"),
    "diagnose_a_failure": ("fail", "error", "broke", "wrong", "debug",
                           "diagnose", "crash", "log"),
    "find_my_results": ("result", "output", "report", "finished", "done",
                        "download", "app"),
}


@mcp.tool()
def get_started(topic: str = "") -> str:
    """
    How to use Foundry Connect end to end. Call this FIRST when a user wants to
    do something scientific with Foundry but has not named a specific run or
    pipeline. It returns the tool chain for the journeys people arrive with:
    starting an analysis from a goal, working out why a run failed, finding
    results, and running a chain of workspace steps without keeping a session
    open.

    Args:
        topic: Optional. Narrow to one journey, e.g. "start", "failed",
            "results", "chain". An unrecognised topic returns all of them.
    """
    try:
        matched = None
        if topic and topic.strip():
            lowered = topic.strip().lower()
            for recipe_id, keywords in _RECIPE_TOPICS.items():
                if any(k in lowered for k in keywords):
                    matched = recipe_id
                    break

        recipes = ([r for r in _RECIPES if r["id"] == matched]
                   if matched else list(_RECIPES))
        summary = (
            f"Recipe: {recipes[0]['name']}."
            if matched
            else f"All {len(recipes)} Foundry Connect recipes."
        )
        result = envelope(
            summary=summary,
            data={"recipes": recipes},
            next_steps=[
                "Follow the steps of whichever recipe matches what the user "
                "asked for; each step names the exact tool to call.",
                "Launching a run uses HPC compute — always confirm with the "
                "user before initiate_run.",
            ],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error building the getting-started recipes: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Turning a pipeline's inputs into answerable decisions
# ---------------------------------------------------------------------------

# Live run 12194 (RNA-seq Pipeline) has 70 inputs and 61 process-option groups.
# Roughly 20 inputs are reference/index locations an admin set once, ~46 are
# run_* step switches, and a handful are the actual experiment. Only the last
# group is a question for a bench scientist.
_PLAN_MAX_DECISIONS = 8
_PLAN_STEP_PREFIX = "run_"
_PLAN_MAX_LISTED_STEPS = 12

# Inputs whose value is a path AND whose name says "this is the experimental
# design" — the one class of path that must never be hidden as a reference.
_PLAN_DESIGN_HINTS = ("group", "compare", "design", "metadata", "samplesheet",
                      "sample_sheet", "contrast")

_PLAN_GENOME_HINTS = ("genome_build", "build", "species", "organism", "assembly")
_PLAN_LAYOUT_HINTS = ("mate", "paired", "single_end", "read_type", "readtype")

# Curated wording for the inputs that recur across the catalog. Everything else
# falls back to a humanized version of the variable name.
_PLAN_LABELS = {
    "reads": "Which samples to analyse",
    "mate": "Are the reads single-end or paired-end?",
    "genome_build": "Which genome build to align against",
    "groups_file": "Sample groups — which sample belongs to which condition",
    "compare_file": "Which comparisons to make (e.g. treated vs control)",
    "gtf_type": "Which gene annotation source to use",
}

# Tokens that read badly in title case.
_PLAN_ACRONYMS = {"gtf", "bed", "rna", "dna", "umi", "tsv", "csv", "bam", "vcf",
                  "qc", "id", "utr", "tdf", "igv", "sam", "bw"}

_YES_NO = ("yes", "no")


def _is_reference_value(value) -> bool:
    """True for values that point at admin-managed data on disk or the web."""
    return isinstance(value, str) and (
        value.startswith("/") or value.startswith("http://")
        or value.startswith("https://")
    )


def _is_design_input(name: str) -> bool:
    lowered = (name or "").lower()
    return any(hint in lowered for hint in _PLAN_DESIGN_HINTS)


def _humanize_input_name(name: str) -> str:
    """Turn `replace_geneID_with_geneName` into something readable."""
    words = [w for w in re.split(r"[_\s]+", name or "") if w]
    if not words:
        return name or ""
    out = []
    for i, word in enumerate(words):
        if word.lower() in _PLAN_ACRONYMS:
            out.append(word.upper())
        elif i == 0:
            out.append(word[0].upper() + word[1:])
        else:
            out.append(word)
    return " ".join(out)


def _decision_for(inp):
    """Build one decision entry from a run input, or None if it is plumbing."""
    name, value = inp["name"], inp["value"]

    if inp["type"] == "vmetaCollection":
        entry = {
            "label": _PLAN_LABELS.get(name, "Which samples to analyse"),
            "input": name, "kind": "samples", "current": value, "allowed": None,
        }
        if inp.get("vmetaCollectionId"):
            entry["vmetaCollectionId"] = inp["vmetaCollectionId"]
        return entry

    lowered = (name or "").lower()
    if _is_design_input(name):
        kind = "design"
    elif _is_reference_value(value):
        return None  # admin-managed reference or index location
    elif any(hint in lowered for hint in _PLAN_GENOME_HINTS):
        kind = "genome"
    elif any(hint in lowered for hint in _PLAN_LAYOUT_HINTS):
        kind = "reads_layout"
    else:
        kind = "setting"

    allowed = list(_YES_NO) if str(value).lower() in _YES_NO else None
    return {
        "label": _PLAN_LABELS.get(name, _humanize_input_name(name)),
        "input": name, "kind": kind, "current": value, "allowed": allowed,
    }


# Samples first, then the experimental design, then what to align against, then
# read layout, then the aggregated step switches, then everything else.
_PLAN_KIND_ORDER = {"samples": 0, "design": 1, "genome": 2, "reads_layout": 3,
                    "steps": 4, "setting": 5}


def _plan_decisions(details):
    """Reduce a run's inputs to the decisions worth asking about.

    Returns (decisions, stats). Nothing is dropped silently: the stats say how
    many reference paths were hidden and how many settings did not fit.
    """
    step_states = {}
    candidates = []
    hidden_references = 0

    for inp in _iter_run_inputs(details.get("inputs") or []):
        name = inp["name"] or ""
        if name.startswith(_PLAN_STEP_PREFIX) and inp["type"] != "vmetaCollection":
            step_states[name[len(_PLAN_STEP_PREFIX):]] = str(inp["value"]).lower()
            continue
        decision = _decision_for(inp)
        if decision is None:
            hidden_references += 1
        else:
            candidates.append(decision)

    if step_states:
        enabled = [s for s, v in step_states.items() if v in ("yes", "true", "1")]
        candidates.append({
            "label": "Which analysis steps to run",
            "kind": "steps",
            "current": f"{len(enabled)} of {len(step_states)} steps enabled",
            "enabled": [s.replace("_", " ") for s in enabled[:_PLAN_MAX_LISTED_STEPS]],
            "more_enabled": max(0, len(enabled) - _PLAN_MAX_LISTED_STEPS),
            "disabled_count": len(step_states) - len(enabled),
            "allowed": list(_YES_NO),
        })

    candidates.sort(key=lambda d: _PLAN_KIND_ORDER.get(d["kind"], 9))
    decisions = candidates[:_PLAN_MAX_DECISIONS]
    stats = {
        "hidden_reference_paths": hidden_references,
        "further_settings_not_shown": max(0, len(candidates) - len(decisions)),
        "process_option_groups": len(details.get("processOptions") or {}),
    }
    return decisions, stats


@mcp.tool()
def plan_run(pipeline_id: int, run_id: str = "") -> str:
    """
    Show the handful of decisions a scientist actually has to make to run a
    pipeline — samples, experimental design, genome, which steps — with the
    current value of each, taken from a real working run.

    Use this after recommend_pipeline / list_featured_pipelines and BEFORE
    duplicate_run. It deliberately hides the reference and index paths an admin
    set once and the process-option groups, which together are the bulk of a
    pipeline's inputs (live: 70 inputs and 61 option groups on RNA-seq) and are
    not decisions a bench scientist should be asked to make.

    Args:
        pipeline_id: The pipeline to plan a run for.
        run_id: Optional. Plan from this specific run instead of the most
            recent successful one.
    """
    try:
        via_client = get_client()
        logger.info(f"Planning a run for pipeline {pipeline_id} (run_id: '{run_id}')")

        example = None
        if run_id:
            example = {"id": run_id, "name": None, "dateCreated": None}
        else:
            example = _find_example_run(via_client, pipeline_id, strict=True)
            if not example:
                result = envelope(
                    summary=(
                        f"Pipeline {pipeline_id} has no successful run to plan "
                        "from, so there is no known-good configuration to show."
                    ),
                    data={"decisions": [], "pipeline": {"id": pipeline_id}},
                    next_steps=[
                        "Call list_runs(search_query=...) to look for a run on "
                        "this pipeline in any state, then plan_run(run_id=...).",
                        "Or pick a different pipeline with "
                        "recommend_pipeline(goal) — one with a working example "
                        "is far easier to adapt.",
                    ],
                )
                return json.dumps(result, indent=2)

        details = via_client.call(
            method="GET", endpoint=f"/api/v1/run/{example['id']}/details"
        )
        decisions, stats = _plan_decisions(details)
        pipeline = details.get("mainPipeline") or {}

        data = {
            "pipeline": {"id": pipeline.get("id", pipeline_id),
                         "name": pipeline.get("name"),
                         "version": pipeline.get("version")},
            "based_on_run": {"id": example["id"],
                             "name": example.get("name") or details.get("name"),
                             "dateCreated": example.get("dateCreated")},
            "decisions": decisions,
        }
        data.update(stats)

        result = envelope(
            summary=(
                f"{len(decisions)} decision(s) to run '{pipeline.get('name')}', "
                f"based on run {example['id']}. "
                f"{stats['hidden_reference_paths']} reference/index paths and "
                f"{stats['process_option_groups']} process-option groups are "
                "hidden — they are already set correctly."
            ),
            data=data,
            next_steps=[
                "Confirm or change the values above with the user first.",
                f"Then: create_run(pipeline_id={pipeline.get('id', pipeline_id)}, "
                "project_id=..., name=...) -> update_run with the answers, run "
                "environment and execution directory -> preflight_run -> "
                "initiate_run to launch.",
                f"Or, to copy this example's settings: duplicate_run(run_id="
                f"'{example['id']}', project_id=..., pipeline_id="
                f"{pipeline.get('id', pipeline_id)}) -> update_run on the new run.",
                "Call get_run_details(run_id='%s', verbose=True) for the full "
                "editable config that update_run needs." % example["id"],
                "Launching a run uses HPC compute — confirm with the user "
                "before initiate_run.",
            ],
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error planning a run for pipeline {pipeline_id}: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Process/Pipeline Management Tools
# ============================================================================

@mcp.tool()
def list_all_processes() -> str:
    """
    List all processes/pipelines in Foundry Connect. Returns details including
    process ID, name, summary, and owner information.
    """
    try:
        via_client = get_client()
        logger.info("Listing all processes")
        processes = via_client.process.list_processes()
        result = serialize_response(processes)
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error listing all processes: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_process_details(process_id: str) -> str:
    """
    Get detailed information about a specific process/pipeline by ID.
    Returns complete process configuration, scripts, parameters, and metadata.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting details for process {process_id}")
        process = via_client.process.get_process(process_id)
        result = serialize_response(process)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting process details: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_process_revisions(process_id: str) -> str:
    """
    Get revision history for a specific process/pipeline.
    Returns all versions and their changes over time.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting revisions for process {process_id}")
        
        revisions = via_client.process.get_process_revisions(process_id)
        result = serialize_response(revisions)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting process revisions: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def compare_process_revisions(process_id: str, revision_a: str, revision_b: str) -> str:
    """
    Compare two revisions of a process and return what changed between them.

    Returns a unified diff per changed file, covering only authored content:
    process.yaml, the input and output schemas, README.md and bin/ scripts.
    Generated files are excluded because they are rebuilt from platform wide
    settings and would show changes the author did not make.

    revision_a and revision_b are the revision row ids returned by
    get_process_revisions, not the human facing version numbers.
    """
    try:
        via_client = get_client()
        logger.info(f"Comparing revisions {revision_a} and {revision_b} of process {process_id}")

        response = requests.get(
            f"{via_client.auth.hostname.rstrip('/')}/api/v1/process/{process_id}/revisions/compare",
            params={"a": revision_a, "b": revision_b},
            headers=via_client.auth.get_headers(),
            timeout=(20, 120),
        )
        response.raise_for_status()
        return json.dumps(serialize_response(response.json()), indent=2)
    except Exception as e:
        logger.error(f"Error comparing process revisions: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_process_parameters() -> str:
    """
    List all available parameters in Foundry Connect.
    Returns parameter definitions including name, type, and constraints.
    """
    try:
        via_client = get_client()
        logger.info("Listing all process parameters")
        
        parameters = via_client.process.list_parameters()
        result = serialize_response(parameters)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing process parameters: {e}")
        return json.dumps({"error": str(e)})




@mcp.tool()
def duplicate_process(process_id: str) -> str:
    """
    Duplicate/clone an existing process/pipeline.
    Creates a copy of the specified process that can be modified independently.
    """
    try:
        via_client = get_client()
        logger.info(f"Duplicating process {process_id}")
        
        duplicated = via_client.process.duplicate_process(process_id)
        result = serialize_response(duplicated)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error duplicating process: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_process_parameters(
    name: str = None,
    qualifier: str = None,
    file_type: str = None,
    id: str = None
) -> str:
    """
    Get parameters filtered by name, qualifier, file type, or ID.
    Returns parameters matching the specified criteria.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting process parameters")
        
        filtered = via_client.process.get_parameters(
            name=name,
            qualifier=qualifier,
            fileType=file_type,
            id_=id
        )
        result = serialize_response(filtered)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting process parameters: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_process_config(
    name: str,
    menu_group_name: str,
    script_body: str,
    input_params: list = None,
    output_params: list = None,
    summary: str = None,
    script_language: str = "Shell",
    permission_settings: dict = None,
    revision_comment: str = None
) -> str:
    """
    Generate a full process configuration using menu group and parameters.
    Creates a complete process definition ready for creation.

    input_params and output_params: Array of parameter dicts to reference existing parameters.
    Each dict should contain the following fields:
        - name (str, required): Name of an existing parameter to match (e.g., "reads", "FastQCout").
          Use list_process_parameters or get_process_parameters to find available parameter names.
        - qualifier (str, required): Parameter qualifier - "file", "set", "val", "each", or "env".
        - fileType (str, required): File type of the parameter (e.g., "fastq", "html", "bam", "csv").
        - displayName (str, optional): Display label for this parameter within the process context.
          If omitted, defaults to the matched parameter's name.
        - optional (bool, optional): Whether the parameter is optional. Defaults to false.
        - test (str, optional): Test value for the parameter (e.g., "testfile.csv").

    Parameters are matched by name + qualifier + fileType against existing server parameters.
    If no match is found, a new parameter is automatically created.

    Example:
        input_params=[{"name": "reads", "displayName": "input_reads", "qualifier": "set", "fileType": "fastq"}]
        output_params=[{"name": "FastQCout", "displayName": "fastqc_report", "qualifier": "file", "fileType": "html"}]
    """
    try:
        via_client = get_client()
        logger.info(f"Creating process config for {name}")
        
        # Format script_body like in the notebook
        formatted_script = f"script:\n\"\"\"\n{script_body}\n\"\"\""
        
        # Build kwargs dict, only including non-None values
        config_kwargs = {
            "name": name,
            "menu_group_name": menu_group_name,
            "script_body": formatted_script,
        }
        if input_params is not None:
            config_kwargs["input_params"] = input_params
        if output_params is not None:
            config_kwargs["output_params"] = output_params
        if summary is not None:
            config_kwargs["summary"] = summary
        if script_language is not None:
            config_kwargs["script_language"] = script_language
        if permission_settings is not None:
            config_kwargs["permission_settings"] = permission_settings
        if revision_comment is not None:
            config_kwargs["revision_comment"] = revision_comment
        
        result_obj = via_client.process.create_process_config(**config_kwargs)
        result = serialize_response(result_obj)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating process config: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_process(process_data: dict) -> str:
    """
    Create a new custom process/pipeline.
    Requires complete process configuration including scripts and parameters.
    Use the output from create_process_config as the process_data input.
    """
    try:
        via_client = get_client()
        logger.info(f"Creating process: {process_data.get('name', 'unnamed')}")
        
        cleaned_data = remove_none(process_data)

        # Import ProcessConfig model from SDK
        from viafoundry.models.domain.process import ProcessConfig
        
        # Reconstruct ProcessConfig object from dict
        try:
            process_config = ProcessConfig.model_validate(cleaned_data)
            logger.debug(f"ProcessConfig validated successfully: {process_config.name}")
        except Exception as e:
            logger.error(f"Failed to validate ProcessConfig: {e}")
            return json.dumps({
                "error": f"Invalid process configuration: {str(e)}",
                "details": "The process_data must match the output from create_process_config"
            }, indent=2)
        
        # SDK accepts ProcessConfig object as process_data argument
        try:
            process = via_client.process.create_process(process_data=process_config)
            result = serialize_response(process)
            return json.dumps(result, indent=2)
        except Exception as e:
            error_details = {
                "error": str(e),
                "error_type": type(e).__name__,
            }
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                error_details["api_response"] = e.response.text
            if hasattr(e, 'status_code'):
                error_details["status_code"] = e.status_code
            logger.error(f"Error creating process: {error_details}")
            return json.dumps(error_details, indent=2)
            
    except Exception as e:
        logger.error(f"Error creating process: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def update_process(process_id: str, process_data: dict) -> str:
    """
    Update an existing process/pipeline.
    Modifies process configuration, scripts, or parameters.

    Ports (inputParameters / outputParameters):
    - Leave a side out to keep its ports exactly as stored. A script-only edit
      needs neither list.
    - A side you send REPLACES every port on that side: stored ports you leave
      out of the list are deleted.
    - Each port you send is matched to a stored port by its "id", or else by
      its displayName, and that stored port is updated in place, keeping its id
      and any field you did not set (such as an output's regEx). Ports that
      match nothing are added as new ports. Use get_process_details for ids.
    - To delete every port on a side, send an empty list and set
      removeAllInputParameters or removeAllOutputParameters to true.
    The result lists, per side, which ports were kept, updated, added and removed.

    WARNING: This modifies a persistent resource on the Foundry Connect server.
    Changes affect all users who reference this process and cannot be undone
    automatically. The tool performs an ownership check before updating —
    if the process is owned by a different user, the update will be rejected.
    Use duplicate_process to create your own copy instead.
    """
    try:
        via_client = get_client()
        logger.info(f"Updating process {process_id}")

        # --- Ownership guard: fetch process and verify before updating ---
        try:
            # Read the raw response: the stored ports are needed with every
            # field, and the SDK response model drops some of them.
            existing_data = via_client.call(method="GET", endpoint=f"/api/v1/process/{process_id}")
            if not isinstance(existing_data, dict):
                raise ValueError("unexpected response when reading the process")
            process_owner_id = existing_data.get("owner_id")
            process_name = existing_data.get("name", "unknown")
            last_modified_by = existing_data.get("last_modified_user", "unknown")
            logger.info(f"Process '{process_name}' (id={process_id}) owned by user_id={process_owner_id}, last modified by {last_modified_by}")
        except Exception as e:
            logger.error(f"Failed to fetch process {process_id} for ownership check: {e}")
            return json.dumps({
                "error": f"Cannot verify process ownership: {str(e)}",
                "hint": "Ensure the process_id is valid and you have read access."
            }, indent=2)

        # Resolve current user identity
        current_user_id = None
        try:
            user_info = via_client.call(method="GET", endpoint="/api/v1/auth/user")
            if isinstance(user_info, dict):
                current_user_id = user_info.get("id")
        except Exception as e:
            logger.error(f"Failed to resolve current user identity: {e}")
            return json.dumps({
                "error": "Ownership check failed",
                "detail": "Could not resolve current user identity. Update refused.",
                "hint": "Ensure your Foundry Connect authentication is configured correctly."
            }, indent=2)

        # Enforce ownership
        if process_owner_id is not None and int(current_user_id) != int(process_owner_id):
            logger.warning(f"Ownership mismatch: current user {current_user_id} != process owner {process_owner_id}")
            return json.dumps({
                "error": "Ownership check failed",
                "detail": f"Process '{process_name}' (id={process_id}) is owned by user_id={process_owner_id}. "
                          f"Your user_id is {current_user_id}. You cannot update another user's process.",
                "hint": "Use duplicate_process to create your own copy, then modify that."
            }, indent=2)

        # --- Validate and clean process_data ---
        cleaned_data = remove_none(process_data)

        from viafoundry.models.domain.process import ConfigParameter, ProcessConfig

        # An SDK without the port id field drops every id during validation,
        # and the server then deletes and recreates every port. Refuse instead.
        if "id" not in ConfigParameter.model_fields or "regEx" not in ConfigParameter.model_fields:
            return json.dumps({
                "error": "This Foundry MCP server is running an SDK that cannot keep process port ids.",
                "detail": "Updating the process now would delete and recreate every port. Update refused.",
                "hint": "Upgrade viafoundry_sdk to 1.1.0 or later."
            }, indent=2)

        try:
            cleaned_data, port_changes = build_update_body(cleaned_data, existing_data)
        except PortMergeError as e:
            return json.dumps({
                "error": "Ports could not be applied",
                "detail": str(e),
                "hint": "Nothing was changed. Call get_process_details for the stored ports and their ids."
            }, indent=2)

        try:
            process_config = ProcessConfig.model_validate(cleaned_data)
        except Exception as e:
            logger.error(f"Failed to validate ProcessConfig for update: {e}")
            return json.dumps({
                "error": f"Invalid process configuration: {str(e)}",
                "details": "The process_data must be a valid process configuration dict."
            }, indent=2)

        # --- Perform the update ---
        try:
            updated = via_client.process.update_process(process_id, process_config)
            result = serialize_response(updated)
            if isinstance(result, dict):
                result["port_changes"] = port_changes
            return json.dumps(result, indent=2)
        except Exception as e:
            error_details = {
                "error": str(e),
                "error_type": type(e).__name__,
            }
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                error_details["api_response"] = e.response.text
            if hasattr(e, 'status_code'):
                error_details["status_code"] = e.status_code
            logger.error(f"Error updating process: {error_details}")
            return json.dumps(error_details, indent=2)

    except Exception as e:
        logger.error(f"Error updating process: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_process_parameter(parameter_data: dict) -> str:
    """
    Create a new parameter for processes.
    Defines a new parameter that can be used across multiple processes.
    """
    try:
        via_client = get_client()
        logger.info(f"Creating process parameter")
        
        param = via_client.process.create_parameter(parameter_data)
        result = serialize_response(param)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating process parameter: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Menu Group Management Tools
# ============================================================================

@mcp.tool()
def create_menu_group(menu_name: str) -> str:
    """
    Create a new menu group for organizing processes.
    Menu groups help organize processes in the UI.
    """
    try:
        via_client = get_client()
        logger.info(f"Creating menu group: {menu_name}")
        
        menu = via_client.process.create_menu_group(name=menu_name)
        result = serialize_response(menu)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating menu group: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_menu_groups() -> str:
    """
    List all menu groups in Foundry Connect.
    Returns all available menu groups used for process organization.
    """
    try:
        via_client = get_client()
        logger.info(f"Listing menu groups")
        
        menus = via_client.process.list_menu_groups()
        result = serialize_response(menus)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing menu groups: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_menu_group_by_name(group_name: str) -> str:
    """
    Find a menu group by its name.
    Returns the menu group ID for the specified name.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting menu group by name: {group_name}")
        
        menu = via_client.process.get_menu_group_by_name(group_name)
        result = serialize_response(menu)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting menu group by name: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Metadata & Dataset Search Tools
# ============================================================================

DATASET_LIST_SCOPES = ("mine", "shared", "public", "all")


def _dataset_scope_filter(via_client, scope: str) -> dict:
    """Build the dataset-search filter for a scope, as the web Datasets tabs do.

    "mine" and "shared" compare against the caller's vmeta user id, read from
    GET /api/v1/auth/user. "public" and "all" need no lookup.
    """
    if scope == "all":
        return {}
    if scope == "public":
        return {"perms.read.user": {"in": "everyone"}}
    user_info = via_client.call(method="GET", endpoint="/api/v1/auth/user")
    vmeta_user_id = ((user_info or {}).get("vmeta") or {}).get("userId")
    if not vmeta_user_id:
        raise ValueError("Could not resolve your dataset owner id from /api/v1/auth/user")
    if scope == "mine":
        return {"owner._id": {"eq": vmeta_user_id}}
    return {"owner._id": {"ne": vmeta_user_id}, "perms.read.user": {"nin": "everyone"}}


def _summarize_dataset(row: dict) -> dict:
    """Trim a dataset-search row to what a model needs to pick a dataset."""
    owner = row.get("owner") or {}
    return {
        "id": row.get("_id"),
        "name": row.get("name"),
        "owner": owner.get("username") if isinstance(owner, dict) else owner,
        "file_count": row.get("fileCount"),
        "canvas_ids": list((row.get("file_ids") or {}).keys()),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
        "tags": [t.get("name") for t in (row.get("tags") or []) if isinstance(t, dict) and t.get("name")],
    }


@mcp.tool()
def list_datasets(
    scope: str = "mine",
    search_query: str = "",
    take: int = 20,
    skip: int = 0,
    sort: str = "updatedAt",
    order: str = "desc",
) -> str:
    """
    List vmeta datasets (sample/file sets) you can use, newest first.
    Use this to find a dataset ID, e.g. to set a run's reads input
    (update_run with type "vmetaCollection") or to inspect its files with
    search_datasets / list_files.

    Args:
        scope: Which datasets to list (default "mine"):
            - "mine": datasets you own.
            - "shared": datasets other users shared with you or your groups (not public ones).
            - "public": datasets readable by everyone.
            - "all": everything you can read (mine + shared + public).
        search_query: Case-insensitive text to match in the dataset name (optional).
        take: Page size (default 20).
        skip: Offset for pagination (default 0).
        sort: Sort field, e.g. "updatedAt" (default), "createdAt", "name".
        order: "desc" (default) or "asc".

    Returns:
        {"scope", "total", "returned", "datasets": [{id, name, owner, file_count,
        canvas_ids, created_at, updated_at, tags}]}. "total" counts all matches,
        so page with skip when total > returned.
    """
    try:
        scope = (scope or "mine").strip().lower()
        if scope not in DATASET_LIST_SCOPES:
            return json.dumps({
                "error": f"Unknown scope '{scope}'. Use one of: {', '.join(DATASET_LIST_SCOPES)}."
            })
        order = order if order in ("asc", "desc") else "desc"

        via_client = get_client()
        flt = _dataset_scope_filter(via_client, scope)
        if search_query and search_query.strip():
            flt["name"] = {"regex": re.escape(search_query.strip())}

        body = {"take": take, "skip": skip, "sort": sort or "updatedAt", "order": order}
        if flt:
            body["filter"] = flt

        logger.info(f"Listing datasets (scope: {scope}, search: '{search_query}', take: {take}, skip: {skip})")
        response = via_client.call(method="POST", endpoint="/api/v1/vmeta/dataset/search", data=body)

        rows = response.get("data", []) if isinstance(response, dict) else []
        datasets = [_summarize_dataset(r) for r in rows if isinstance(r, dict)]
        return json.dumps({
            "scope": scope,
            "total": response.get("results", len(datasets)) if isinstance(response, dict) else len(datasets),
            "returned": len(datasets),
            "datasets": datasets,
        }, indent=2)
    except Exception as e:
        logger.error(f"Error listing datasets: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def search_datasets(dataset_id: str, filter_data: dict = None) -> str:
    """
    Search for files in a vmeta dataset with filtering, sorting, and pagination.
    This searches the FILES INSIDE one known dataset. To find datasets
    themselves (and their IDs), use list_datasets.

    Args:
        dataset_id: ID of the vmeta dataset to search files in.
        filter_data: Search options dict with the following keys:
            - filter: Filter criteria dict. Examples:
                - {"name": "README"} - exact match
                - {"name": {"regex": "README"}} - regex/partial match (case-insensitive)
                - {"owner.username": "admin@example.com"} - nested field match
                - {"createdAt": {"gte": "2024-01-01"}} - date comparison (gt, gte, lt, lte)
                - {"status": {"in": ["active", "pending"]}} - match any value in list
                - {"status": {"ne": "deleted"}} - not equal
            - sort: Field name to sort by (e.g., "name", "createdAt")
            - order: Sort order - "asc" or "desc"
            - fields: Comma-separated field names to return (e.g., "name,file1,createdAt")
            - take: Max number of results to return (pagination)
            - skip: Number of results to skip (pagination)
    
    Returns:
        Matching files with their metadata and field type information.
    
    Examples:
        # Search by exact name
        search_datasets(dataset_id="abc123", filter_data={"filter": {"name": "README"}})
        
        # Search with regex and pagination
        search_datasets(
            dataset_id="abc123",
            filter_data={
                "filter": {"name": {"regex": "sample"}},
                "sort": "createdAt",
                "order": "desc",
                "take": 10,
                "skip": 0
            }
        )
        
        # Search with date filter
        search_datasets(
            dataset_id="abc123",
            filter_data={"filter": {"createdAt": {"gte": "2024-01-01"}}}
        )
    """
    try:
        via_client = get_client()
        logger.info(f"Searching files in dataset {dataset_id}" +
                   (f" with options: {filter_data}" if filter_data else ""))
        
        datasets = via_client.metadata.search_dataset_files(
            dataset_id=dataset_id,
            filter_data=filter_data
        )
        
        result = serialize_response(datasets)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error searching dataset files: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def search_collections(filter_data: dict = None) -> str:
    """
    Search for collections in Foundry Connect metadata system with filtering, sorting, and pagination.
    Collections are groups of related datasets (e.g., "Files", "Samples").
    
    Args:
        filter_data: Search options dict with the following keys:
            - filter: Filter criteria dict. Examples:
                - {"name": "file"} - exact match
                - {"name": {"regex": "file"}} - regex/partial match (case-insensitive)
                - {"label": "Files"} - filter by label
                - {"active": true} - filter by active status
            - sort: Field name to sort by (e.g., "name", "createdAt")
            - order: Sort order - "asc" or "desc"
            - fields: Comma-separated field names to return (e.g., "name,label,canvasID")
            - take: Max number of results to return (pagination)
            - skip: Number of results to skip (pagination)
    
    Returns:
        Matching collections with their metadata.
    
    Examples:
        # List all collections (no filter)
        search_collections()
        
        # Search by exact name
        search_collections(filter_data={"filter": {"name": "file"}})
        
        # Search with regex and pagination
        search_collections(filter_data={
            "filter": {"name": {"regex": "sample"}},
            "sort": "createdAt",
            "order": "desc",
            "take": 10
        })
    """
    try:
        via_client = get_client()
        logger.info(f"Searching collections" +
                   (f" with options: {filter_data}" if filter_data else ""))
        
        collections = via_client.metadata.search_collections(filter_data)
        result = serialize_response(collections)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error searching collections: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_collection_details(collection_id: str) -> str:
    """
    Get detailed information about a specific collection by ID.
    Returns collection metadata and associated datasets.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting details for collection {collection_id}")
        collection = via_client.metadata.get_collection(collection_id)
        result = serialize_response(collection)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting collection details: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_collection(collection_data: dict) -> str:
    """
    Create a new dataset collection.
    Collections group related datasets together for organization.
    
    Args:
        collection_data: Dict containing collection properties:
            - name (str, required): Name of the collection.
            - label (str, required): Display label for easy identification.
            - canvasID (str, required): The canvas ID that the collection belongs to.
            - _id (str, optional): Custom unique identifier for the collection.
            - version (int, optional): Version number of the collection.
            - dataPerms (list, optional): Data-level permission settings.
            - perms (list, optional): Collection-level permission settings.
            - dataDeleteProtected (bool, optional): Whether to protect data from deletion.
    
    Returns:
        The created collection data.
    
    Example:
        create_collection(collection_data={
            "name": "samples",
            "label": "Sample Collection",
            "canvasID": "65c21d6a593f32e0103daf25"
        })
    """
    try:
        via_client = get_client()
        logger.info(f"Creating collection: {collection_data.get('name', 'unnamed')}")
        
        collection = via_client.metadata.create_collection(collection_data)
        result = serialize_response(collection)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating collection: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def add_files_to_dataset(dataset_id: str, file_data: dict) -> str:
    """
    Add a file to an existing dataset.
    Associates a file with a dataset.
    
    Args:
        dataset_id: ID of the dataset to add the file to.
        file_data: Dict containing file information:
            - canvasId (str, required): The ID of the study tracker canvas.
            - file (dict, required): File object with file metadata/properties.
    
    Returns:
        Confirmation of file addition.
    
    Example:
        add_files_to_dataset(
            dataset_id="6984ba1e8518d10eb6fe636d",
            file_data={
                "canvasId": "66269972dc000cff1c8a54b0",
                "file": {"name": "sample.fastq", "path": "/data/sample.fastq"}
            }
        )
    """
    try:
        via_client = get_client()
        logger.info(f"Adding file to dataset {dataset_id}: {file_data}")
        
        result_obj = via_client.metadata.add_files_to_dataset(dataset_id, file_data)
        result = serialize_response(result_obj)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error adding files to dataset: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_collection_fields(collection_id: str) -> str:
    """
    Get metadata fields associated with a specific collection.
    Returns the schema/structure of metadata for the collection.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting fields for collection {collection_id}")
        
        fields = via_client.metadata.get_collection_fields(collection_id)
        result = serialize_response(fields)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting collection fields: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Metadata Canvas Tools
# ============================================================================

@mcp.tool()
def search_canvas(filter_data: dict = None) -> str:
    """
    Search for canvas visualizations in Foundry Connect with filtering, sorting, and pagination.
    Canvas objects represent data visualizations and dashboards.
    
    Args:
        filter_data: Search options dict with the following keys:
            - filter: Filter criteria dict. Examples:
                - {"name": "dashboard"} - exact match
                - {"name": {"regex": "dash"}} - regex/partial match (case-insensitive)
                - {"label": "My Dashboard"} - filter by label
                - {"active": true} - filter by active status
            - sort: Field name to sort by (e.g., "name", "createdAt")
            - order: Sort order - "asc" or "desc"
            - fields: Comma-separated field names to return
            - take: Max number of results to return (pagination)
            - skip: Number of results to skip (pagination)
    
    Returns:
        Matching canvas items with their metadata.
    
    Examples:
        # List all canvas items (no filter)
        search_canvas()
        
        # Search by exact name
        search_canvas(filter_data={"filter": {"name": "dashboard"}})
        
        # Search with regex and pagination
        search_canvas(filter_data={
            "filter": {"name": {"regex": "sample"}},
            "sort": "createdAt",
            "order": "desc",
            "take": 10
        })
    """
    try:
        via_client = get_client()
        logger.info(f"Searching canvas" +
                   (f" with options: {filter_data}" if filter_data else ""))
        
        canvas_results = via_client.metadata.search_canvas(filter_data)
        result = serialize_response(canvas_results)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error searching canvas: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_canvas_details(canvas_id: str) -> str:
    """
    Get detailed information about a specific canvas by ID.
    Returns canvas configuration, fields, and visualization settings.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting details for canvas {canvas_id}")
        
        canvas = via_client.metadata.get_canvas(canvas_id)
        result = serialize_response(canvas)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting canvas details: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_canvas_fields(canvas_id: str) -> str:
    """
    Get metadata fields associated with a specific canvas.
    Returns the schema/structure used by the canvas visualization.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting fields for canvas {canvas_id}")
        
        fields = via_client.metadata.get_canvas_fields(canvas_id)
        result = serialize_response(fields)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting canvas fields: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_canvas(canvas_data: dict) -> str:
    """
    Create a new canvas visualization/dashboard.
    Defines a new data visualization or analysis dashboard.
    """
    try:
        via_client = get_client()
        logger.info(f"Creating canvas")
        
        canvas = via_client.metadata.create_canvas(canvas_data)
        result = serialize_response(canvas)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating canvas: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Metadata Fields Tools
# ============================================================================

@mcp.tool()
def search_metadata_fields(filter_data: dict = None) -> str:
    """
    Search for metadata field definitions in Foundry Connect with filtering, sorting, and pagination.
    Fields define the schema for metadata records.
    
    Args:
        filter_data: Search options dict with the following keys:
            - filter: Filter criteria dict. Examples:
                - {"name": "field_name"} - exact match
                - {"name": {"regex": "field"}} - regex/partial match (case-insensitive)
                - {"type": "String"} - filter by field type
            - sort: Field name to sort by (e.g., "name", "createdAt")
            - order: Sort order - "asc" or "desc"
            - fields: Comma-separated field names to return
            - take: Max number of results to return (pagination)
            - skip: Number of results to skip (pagination)
    
    Returns:
        Matching field definitions with their types and constraints.
    
    Examples:
        # List all fields (no filter)
        search_metadata_fields()
        
        # Search by name
        search_metadata_fields(filter_data={"filter": {"name": "sample_id"}})
    """
    try:
        via_client = get_client()
        logger.info(f"Searching metadata fields" +
                   (f" with options: {filter_data}" if filter_data else ""))
        
        fields = via_client.metadata.search_fields(filter_data)
        result = serialize_response(fields)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error searching metadata fields: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_field_details(field_id: str) -> str:
    """
    Get detailed information about a specific metadata field.
    Returns field definition, type, constraints, and usage.
    """
    try:
        via_client = get_client()
        logger.info(f"Getting field details for field {field_id}")
        
        field = via_client.metadata.get_field(field_id)
        result = serialize_response(field)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting field details: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_metadata_field(field_data: dict) -> str:
    """
    Create a new metadata field definition.
    Defines a new field that can be used in metadata records.
    """
    try:
        via_client = get_client()
        logger.info(f"Creating metadata field")
        
        field = via_client.metadata.create_field(field_data)
        result = serialize_response(field)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating metadata field: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Metadata Records Tools
# ============================================================================

@mcp.tool()
def search_metadata_records(canvas_id: str, collection_name: str, filter_data: dict = None) -> str:
    """
    Search for metadata records (data entries) in a Foundry Connect canvas collection.
    Metadata records contain actual data values for defined fields.
    
    Args:
        canvas_id: The canvas ID to search in.
        collection_name: Name of the collection to search in (e.g., "file", "sample").
        filter_data: Optional search options dict with the following keys:
            - filter: Filter criteria dict. Examples:
                - {"name": "record_name"} - exact match
                - {"name": {"regex": "sample"}} - regex/partial match (case-insensitive)
                - {"status": "active"} - filter by status
            - sort: Field name to sort by (e.g., "name", "createdAt")
            - order: Sort order - "asc" or "desc"
            - fields: Comma-separated field names to return
            - take: Max number of results to return (pagination)
            - skip: Number of results to skip (pagination)
    
    Returns:
        Matching records with their field values.
    
    Examples:
        # List all records in "file" collection
        search_metadata_records(canvas_id="abc123", collection_name="file")
        
        # Search by name in "sample" collection
        search_metadata_records(canvas_id="abc123", collection_name="sample", filter_data={"filter": {"name": "sample_001"}})
    """
    try:
        via_client = get_client()
        logger.info(f"Searching metadata records in canvas '{canvas_id}', collection '{collection_name}'" +
                   (f" with options: {filter_data}" if filter_data else ""))
        
        records = via_client.metadata.search_data(canvas_id, collection_name, filter_data)
        result = serialize_response(records)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error searching metadata records: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_metadata_record(canvas_id: str, collection_name: str, data_id: str) -> str:
    """
    Get a specific metadata record by ID.
    Returns the complete metadata record with all field values.
    
    Args:
        canvas_id: The canvas ID where the collection exists.
        collection_name: Name of the collection containing the record.
        data_id: The unique ID of the data record to retrieve.
    
    Returns:
        The data record with all field values.
    
    Example:
        get_metadata_record(
            canvas_id="65c21d6a593f32e0103daf25",
            collection_name="samples",
            data_id="66269972dc000cff1c8a54b0"
        )
    """
    try:
        via_client = get_client()
        logger.info(f"Getting metadata record {data_id} from canvas '{canvas_id}', collection '{collection_name}'")
        
        record = via_client.metadata.get_data(canvas_id, collection_name, data_id)
        result = serialize_response(record)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting metadata record: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_metadata_record(canvas_id: str, collection_name: str, data_entry: dict) -> str:
    """
    Create a new metadata data record in a collection.
    Adds a new entry with field values to the metadata system.
    
    Args:
        canvas_id: The canvas ID where the collection exists.
        collection_name: Name of the collection to add the record to.
        data_entry: Dict containing the record data. Can include any fields
            except reserved keys. Optional 'perms' field for permissions.
    
    Returns:
        The created data entry.
    
    Example:
        create_metadata_record(
            canvas_id="65c21d6a593f32e0103daf25",
            collection_name="samples",
            data_entry={"name": "Sample001", "status": "active"}
        )
    """
    try:
        via_client = get_client()
        logger.info(f"Creating metadata record in canvas '{canvas_id}', collection '{collection_name}': {data_entry}")
        
        record = via_client.metadata.create_data(canvas_id, collection_name, data_entry)
        result = serialize_response(record)
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating metadata record: {e}")
        return json.dumps({"error": str(e)})


# Keys that identify a record or control who can see it. The update tools
# refuse them so a field edit can never change a record's identity or access.
RESERVED_RECORD_KEYS = ("_id", "owner", "perms", "DID")


def _record_update_problem(data_id, update_data):
    """Return why an update cannot be sent, or None when it is valid."""
    if not isinstance(data_id, str) or not data_id.strip():
        return "data_id is required"
    if not isinstance(update_data, dict) or not update_data:
        return "update_data must be a non-empty object"
    reserved = [key for key in RESERVED_RECORD_KEYS if key in update_data]
    if reserved:
        return f"update_data contains reserved keys that cannot be updated: {', '.join(reserved)}"
    return None


@mcp.tool()
def update_metadata_record(canvas_id: str, collection_name: str, data_id: str, update_data: dict) -> str:
    """
    Update fields on an existing metadata record.
    Only the keys in update_data change; every other field keeps its value.

    WARNING: This modifies a persistent record on the Foundry Connect server
    and cannot be undone automatically. Show the user the record and the new
    values and get confirmation before calling it. To change many records,
    use update_metadata_records so the user confirms the whole set once.

    Args:
        canvas_id: The canvas ID where the collection exists.
        collection_name: Name of the collection that holds the record.
        data_id: The record's _id.
        update_data: Dict of field names to new values. The reserved keys
            _id, owner, perms and DID are refused.

    Returns:
        The updated record, or {"error": ...}.

    Example:
        update_metadata_record(
            canvas_id="65c21d6a593f32e0103daf25",
            collection_name="samples",
            data_id="6977762ac783bd6d7ed0db01",
            update_data={"group": "chow.wt"}
        )
    """
    problem = _record_update_problem(data_id, update_data)
    if problem:
        return json.dumps({"error": problem})
    try:
        via_client = get_client()
        logger.info(f"Updating metadata record '{data_id}' in canvas '{canvas_id}', collection '{collection_name}': {update_data}")

        record = via_client.metadata.update_data(canvas_id, collection_name, data_id, update_data)
        return json.dumps(serialize_response(record), indent=2)
    except Exception as e:
        logger.error(f"Error updating metadata record: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def update_metadata_records(canvas_id: str, collection_name: str, updates: list[dict]) -> str:
    """
    Update fields on many metadata records in one collection.
    Each item is {"data_id": "<record _id>", "update_data": {...}}. Only the
    keys in each update_data change on that record.

    WARNING: This modifies persistent records on the Foundry Connect server
    and cannot be undone automatically. Before calling it, show the user one
    table with every record and its new values, and get a single confirmation
    for the whole set.

    Every item is checked before anything is written. If any item has a
    missing or repeated data_id, an empty update_data, or a reserved key
    (_id, owner, perms, DID), nothing is updated and the invalid items are
    listed. Once writing starts, a failure on one record does not stop the
    others, and the result reports each record.

    Args:
        canvas_id: The canvas ID where the collection exists.
        collection_name: Name of the collection that holds the records.
        updates: List of {"data_id": str, "update_data": dict}.

    Returns:
        {"updated": n, "failed": n, "results": [{"data_id", "ok", "record" or "error"}]},
        or {"error": ..., "invalid_rows": [{"index", "data_id", "error"}]} when
        nothing was written.

    Example:
        update_metadata_records(
            canvas_id="65c21d6a593f32e0103daf25",
            collection_name="samples",
            updates=[
                {"data_id": "6977762ac783bd6d7ed0db01", "update_data": {"group": "chow.wt"}},
                {"data_id": "6977762ac783bd6d7ed0db02", "update_data": {"group": "hfd.wt"}}
            ]
        )
    """
    if not isinstance(updates, list) or not updates:
        return json.dumps({"error": "updates must be a non-empty list"})

    invalid_rows = []
    seen = set()
    for index, item in enumerate(updates):
        if not isinstance(item, dict):
            invalid_rows.append({"index": index, "data_id": None, "error": "each update must be an object"})
            continue
        data_id = item.get("data_id")
        problem = _record_update_problem(data_id, item.get("update_data"))
        if not problem and data_id in seen:
            problem = "data_id appears more than once"
        if problem:
            invalid_rows.append({"index": index, "data_id": data_id, "error": problem})
        if isinstance(data_id, str):
            seen.add(data_id)
    if invalid_rows:
        return json.dumps({"error": "No records were updated because some updates are invalid", "invalid_rows": invalid_rows}, indent=2)

    try:
        via_client = get_client()
    except Exception as e:
        logger.error(f"Error updating metadata records: {e}")
        return json.dumps({"error": str(e)})

    logger.info(f"Updating {len(updates)} metadata records in canvas '{canvas_id}', collection '{collection_name}'")
    results = []
    for item in updates:
        data_id = item["data_id"]
        try:
            record = via_client.metadata.update_data(canvas_id, collection_name, data_id, item["update_data"])
            results.append({"data_id": data_id, "ok": True, "record": serialize_response(record)})
        except Exception as e:
            logger.error(f"Error updating metadata record '{data_id}': {e}")
            results.append({"data_id": data_id, "ok": False, "error": str(e)})

    updated = sum(1 for r in results if r["ok"])
    return json.dumps({"updated": updated, "failed": len(results) - updated, "results": results}, indent=2)


# ============================================================================
# App Launch Tools
# ============================================================================

@mcp.tool()
def list_apps(search: str = None) -> str:
    """
    List all available applications in Foundry Connect with their names, IDs, and details.
    Use this to find apps by name before launching them.
    Returns app information including ID, name, description, image, and configuration.
    """
    try:
        via_client = get_client()
        logger.info(f"Listing apps with search filter: {search}")
        
        # Call the /api/v1/app endpoint to get all apps
        response = via_client.call(
            method="GET",
            endpoint="/api/v1/app"
        )
        
        # Extract apps from paginated response
        if isinstance(response, dict) and 'data' in response:
            apps = response['data']
        else:
            apps = response if isinstance(response, list) else []
        
        # If search filter provided, filter apps by name
        if search and isinstance(apps, list):
            search_lower = search.lower()
            filtered_apps = [
                app for app in apps
                if search_lower in str(app.get('name', '')).lower()
            ]
            result = filtered_apps
        else:
            result = apps
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing apps: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def discover_app_endpoints(search: str = None, as_json: bool = False) -> str:
    """
    Discover and search for available API endpoints in Foundry Connect.
    Search by name, description, or endpoint path.
    Returns endpoint details including path, methods, and descriptions.
    Use list_apps for finding apps by name instead.
    """
    try:
        via_client = get_client()
        logger.info(f"Discovering app endpoints with search: {search}")
        
        # Use the SDK's discover method to find endpoints
        endpoints = via_client.discover(search=search, as_json=as_json)
        
        # If as_json is True, endpoints is already a JSON string
        if as_json:
            return endpoints
        else:
            return json.dumps(endpoints, indent=2)
    except Exception as e:
        logger.error(f"Error discovering app endpoints: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def launch_app(app_id: str, run_type: str = "standalone", parameters: dict = None) -> str:
    """
    Launch/run an application or pipeline in Foundry Connect.
    Executes a specific app with the provided parameters.
    Use discover_app_endpoints first to find the correct app_id and endpoint.
    """
    try:
        via_client = get_client()
        logger.info(f"Launching app {app_id} with type {run_type}")
        
        # Build the endpoint
        endpoint = f"/api/v1/app/call/{app_id}"
        
        # Prepare the request data
        data = {
            "type": run_type,
        }
        if parameters:
            data.update(parameters)
        
        # Make the API call using the generic call method
        result = via_client.call(
            method="POST",
            endpoint=endpoint,
            data=data
        )
        
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error launching app: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Process git integration
#
# These call the REST API directly through `via_client.call` rather than through
# an SDK method, because the SDK pin (see the Dockerfile's SDK_GIT_REF) predates
# these endpoints. Adding SDK methods would couple this change to an SDK release
# and a deploy ordering problem; calling the endpoint is enough.
# ============================================================================


@mcp.tool()
def connect_process_repository(
    process_id: str,
    repo_url: str,
    provider: str = "github",
    default_branch: str = "main",
    service_token_id: int = None,
) -> str:
    """
    Connect a process to the git repository it publishes to.

    Registers where a process's revisions get exported. `repo_url` must be an https
    URL; ssh remotes are rejected because they would authenticate from the server's
    own key material rather than the stored credential.

    WARNING: This modifies a persistent resource. The repository URL cannot be
    changed once set, because published revision tags live in the repository that
    is already registered. The branch and the stored credential can be updated.

    Requires write permission on the process.
    """
    try:
        via_client = get_client()
        logger.info(f"Connecting process {process_id} to a repository")

        payload = {
            "repoUrl": repo_url,
            "provider": provider,
            "defaultBranch": default_branch,
        }
        if service_token_id is not None:
            payload["serviceTokenId"] = service_token_id

        result = via_client.call(
            method="POST",
            endpoint=f"/api/v1/process/{process_id}/git/connect",
            data=payload,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error connecting process {process_id} to a repository: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def export_process_revision(process_id: str) -> str:
    """
    Export a process revision to its connected git repository.

    Materializes the revision (process.yaml, README.md, JSON Schemas, and any
    attached script assets under bin/), commits it, tags it `foundry/rev-<n>`, and
    pushes without force.

    Returns `committed: false` when the revision is unchanged since its last
    export, in which case no commit is made. Exporting a revision older than the
    highest already in the repository is refused.

    Connect the process to a repository first with connect_process_repository.
    Requires write permission on the process.
    """
    try:
        via_client = get_client()
        logger.info(f"Exporting process revision {process_id}")

        result = via_client.call(
            method="POST",
            endpoint=f"/api/v1/process/{process_id}/git/export",
            data={},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error exporting process revision {process_id}: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_process_script_assets(process_id: str) -> str:
    """
    List the R, Python or shell files attached to a process revision.

    Returns each asset's path, contents, whether it is executable, and a digest of
    its contents.
    """
    try:
        via_client = get_client()
        logger.info(f"Listing script assets for process {process_id}")

        result = via_client.call(
            method="GET",
            endpoint=f"/api/v1/process/{process_id}/script-assets",
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing script assets for process {process_id}: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def put_process_script_asset(
    process_id: str,
    path: str,
    contents: str,
    executable: bool = True,
) -> str:
    """
    Attach a script to a process revision, or replace the one already at that path.

    A `path` with no directory lands in `bin/`, which is the only directory
    Nextflow puts on PATH, so the script is callable by name from the process
    body. A path naming a directory is kept as written.

    WARNING: This changes the process itself, not just its metadata. The script is
    part of the process's content hash, so attaching or editing one returns the
    revision to draft, regenerates its contract, and marks any validation or test
    evidence gathered against the old content as stale. It is refused outright on a
    published revision; create a new revision instead.
    """
    try:
        via_client = get_client()
        logger.info(f"Writing script asset {path} on process {process_id}")

        result = via_client.call(
            method="PUT",
            endpoint=f"/api/v1/process/{process_id}/script-assets",
            data={"path": path, "contents": contents, "executable": executable},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error writing script asset on process {process_id}: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def delete_process_script_asset(process_id: str, path: str) -> str:
    """
    Remove a script asset from a process revision.

    WARNING: Like attaching one, this changes the process's content: it returns the
    revision to draft, regenerates its contract, and marks evidence gathered
    against the old content as stale. Refused on a published revision.
    """
    try:
        via_client = get_client()
        logger.info(f"Deleting script asset {path} from process {process_id}")

        result = via_client.call(
            method="DELETE",
            endpoint=f"/api/v1/process/{process_id}/script-assets",
            data={"path": path},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting script asset from process {process_id}: {e}")
        return json.dumps({"error": str(e)})


# ============================================================================
# Git import and pipeline git integration
#
# Like the process git tools above, these call the REST API directly. Failures
# are raised as ToolError rather than returned as text, so a refused import or
# export never reads as a successful one.
# ============================================================================


def _server_refusal(error: Exception) -> Optional[dict]:
    """
    The status and message of the HTTP refusal behind an SDK error, if any.

    The SDK raises its own error from inside the HTTPError handler and names
    only the status for most codes, so the server's reason (such as which
    process already holds a revision) survives only on the chained HTTPError.
    """
    seen = error
    while seen is not None:
        response = getattr(seen, "response", None) if isinstance(seen, requests.HTTPError) else None
        if response is not None:
            try:
                body = response.json()
            except ValueError:
                return None
            if isinstance(body, dict):
                for key in ("message", "error"):
                    value = body.get(key)
                    if isinstance(value, str) and value.strip():
                        return {"error": value.strip(), "status": response.status_code}
            return None
        seen = seen.__cause__ or seen.__context__
    return None


def _git_call(method: str, endpoint: str, action: str, data: dict = None) -> str:
    """Call a git endpoint and return its JSON, raising ToolError on failure."""
    try:
        via_client = get_client()
        logger.info(f"{action}: {method} {endpoint}")
        kwargs = {"method": method, "endpoint": endpoint}
        if data is not None:
            kwargs["data"] = data
        return json.dumps(via_client.call(**kwargs), indent=2)
    except Exception as e:
        logger.error(f"Error {action}: {e}")
        raise ToolError(json.dumps(_server_refusal(e) or {"error": str(e)})) from e


@mcp.tool()
def check_process_import(
    repo_url: str,
    provider: str = "github",
    repo_path: str = None,
    revision: int = None,
    service_token_id: int = None,
) -> str:
    """
    Read a git repository and report what importing a process from it would do.
    Writes nothing.

    Without `repo_path`, lists every process the repository publishes and its
    revisions. With `repo_path`, resolves one revision (the highest published when
    `revision` is omitted) and returns its tag, `commitId`, a contract summary
    (name, inputs, outputs, script assets), which local process family it would
    join, and `consentText`.

    `repo_url` must be https. Pass `service_token_id` (a stored login) for a
    private repository.

    Show the contract and `consentText` to the user before calling
    import_process_from_repository, and pass it this check's `revision.revId` and
    `revision.commitId`.
    """
    payload = {"repoUrl": repo_url, "provider": provider}
    if repo_path is not None:
        payload["repoPath"] = repo_path
    if revision is not None:
        payload["revision"] = revision
    if service_token_id is not None:
        payload["serviceTokenId"] = service_token_id
    return _git_call("POST", "/api/v1/process/git/import/check", "checking a process import", payload)


@mcp.tool()
def import_process_from_repository(
    repo_url: str,
    repo_path: str,
    revision: int,
    expected_commit_id: str,
    menu_group_id: int,
    confirm_import: bool,
    provider: str = "github",
    service_token_id: int = None,
) -> str:
    """
    Import a process revision from a git repository as a new process.

    WARNING: This creates a process from code that has not been reviewed on this
    installation. Run check_process_import first, show the user its contract and
    `consentText`, and set `confirm_import` to true only after the user agrees.

    `revision` and `expected_commit_id` are the check's `revision.revId` and
    `revision.commitId`. The import is refused if the repository no longer has
    that commit for the revision, so the user gets exactly what they reviewed.
    `menu_group_id` is the process menu group for the new process (see
    list_menu_groups).

    Returns the new process id, its family id (`processGid`) and its name.
    """
    if confirm_import is not True:
        raise ToolError(json.dumps({
            "error": "confirm_import must be true. Show the user the check_process_import "
                     "result and its consentText, and import only after they agree."
        }))
    payload = {
        "repoUrl": repo_url,
        "provider": provider,
        "repoPath": repo_path,
        "revision": revision,
        "expectedCommitId": expected_commit_id,
        "menuGroupId": menu_group_id,
        "confirmImport": True,
    }
    if service_token_id is not None:
        payload["serviceTokenId"] = service_token_id
    return _git_call("POST", "/api/v1/process/git/import", "importing a process", payload)


@mcp.tool()
def connect_pipeline_repository(
    pipeline_id: int,
    repo_url: str,
    repo_path: str,
    provider: str = "github",
    default_branch: str = "main",
    service_token_id: int = None,
) -> str:
    """
    Connect a pipeline to the git repository it publishes to, or update that
    connection.

    `repo_path` is the pipeline's folder in the repository. The processes the
    pipeline uses are exported beside it. `repo_url` must be https. Pass
    `service_token_id` (a stored login with write access) to be able to export.

    WARNING: This modifies a persistent resource. Calling it again replaces the
    connection, including the stored login: omit `service_token_id` and the
    connection is left without one.

    Requires write permission on the pipeline.
    """
    payload = {
        "repoUrl": repo_url,
        "repoPath": repo_path,
        "provider": provider,
        "defaultBranch": default_branch,
        "serviceTokenId": service_token_id,
    }
    return _git_call(
        "POST", f"/api/v1/pipeline/{pipeline_id}/git/connect", f"connecting pipeline {pipeline_id}", payload
    )


@mcp.tool()
def get_pipeline_repository(pipeline_id: int) -> str:
    """
    Read the git repository a pipeline publishes to: URL, folder, branch,
    provider, stored login id and the last exported tag. Returns null when the
    pipeline is not connected.

    Requires write permission on the pipeline.
    """
    return _git_call(
        "GET", f"/api/v1/pipeline/{pipeline_id}/git/connection", f"reading pipeline {pipeline_id}'s repository"
    )


@mcp.tool()
def export_pipeline_revision(pipeline_id: int) -> str:
    """
    Export a pipeline, with every process and module it uses, to its connected
    git repository.

    Each process and module is written to its own folder with its own tag, then
    the pipeline itself is tagged. Pushes without force. Returns the pipeline's
    tag, whether a commit was made (`committed: false` when nothing changed since
    the last export), and the tag of each process and module.

    WARNING: This pushes to a remote repository. Connect the pipeline first with
    connect_pipeline_repository, using a login with write access.

    Requires write permission on the pipeline.
    """
    return _git_call(
        "POST", f"/api/v1/pipeline/{pipeline_id}/git/export", f"exporting pipeline {pipeline_id}", {}
    )


@mcp.tool()
def check_pipeline_import(
    repo_url: str,
    slug: str,
    rev: int = None,
    service_token_id: int = None,
) -> str:
    """
    Read a pipeline from a git repository and report what importing it would do.
    Writes nothing.

    `slug` is the pipeline's folder in the repository. `rev` is the revision to
    check, or omit it for the highest published. Returns the resolved `rev` and
    `tag`, one plan item per process, module and the pipeline itself with its
    `status` (`new`, `update-available`, `already-imported`, or a blocking status
    such as `uuid-conflict`) and `statusReason`, and `canApply`, which is false
    when any item blocks the import.

    `repo_url` must be https. Pass `service_token_id` (a stored login) for a
    private repository.

    Show the plan to the user before calling import_pipeline_from_repository, and
    pass it this check's `rev`.
    """
    payload = {"repoUrl": repo_url, "slug": slug, "rev": rev, "serviceTokenId": service_token_id}
    return _git_call("POST", "/api/v1/pipeline/git/import/check", "checking a pipeline import", payload)


@mcp.tool()
def import_pipeline_from_repository(
    repo_url: str,
    slug: str,
    rev: int,
    menu_group_id: int,
    pipeline_group_id: int,
    service_token_id: int = None,
) -> str:
    """
    Import a pipeline, with the processes and modules it uses, from a git
    repository.

    WARNING: This creates processes and a pipeline from code that has not been
    reviewed on this installation. Run check_pipeline_import first, show the user
    the plan, and import only if `canApply` is true and the user agrees.

    `rev` is the check's `rev`, so the import is the revision the user reviewed.
    `menu_group_id` is the process menu group for new processes (see
    list_menu_groups). `pipeline_group_id` is the pipeline menu group for the
    pipeline (see list_pipeline_menu_groups).

    Returns `pipelineId` (null when the pipeline already exists here and you may
    not read it), `created`, and the ids of the processes used.
    """
    payload = {
        "repoUrl": repo_url,
        "slug": slug,
        "rev": rev,
        "serviceTokenId": service_token_id,
        "menuGroupId": menu_group_id,
        "pipelineGroupId": pipeline_group_id,
    }
    return _git_call("POST", "/api/v1/pipeline/git/import", "importing a pipeline", payload)


@mcp.tool()
def list_pipeline_menu_groups() -> str:
    """
    List the pipeline menu groups, the groups pipelines are listed under.
    Use one as `pipeline_group_id` in import_pipeline_from_repository.
    Process menu groups are a separate list (see list_menu_groups).
    """
    return _git_call("GET", "/api/v1/menu-group/pipeline", "listing pipeline menu groups")


# ============================================================================
# Pipeline Building Tools
#
# Build a pipeline graph from existing processes with the SDK's PipelineBuilder,
# which type checks every connection, and save it through the editor's own
# endpoint (POST /api/pipeline/v1). create_pipeline_config and create_pipeline
# take the same arguments: the config call is the preview, and the create call
# rebuilds the graph from its arguments, so what is saved is what was checked.
#
# Process ids and pipeline ids are separate id spaces, and pipeline menu groups
# are separate from process menu groups. Refusals are raised as ToolError.
# ============================================================================

_NODE_KEY = re.compile(r"\bg-\d+\b")


def _pipelines(via_client):
    """The SDK's pipelines API, refusing when the installed SDK predates it."""
    pipelines = getattr(via_client, "pipelines", None)
    if pipelines is None:
        raise ToolError(json.dumps({
            "error": "This Foundry MCP server is running an SDK without pipeline building.",
            "hint": "Upgrade viafoundry_sdk to 1.3.0 or later.",
        }))
    return pipelines


def _process_reader(via_client):
    """get_process for PipelineBuilder: GET /api/v1/process/{id}, refusing a non-process."""
    def get_process(process_id):
        try:
            process = via_client.call(method="GET", endpoint=f"/api/v1/process/{process_id}")
        except Exception as e:
            raise ToolError(json.dumps({
                "error": f"Process {process_id} could not be read: {e}",
                "hint": "Process ids and pipeline ids are separate id spaces.",
            })) from e
        if not isinstance(process, dict) or not isinstance(process.get("inputs"), list) \
                or not isinstance(process.get("outputs"), list):
            raise ToolError(json.dumps({
                "error": f"Process {process_id} could not be read as a process.",
                "hint": "Process ids and pipeline ids are separate id spaces; "
                        "use list_processes or get_process_details for process ids.",
            }))
        return process
    return get_process


def _describe_connections(builder, labels):
    """Each edge as {from, output, to, input}, plus process inputs nothing feeds."""
    from viafoundry.pipelines import INPUT_NODE, OUTPUT_NODE, parse_edge

    def port(handle, side):
        if handle["param"] in (INPUT_NODE, OUTPUT_NODE):
            return None
        return builder.process(handle["param"])[side][handle["slot"]].get("name")

    connections, fed = [], set()
    for edge in builder.edges:
        source, target = parse_edge(edge)
        fed.add((target["node"], target["slot"]))
        connections.append({
            "from": labels.get(source["node"], source["node"]),
            "output": port(source, "outputs"),
            "output_slot": source["slot"],
            "to": labels.get(target["node"], target["node"]),
            "input": port(target, "inputs"),
            "input_slot": target["slot"],
        })
    unconnected = [
        {"process": labels.get(key, key), "input": inp.get("name"), "slot": slot}
        for key, node in builder.nodes.items() if node["param"] not in (INPUT_NODE, OUTPUT_NODE)
        for slot, inp in enumerate(builder.process(node["param"])["inputs"])
        if (key, slot) not in fed
    ]
    return connections, unconnected


def _build_pipeline(via_client, processes, connections, inputs, outputs):
    """Assemble and type check a graph. Returns (builder, {g-N: caller's key})."""
    from viafoundry.pipelines import PipelineBuilder, PipelineGraphError

    _pipelines(via_client)
    builder = PipelineBuilder(_process_reader(via_client))
    keys, labels = {}, {}

    def refuse(detail):
        raise ToolError(json.dumps({
            "error": "The pipeline graph was refused.",
            "detail": detail,
            "hint": "Nothing was saved. Use get_process_details for a process's inputs and outputs.",
        }))

    def add(kind, spec, make):
        if not isinstance(spec, dict) or not isinstance(spec.get("key"), str) or not spec["key"]:
            refuse(f"Every {kind} needs a \"key\" string, the name connections refer to it by.")
        if spec["key"] in keys:
            refuse(f"The key {spec['key']!r} is used twice.")
        node = make(spec)
        keys[spec["key"]], labels[node] = node, spec["key"]

    def node(key):
        if key not in keys:
            refuse(f"A connection names {key!r}, which is not the key of any process, input or output.")
        return keys[key]

    if not processes:
        refuse("A pipeline needs at least one process.")
    try:
        for spec in processes:
            process_id = spec.get("process_id") if isinstance(spec, dict) else None
            if isinstance(process_id, str) and process_id.isdigit():
                process_id = int(process_id)
            if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
                refuse(f"Every process needs a \"process_id\", a positive whole number; got {process_id!r}.")
            add("process", spec, lambda s: builder.add_process(process_id, s.get("x"), s.get("y"), s.get("name")))
        for spec in inputs or []:
            add("input", spec, lambda s: builder.add_input(s.get("name") or s["key"], s.get("x"), s.get("y")))
        for spec in outputs or []:
            add("output", spec, lambda s: builder.add_output(s.get("name") or s["key"], s.get("publish"), s.get("x"), s.get("y")))
        for spec in connections or []:
            if not isinstance(spec, dict):
                refuse("Every connection is a dict with \"from\" and \"to\".")
            builder.connect(node(spec.get("from")), node(spec.get("to")), output=spec.get("output"), input=spec.get("input"))
    except PipelineGraphError as e:
        refuse(_NODE_KEY.sub(lambda m: repr(labels.get(m.group(0), m.group(0))), str(e)))
    return builder, labels


def _with_doc(text):
    """Set a tool's docstring from text built at import time (FastMCP reads __doc__)."""
    def apply(fn):
        fn.__doc__ = text
        return fn
    return apply


_PIPELINE_GRAPH_HELP = """
    processes: list of {"key", "process_id", "name"?, "x"?, "y"?}. `key` is any
      label you choose; connections refer to nodes by it.
    inputs: list of {"key", "name"?}, the pipeline inputs. `name` is the field
      label on the run page (defaults to the key).
    outputs: list of {"key", "name"?, "publish"?}, the pipeline outputs.
      `publish` (html, rdata, ...) shows the output as a report of that type.
    connections: list of {"from", "to", "output"?, "input"?}. Edges run from a
      process or pipeline input to a process or pipeline output. `output` and
      `input` pick the process's port by 0-based slot, sname, name or parameter
      id; leave one out when the process has only one. Ends must match on file
      type, and a value (qualifier val) connects only to a value. One pipeline
      input may feed several process inputs of the same type.
    pipeline_group_id: a pipeline menu group from list_pipeline_menu_groups,
      not a process menu group.
"""


@mcp.tool()
def get_pipeline(pipeline_id: int) -> str:
    """
    Read one pipeline: its fields, its graph (nodes and edges as the editor saves
    them) and `connections`, each edge spelled out as process and port names
    with 0-based port slots (slot 1 on a pipeline input or output).
    `unconnected_inputs` lists process inputs nothing feeds. Writes nothing.

    A node's `param` is a process id, "inPro" (pipeline input) or "outPro"
    (pipeline output). Process ids and pipeline ids are separate id spaces.
    """
    from viafoundry.pipelines import PipelineBuilder

    try:
        via_client = get_client()
        pipeline = _pipelines(via_client).get_pipeline(pipeline_id)
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error reading pipeline {pipeline_id}: {e}")
        raise ToolError(json.dumps({"error": str(e)})) from e

    if not pipeline.get("id"):
        raise ToolError(json.dumps({
            "error": f"No pipeline {pipeline_id} could be read.",
            "hint": "Process ids and pipeline ids are separate id spaces; use list_pipelines for pipeline ids.",
        }))
    builder = PipelineBuilder(_process_reader(via_client), pipeline["nodes"], pipeline["edges"])
    labels = {key: f"{key} {node.get('nodeName')}" for key, node in pipeline["nodes"].items()}
    try:
        pipeline["connections"], pipeline["unconnected_inputs"] = _describe_connections(builder, labels)
    except Exception as e:
        logger.warning(f"Could not name the connections of pipeline {pipeline_id}: {e}")
        pipeline["connections_error"] = "Could not read every process in this pipeline to name its ports."
    return json.dumps(serialize_response(pipeline), indent=2)


_CREATE_PIPELINE_CONFIG_DOC = """
    Preview a new pipeline built from existing processes. Writes nothing.

    Reads each process, places the nodes, type checks every connection and
    returns the nodes and edges the editor would save, every connection spelled
    out, and `unconnected_inputs` (process inputs nothing feeds, which a run
    cannot fill unless they are optional). A refused connection raises an error
    naming the node by your key.
""" + _PIPELINE_GRAPH_HELP + """
    Example (pipeline 1777): processes=[{"key": "check", "process_id": 3017}],
      inputs=[{"key": "label", "name": "inputParam"}],
      outputs=[{"key": "report", "name": "outputParam"}],
      connections=[{"from": "label", "to": "check"}, {"from": "check", "to": "report"}]

    Show the user `connections` and `unconnected_inputs`, then call
    create_pipeline with the same arguments and confirm_create=true.
"""


@mcp.tool()
@_with_doc(_CREATE_PIPELINE_CONFIG_DOC)
def create_pipeline_config(
    name: str,
    pipeline_group_id: int,
    processes: list,
    connections: list,
    inputs: list = None,
    outputs: list = None,
    summary: str = "",
    description: str = "",
) -> str:
    builder, labels = _build_pipeline(get_client(), processes, connections, inputs, outputs)
    described, unconnected = _describe_connections(builder, labels)
    return json.dumps({
        "name": name,
        "pipeline_group_id": pipeline_group_id,
        "summary": summary,
        "description": description,
        "nodes": builder.nodes,
        "edges": builder.edges,
        "connections": described,
        "unconnected_inputs": unconnected,
        "next_steps": [
            "Show the user the connections and unconnected inputs.",
            "create_pipeline(...) with the same arguments and confirm_create=true after the user agrees.",
        ],
    }, indent=2)


_CREATE_PIPELINE_DOC = """
    Create a new private pipeline from existing processes.

    WARNING: This creates a pipeline on the Foundry Connect server. Call
    create_pipeline_config with the same arguments first, show the user its
    connections, and set `confirm_create` to true only after they agree. The
    graph is rebuilt and type checked from these arguments, never taken from a
    preview. It always creates a new pipeline and never saves over one.
""" + _PIPELINE_GRAPH_HELP + """
    Returns the new pipeline's id. Open it in the pipeline editor to publish it,
    or start a run with create_run(pipeline_id, project_id, name).
"""


@mcp.tool()
@_with_doc(_CREATE_PIPELINE_DOC)
def create_pipeline(
    name: str,
    pipeline_group_id: int,
    processes: list,
    connections: list,
    confirm_create: Annotated[bool, Strict()],
    inputs: list = None,
    outputs: list = None,
    summary: str = "",
    description: str = "",
) -> str:
    if confirm_create is not True:
        raise ToolError(json.dumps({
            "error": "confirm_create must be true. Show the user the create_pipeline_config "
                     "result, and create only after they agree."
        }))
    via_client = get_client()
    builder, _ = _build_pipeline(via_client, processes, connections, inputs, outputs)
    try:
        logger.info(f"Creating pipeline {name!r} with {len(builder.nodes)} nodes")
        created = _pipelines(via_client).create_pipeline(
            name=name,
            pipeline_group_id=pipeline_group_id,
            nodes=builder.nodes,
            edges=builder.edges,
            summary=summary,
            description=description,
        )
    except Exception as e:
        logger.error(f"Error creating pipeline: {e}")
        raise ToolError(json.dumps({"error": str(e)})) from e
    result = serialize_response(created)
    pipeline_id = result.get("id") if isinstance(result, dict) else None
    return json.dumps({
        "pipeline_id": pipeline_id,
        "response": result,
        "next_steps": [
            f"get_pipeline({pipeline_id}) to check the saved graph.",
            "create_run(pipeline_id, project_id, name) to start a run from it.",
        ],
    }, indent=2)


@mcp.tool()
def list_pipeline_revisions(pipeline_id: int) -> str:
    """
    List every revision of a pipeline, given the id of any one of them. Writes
    nothing.

    Each revision is its own pipeline with its own id (Xenium pipeline 1545 is
    version 1.0.3 and 1776 is version 1.0.4), so pass the id of the revision
    you mean to get_pipeline, create_run or update_pipeline. `is_this_id` marks
    the revision whose id you gave.
    """
    def refuse(detail):
        raise ToolError(json.dumps({
            "error": f"No revisions of pipeline {pipeline_id} could be read.",
            "detail": detail,
            "hint": "Process ids and pipeline ids are separate id spaces; use list_pipelines for pipeline ids.",
        }))

    try:
        response = get_client().call(method="GET", endpoint=f"/api/pipeline/v1/{pipeline_id}/revisions")
    except Exception as e:
        logger.error(f"Error listing revisions of pipeline {pipeline_id}: {e}")
        refuse(str(e))
    rows = response.get("data") if isinstance(response, dict) else None
    if not rows:
        refuse("The server returned no revisions.")
    revisions = [{
        "pipeline_id": row.get("id"),
        "version": row.get("version"),
        "rev_id": row.get("revId"),
        "rev_comment": row.get("revComment"),
        "created_at": row.get("createdAt"),
        "is_this_id": row.get("id") == pipeline_id,
    } for row in rows]
    return json.dumps({
        "pipeline_id": pipeline_id,
        "total": response.get("total", len(rows)),
        "revisions": revisions,
    }, indent=2)


_UPDATE_PIPELINE_DOC = """
    Save a new graph over an existing private pipeline.

    WARNING: This overwrites the pipeline on the Foundry Connect server in
    place: same id, same revision, and runs already made from it see the new
    graph. Call get_pipeline(pipeline_id) and create_pipeline_config with the
    new graph first, show the user the old and new connections, and set
    `confirm_update` to true only after they agree.

    Refuses a pipeline that is public or shared with a group, pinned, or that
    the signed in account cannot edit. To change one of those, create_pipeline
    makes a new private pipeline instead.

    The graph replaces the old one entirely: pass every process, input, output
    and connection the pipeline should have. `name`, `summary` and
    `description` change only when given; every other field is kept.
""" + _PIPELINE_GRAPH_HELP + """
    Returns the saved edges, the connections spelled out, and
    `previous_edges`, the graph before the save.
"""


def _refuse_shared_pipeline(pipeline, pipeline_id):
    """Refuse a save over a pipeline other people may run or that this account cannot edit."""
    reasons = []
    if pipeline.get("updateAllowed") is False:
        reasons.append("the signed in account cannot edit it")
    if str(pipeline.get("perms")) != "3":
        reasons.append(f"it is not private (permission {pipeline.get('perms')})")
    if str(pipeline.get("pin")).lower() == "true":
        reasons.append("it is pinned")
    if reasons:
        raise ToolError(json.dumps({
            "error": f"Pipeline {pipeline_id} was not saved: {', and '.join(reasons)}.",
            "hint": "A save edits in place under every run that uses it. Use create_pipeline "
                    "for a new private pipeline, or make this one private in the pipeline editor first.",
        }))


@mcp.tool()
@_with_doc(_UPDATE_PIPELINE_DOC)
def update_pipeline(
    pipeline_id: int,
    processes: list,
    connections: list,
    confirm_update: Annotated[bool, Strict()],
    inputs: list = None,
    outputs: list = None,
    name: str = None,
    summary: str = None,
    description: str = None,
) -> str:
    if confirm_update is not True:
        raise ToolError(json.dumps({
            "error": "confirm_update must be true. Show the user the current and new "
                     "connections, and save only after they agree."
        }))
    via_client = get_client()
    pipelines = _pipelines(via_client)
    try:
        current = pipelines.get_pipeline(pipeline_id)
    except Exception as e:
        logger.error(f"Error reading pipeline {pipeline_id} before a save: {e}")
        raise ToolError(json.dumps({"error": str(e)})) from e
    if not current.get("id"):
        raise ToolError(json.dumps({
            "error": f"No pipeline {pipeline_id} could be read.",
            "hint": "Process ids and pipeline ids are separate id spaces; use list_pipelines for pipeline ids.",
        }))
    _refuse_shared_pipeline(current, pipeline_id)

    builder, labels = _build_pipeline(via_client, processes, connections, inputs, outputs)
    changes = {"nodes": builder.nodes, "edges": builder.edges}
    changes.update(remove_none({"name": name, "summary": summary, "description": description}))
    try:
        logger.info(f"Saving a graph of {len(builder.nodes)} nodes over pipeline {pipeline_id}")
        pipelines.update_pipeline(pipeline_id, **changes)
    except Exception as e:
        logger.error(f"Error saving pipeline {pipeline_id}: {e}")
        raise ToolError(json.dumps({"error": str(e)})) from e
    described, unconnected = _describe_connections(builder, labels)
    return json.dumps({
        "pipeline_id": pipeline_id,
        "edges": builder.edges,
        "connections": described,
        "unconnected_inputs": unconnected,
        "previous_edges": current["edges"],
        "next_steps": [f"get_pipeline({pipeline_id}) to check the saved graph."],
    }, indent=2)


# ============================================================================
# Workspace Tools
#
# Run code in a container on a run environment's workspace. These call the REST
# API through `request_json` rather than the SDK's `call`, because a submit
# needs an `Idempotency-Key` header and the typed refusal in the error body.
#
# Approval is a person's step in a signed in browser session. The server refuses
# an approve or reject made with a token, and no tool here offers one: a tool
# hands back the approval link and the client shares it.
#
# Refusals are raised as ToolError carrying `reason`, so a refused job never
# reads as a submitted one.
# ============================================================================

_WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_SUBMISSION_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_COMMAND_BYTES = 32 * 1024

# What a person should do about a submit outcome that did not start a job. The
# server's own message wins when it sends one.
_SUBMIT_REFUSALS = {
    "conflict": (
        "submission_key_conflict",
        "This submission_key was already used for a different job. Call again with the same "
        "command and resources to read that job, or use a new submission_key for a new job.",
    ),
    "configuration_changed": (
        "configuration_changed",
        "The run environment's settings changed while the job was being submitted. Call again "
        "with the same submission_key.",
    ),
    "dispatch_refused": (
        "dispatch_refused",
        "The run environment refused to start the job. Check the run environment, then use a new "
        "submission_key to try again.",
    ),
}

# Every outcome kind that says the job was not submitted.
_SUBMIT_REFUSAL_KINDS = frozenset(_SUBMIT_REFUSALS) | {"refused", "request_terminal"}

_ANSWER_LOST_MESSAGE = (
    "The job may have started but its answer was lost. Call workspace_job_status, or call "
    "again with the same submission_key; do not use a new key."
)

_NOT_FOUND_MESSAGE = (
    "Not found. Either the workspace or job does not exist for you, or workspace jobs are not "
    "turned on in this Foundry Connect installation."
)


_AUTH_MESSAGE = (
    "Foundry Connect did not accept this request's credentials. Check the token this client is "
    "configured with: it must be set, current (not expired or revoked) and belong to a user who "
    "may use this workspace."
)

_STATUS_FALLBACKS = {
    401: ("unauthorized", _AUTH_MESSAGE),
    403: ("forbidden", _AUTH_MESSAGE),
    404: ("not_found_or_disabled", _NOT_FOUND_MESSAGE),
}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# The typed fields a newer server adds to a refusal, each with the type it must
# have to be passed on. A field that is absent or of another type is left out,
# so an older server's refusal reads exactly as before.
_TYPED_REFUSAL_FIELDS = {
    "category": lambda v: isinstance(v, str) and bool(v),
    "retryable": lambda v: isinstance(v, bool),
    "limit": _is_number,
    "current": _is_number,
    "mount": lambda v: isinstance(v, int) and not isinstance(v, bool),
}


def _refusal_next_step(reason: str, category, retryable):
    """What the client should do about a quota or ceiling refusal, or None."""
    if category == "quota":
        if reason == "storage":
            return (
                "Your workspaces on this run environment use more disk than your storage limit "
                "allows (limit and current are in MB). Remove files or workspaces you no longer "
                "need, or ask the run environment's owner to raise the limit, then call again with "
                "a new submission_key."
            )
        if retryable is True:
            return "Wait, then call again with a new submission_key once one of your limits has room."
        return (
            "Remove files or workspaces you no longer need, or ask the run environment's owner to "
            "raise the limit, then call again with a new submission_key."
        )
    if category == "ceiling":
        return "Ask for less (fewer CPUs, less memory or fewer minutes), then call again with a new submission_key."
    return None


def _workspace_error(body, http_status: int, fallback_reason: str, fallback_message: str, **extra):
    """
    Raise a ToolError from a refusal body, keeping the server's reason and
    message, and its category, retry hint, cap figures and mount index when it
    sends them.
    """
    body = body if isinstance(body, dict) else {}
    fallback_reason, fallback_message = _STATUS_FALLBACKS.get(http_status, (fallback_reason, fallback_message))
    reason = body.get("reason") or fallback_reason
    message = body.get("message") or fallback_message
    error = {"error": message, "reason": reason, "http_status": http_status}
    if body.get("remedy"):
        error["remedy"] = body["remedy"]
    elif http_status in (401, 403) and message != _AUTH_MESSAGE:
        error["remedy"] = _AUTH_MESSAGE
    for field, valid in _TYPED_REFUSAL_FIELDS.items():
        if field in body and valid(body[field]):
            error[field] = body[field]
    next_step = _refusal_next_step(reason, error.get("category"), error.get("retryable"))
    if next_step:
        error["next_step"] = next_step
    error.update({k: v for k, v in extra.items() if v is not None})
    raise ToolError(json.dumps(error))


def _refuse(reason: str, message: str):
    raise ToolError(json.dumps({"error": message, "reason": reason}))


def _workspace_request(method: str, endpoint: str, if_it_may_have_arrived: str = "", **kwargs):
    """
    `request_json` for the workspace tools, with a request that got no HTTP
    answer raised as a ToolError. `if_it_may_have_arrived` is the sentence added
    when the request may have reached the server, saying how to check before
    trying again.
    """
    try:
        return request_json(get_client(), method, endpoint, **kwargs)
    except RequestFailed as e:
        message = e.message
        if e.may_have_arrived:
            message = f"{message} {if_it_may_have_arrived or 'Try again.'}"
        raise ToolError(json.dumps({"error": message, "reason": e.reason})) from e


def _check_workspace_id(workspace_id: str) -> None:
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_PATTERN.match(workspace_id):
        _refuse("invalid_workspace_id", "workspace_id must be a workspace id as workspace_list or workspace_create returned it.")


def _check_submission_key(key: str) -> None:
    if not isinstance(key, str) or not _SUBMISSION_KEY_PATTERN.match(key):
        _refuse(
            "invalid_submission_key",
            "submission_key must be 1 to 128 characters of letters, digits and . _ : -",
        )


def _positive_int(value, name: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = 0
    if number < 1:
        _refuse("invalid_argument", f"{name} must be a positive whole number.")
    return number


def _web_base_url() -> str:
    """The Foundry Connect address the client is configured with, for links a person opens."""
    hostname, _ = get_credentials()
    base = (hostname or get_fixed_hostname() or "").rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def _approve_link(workspace_id: str, request_id) -> str:
    return f"{_web_base_url()}/workspaces/{workspace_id}/requests/{request_id}"


@mcp.tool()
def workspace_list(run_environment_id: str = "") -> str:
    """
    List your workspaces, newest first, optionally only those on one run
    environment. Each gives its workspace_id, name, run environment, image and
    state (provisioning, ready, archived, destroyed).
    """
    params = None
    if str(run_environment_id or "").strip():
        params = {"profileClusterId": _positive_int(run_environment_id, "run_environment_id")}
    status, body = _workspace_request("GET", "/api/v1/agent-workspace", params=params, retry=True)
    if status != 200:
        _workspace_error(body, status, "list_failed", "Could not list your workspaces.")
    return json.dumps({"workspaces": (body or {}).get("workspaces", [])}, indent=2)


@mcp.tool()
def workspace_create(run_environment_id: str, project_id: str, name: str, image: str) -> str:
    """
    Create a workspace on a run environment that has container execution turned
    on: a folder on the cluster that jobs from workspace_run run in. `image` is
    the container image jobs use when the environment names none. Folders the
    jobs can see are chosen by the run environment's owner and the workspace
    owner in Foundry Connect, not here.

    WARNING: This creates a folder on the cluster. Confirm with the user first.
    """
    data = {
        "profileClusterId": _positive_int(run_environment_id, "run_environment_id"),
        "projectId": _positive_int(project_id, "project_id"),
        "name": str(name or "").strip(),
        "image": str(image or "").strip(),
    }
    if not data["name"] or not data["image"]:
        _refuse("invalid_argument", "name and image are required.")
    # Not retried: a create has no key, so a retry could make a second workspace.
    status, body = _workspace_request(
        "POST", "/api/v1/agent-workspace", data=data,
        if_it_may_have_arrived=(
            "The workspace may have been created before the answer was lost. Call workspace_list "
            "to check before trying again, so a second workspace is not made."
        ),
    )
    if status not in (200, 201):
        workspace_id = body.get("workspaceId") if isinstance(body, dict) else None
        _workspace_error(body, status, "create_refused", "The workspace could not be created.", workspace_id=workspace_id)
    return json.dumps({"workspace": body}, indent=2)


@mcp.tool()
def workspace_run(
    workspace_id: str,
    command: str,
    cpus: int = 1,
    memory_mb: int = 1024,
    wall_minutes: int = 30,
    submission_key: str = "",
) -> str:
    """
    Run a command in a container in one of your workspaces, as a job on the
    workspace's run environment.

    This LAUNCHES cluster compute (it consumes cluster time). Always confirm with
    the user before calling.

    To check or retry the same job, call again with the submission_key this
    returned. A new key starts a new job.

    `status` in the answer is one of:
    - "submitted": the job is on the scheduler (`scheduler_job_id`).
    - "awaiting_approval": the run environment needs a person's approval. A
      person must open the `approve_at` link in Foundry Connect and approve; no
      tool can approve. Share the link, then call workspace_job_status.
    - "review_stale": the run environment changed after the approval, so a
      person must review the job again at the `approve_at` link.
    - "uncertain": the job may have started but its outcome is not known
      (`reason` says why when the server gave one). Call workspace_job_status,
      or call again with the SAME submission_key. Never use a new key for it.
    A refusal is raised as an error with a `reason`; a lost connection is
    raised with reason "uncertain" and the submission_key to check with. A
    refusal may also carry `category` (quota, ceiling, mount_policy, access,
    environment, scheduler, request), `retryable`, a cap's `limit` and
    `current`, the `mount` index it is about, and `next_step` for a quota or
    ceiling refusal.

    `command` is at most 32 KiB. Folders the job can see are set on the
    workspace in Foundry Connect; this tool cannot add or change them.
    """
    _check_workspace_id(workspace_id)
    key = submission_key.strip() if isinstance(submission_key, str) else ""
    if key:
        _check_submission_key(key)
    else:
        key = "mcp-" + uuid.uuid4().hex
    if not isinstance(command, str) or not command.strip():
        _refuse("invalid_argument", "command is required.")
    if len(command.encode("utf-8")) > _MAX_COMMAND_BYTES:
        _refuse("command_too_large", "command is over 32 KiB. Put the script in a file in the workspace and run that.")
    data = {
        "command": command,
        "cpus": _positive_int(cpus, "cpus"),
        "memoryMb": _positive_int(memory_mb, "memory_mb"),
        "wallMinutes": _positive_int(wall_minutes, "wall_minutes"),
    }

    # One key for the whole call: every retry inside it is the same logical submit.
    try:
        status, body, earlier_attempt_lost = send_request(
            get_client(), "POST", f"/api/v1/agent-workspace/{workspace_id}/jobs",
            data=data, headers={"Idempotency-Key": key}, retry=True,
        )
    except RequestFailed as e:
        if not e.may_have_arrived:
            # Nothing was sent, so no job started.
            raise ToolError(json.dumps({
                "error": e.message, "reason": e.reason, "submission_key": key, "workspace_id": workspace_id,
            })) from e
        # The server may have received the submit before the connection failed,
        # so the job may have started. The key is the only way to find it again.
        raise ToolError(json.dumps({
            "error": f"{e.message} {_ANSWER_LOST_MESSAGE}",
            "reason": "uncertain",
            "submission_key": key,
            "workspace_id": workspace_id,
        })) from e
    outcome = body if isinstance(body, dict) else {}
    kind = outcome.get("kind")
    # Only a string kind can name an outcome. Any other kind (a list or an
    # object) is unhashable, so a set or dict lookup on it would raise and lose
    # the key; it is an outcome this client cannot read, so it stays uncertain.
    kind_readable = kind is None or isinstance(kind, str)
    result = {"submission_key": key, "workspace_id": workspace_id}

    if kind == "submitted":
        result.update(status="submitted", scheduler_job_id=outcome.get("jobId"),
                      message="The job was submitted. Call workspace_job_status with this submission_key to follow it.")
        return json.dumps(result, indent=2)
    if kind == "awaiting_approval":
        result.update(
            status="awaiting_approval",
            approve_at=_approve_link(workspace_id, outcome.get("requestId")),
            message="A person must approve this job in Foundry Connect before it runs. Share the link, then call workspace_job_status.",
        )
        return json.dumps(result, indent=2)
    if kind == "review_stale":
        result.update(
            status="review_stale",
            approve_at=_approve_link(workspace_id, outcome.get("requestId")),
            message="The run environment changed after the approval. A person must review the job again at the link, then call workspace_job_status.",
        )
        return json.dumps(result, indent=2)
    # A job is reported as not submitted only when the server said so: a 4xx
    # carrying a refusal this client knows, or a 4xx with no outcome at all (a
    # validation error) when no earlier attempt of this call was lost. Anything
    # else may hide a started job: a gateway 502 or 504 lost the answer, a 2xx
    # or 5xx is never a refusal, a 4xx with no outcome after a lost attempt (a
    # gateway 408 or 429) says nothing about that attempt, and an outcome kind
    # this client does not know (a newer or older server) cannot be read.
    refused = 400 <= status < 500 and kind_readable and (
        kind in _SUBMIT_REFUSAL_KINDS or (kind is None and not earlier_attempt_lost)
    )
    if kind == "uncertain" or not refused:
        result.update(status="uncertain", message=_ANSWER_LOST_MESSAGE)
        if outcome.get("reason"):
            result["reason"] = outcome["reason"]
        if kind != "uncertain":
            result["http_status"] = status
            if kind is not None:
                result["server_outcome"] = kind
        return json.dumps(result, indent=2)

    if kind == "request_terminal":
        state = outcome.get("state") or "rejected"
        _refuse_with_key(
            f"request_{state}",
            f"This job's approval request was {state}. Call again with a new submission_key to ask again.",
            key,
            status,
        )
    if kind in _SUBMIT_REFUSALS:
        reason, message = _SUBMIT_REFUSALS[kind]
        if kind == "dispatch_refused" and outcome.get("reason"):
            # The server sends a code here, used as it is.
            reason = outcome["reason"]
        _workspace_error(
            {**outcome, "reason": reason, "message": outcome.get("message") or message},
            status, reason, message, submission_key=key,
        )
    _workspace_error(outcome, status, "submit_refused", "The job was not submitted.", submission_key=key)


def _refuse_with_key(reason: str, message: str, key: str, http_status: int):
    raise ToolError(json.dumps({"error": message, "reason": reason, "http_status": http_status, "submission_key": key}))


@mcp.tool()
def workspace_job_status(workspace_id: str, submission_key: str) -> str:
    """
    Read one job of your workspace by the submission_key workspace_run returned.
    `status` is one of awaiting_approval, rejected, expired, starting, refused,
    uncertain, dispatch_refused, queued, running, completed, failed, unknown.
    A job waiting for approval also carries its `approve_at` link.

    `refused` means the job was approved but the server refused to start it
    (for example over the storage quota, or a mount outside the allowed data
    roots). Its `refusal` field holds the refusal code; it is null for every
    other status. A refused job is not final: fix the cause, then call
    workspace_run again with the same submission_key to retry it.
    """
    _check_workspace_id(workspace_id)
    _check_submission_key(submission_key)
    status, body = _workspace_request(
        "GET", f"/api/v1/agent-workspace/{workspace_id}/jobs/{submission_key}", retry=True,
    )
    if status != 200:
        _workspace_error(body, status, "job_read_failed", "Could not read the job.")
    result = {"job": body}
    if isinstance(body, dict) and body.get("status") == "awaiting_approval" and body.get("requestId"):
        result["approve_at"] = _approve_link(workspace_id, body["requestId"])
    return json.dumps(result, indent=2)


@mcp.tool()
def workspace_cancel(workspace_id: str, submission_key: str) -> str:
    """
    Cancel a queued or running job of your workspace through its run
    environment's scheduler. Returns once the scheduler took the cancel; call
    workspace_job_status to see how the job ended. A job still waiting for
    approval is not cancelled here: a person rejects it at its approval link.

    WARNING: This stops cluster work. Confirm with the user first.
    """
    _check_workspace_id(workspace_id)
    _check_submission_key(submission_key)
    # Not retried: a lost answer is read back with workspace_job_status.
    status, body = _workspace_request(
        "POST", f"/api/v1/agent-workspace/{workspace_id}/jobs/{submission_key}/cancel",
        if_it_may_have_arrived=(
            "The cancel may have reached the scheduler. Call workspace_job_status to check before trying again."
        ),
    )
    if status in (200, 202):
        return json.dumps({
            "status": "cancel_requested",
            "submission_key": submission_key,
            "message": "The scheduler took the cancel. Call workspace_job_status to see how the job ended.",
        }, indent=2)
    if isinstance(body, dict) and body.get("reason") == "job_not_cancellable":
        job_status = body.get("status")
        if job_status == "awaiting_approval":
            message = "This job is waiting for approval and has not started. A person can reject it at its approval link."
        else:
            message = f"This job cannot be cancelled: its status is {job_status}. Only a queued or running job with a scheduler job can be."
        _workspace_error({"reason": "job_not_cancellable", "message": message}, status, "", "", job_status=job_status)
    _workspace_error(body, status, "cancel_failed", "The job could not be cancelled.")


# ============================================================================
# Server Entry Point
# ============================================================================

def main():
    """Entry point for the HTTP MCP server."""
    parser = argparse.ArgumentParser(
        description='Foundry Connect MCP HTTP Server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Credentials are configured in ~/.cursor/mcp.json:

{
  "mcpServers": {
    "foundry": {
      "url": "http://127.0.0.1:8705/mcp",
      "headers": {
        "X-Foundry-Connect-Hostname": "https://your-viafoundry.com",
        "X-Foundry-Connect-Token": "your-personal-access-token"
      }
    }
  }
}

Security Modes:
  - Fixed Hostname: Set FRONTEND_HOSTNAME (and optionally FRONTEND_PROTOCOL,
    FRONTEND_PATH_PREFIX) to lock the server to a specific Foundry Connect instance.
    Used for production deployments.
  - Open Mode: Without FRONTEND_HOSTNAME, clients can specify any target via
    X-Foundry-Connect-Hostname header (development mode, only safe for localhost).

Examples:
  python -m foundry_mcp.server --port 8705
  FRONTEND_HOSTNAME=prod.viafoundry.com python -m foundry_mcp.server
        """
    )
    parser.add_argument('--port', type=int, default=8705, 
                        help='Port to run the server on (default: 8705)')
    parser.add_argument('--host', type=str, default='127.0.0.1', 
                        help='Host to bind to (default: 127.0.0.1)')
    args = parser.parse_args()
    
    # Determine security mode based on fixed hostname configuration
    fixed_hostname = get_fixed_hostname()
    
    # Log startup banner with security mode information
    logger.info("=" * 60)
    logger.info("Foundry Connect MCP HTTP Server")
    logger.info("=" * 60)
    logger.info(f"Endpoint: http://{args.host}:{args.port}/mcp")
    logger.info("")
    
    if fixed_hostname:
        # Fixed hostname mode (production)
        logger.info("Security Mode: FIXED HOSTNAME (production)")
        logger.info(f"  Target: {fixed_hostname}")
        logger.info("  Client X-Foundry-Connect-Hostname headers will be ignored")
        logger.info("")
        logger.info("Configure in mcp.json headers:")
        logger.info("  X-Foundry-Connect-Token: your-personal-access-token")
    else:
        # Open mode (development)
        logger.info("Security Mode: OPEN (development)")
        logger.info("  Clients can specify any Foundry Connect instance via header")
        if args.host != "127.0.0.1" and args.host != "localhost":
            logger.warning(
                "WARNING: Server bound to non-localhost without fixed hostname!"
            )
            logger.warning(
                "  This allows the server to be used as an open proxy."
            )
            logger.warning(
                "  Set FRONTEND_HOSTNAME for production deployments."
            )
        logger.info("")
        logger.info("Configure in mcp.json headers:")
        logger.info("  X-Foundry-Connect-Hostname: https://your-viafoundry.com")
        logger.info("  X-Foundry-Connect-Token: your-personal-access-token")
    
    logger.info("=" * 60)
    
    # Configure server settings
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    
    # Wrap the MCP app with credentials middleware
    from starlette.middleware.cors import CORSMiddleware
    
    # Get the streamable HTTP app and wrap it
    mcp_app = mcp.streamable_http_app()
    
    # Add credentials middleware with fixed hostname for production mode
    wrapped_app = CredentialsMiddleware(mcp_app, fixed_hostname=fixed_hostname)
    
    # Add CORS middleware
    wrapped_app = CORSMiddleware(
        wrapped_app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )
    
    # Run with uvicorn using matching log config
    import uvicorn
    uvicorn.run(
        wrapped_app,
        host=args.host,
        port=args.port,
        log_config=get_uvicorn_log_config(),
        # Trust X-Forwarded-For / X-Forwarded-Proto from reverse proxies so
        # uvicorn's access log shows the real client IP instead of the proxy's
        # Docker-internal address (e.g. 10.99.0.1).
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
