#!/usr/bin/env python3
"""
Foundry Connect MCP HTTP Server

Credentials are configured in mcp.json via headers:
{
  "foundry": {
    "url": "http://127.0.0.1:8705/mcp",
    "headers": {
      "X-ViaFoundry-Hostname": "https://your-viafoundry.com",
      "X-ViaFoundry-Token": "your-token-here"
    }
  }
}

Run with: python -m foundry_mcp.server --port 8705
"""

import json
import logging
import argparse
import base64
import tempfile
import os
import requests

from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# Import from our modules
from .client import get_client
from .config import (
    set_credentials, validate_credentials, get_fixed_hostname,
    HEADER_HOSTNAME, HEADER_TOKEN, HEADER_HOSTNAME_NEW, HEADER_TOKEN_NEW
)
from .utils import serialize_response, MCP_TOKEN_PREFIX, remove_none, envelope, tail_text
from .log import get_logger, get_uvicorn_log_config, mask_token


# Get logger for this module
logger = get_logger(__name__)

# Maximum file size for upload/download via base64 (200MB)
# Base64 encoding increases size by ~33%, so 200MB file becomes ~267MB in transport
MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024  # 200MB


class CredentialsMiddleware:
    """
    Middleware that extracts Foundry Connect credentials from request headers,
    validates them, and stores them in context variables for use by tool handlers.
    
    Security Modes:
      - Fixed Hostname Mode (production): When fixed_hostname is set, the server
        uses that hostname for all requests, ignoring client X-ViaFoundry-Hostname
        headers. This prevents the server from being used as an open proxy.
      
      - Open Mode (development): When fixed_hostname is None, the server accepts
        X-ViaFoundry-Hostname from clients, allowing connection to any Foundry Connect
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
            # Prefer the explicit X-ViaFoundry-Token header (IDE/manual setup);
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
                detail = "Missing X-ViaFoundry-Token header."
            elif not token.startswith(MCP_TOKEN_PREFIX):
                detail = f"Invalid token format: X-ViaFoundry-Token must start with '{MCP_TOKEN_PREFIX}'"
            else:
                detail = "Invalid credentials."
            help_msg = "Configure X-ViaFoundry-Token in mcp.json headers."
        else:
            # Open mode - need both hostname and token from client
            if not hostname and not token:
                detail = "Missing credentials. Provide X-ViaFoundry-Hostname and X-ViaFoundry-Token headers."
            elif not hostname:
                detail = "Missing X-ViaFoundry-Hostname header."
            elif not token:
                detail = "Missing X-ViaFoundry-Token header."
            elif not (hostname.startswith("http://") or hostname.startswith("https://")):
                detail = f"Invalid hostname format: '{hostname}'. Must start with http:// or https://"
            elif not token.startswith(MCP_TOKEN_PREFIX):
                detail = f"Invalid token format: X-ViaFoundry-Token must start with '{MCP_TOKEN_PREFIX}'"
            else:
                detail = "Invalid credentials."
            help_msg = "Configure credentials in mcp.json headers: X-ViaFoundry-Hostname and X-ViaFoundry-Token"
        
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
    # CredentialsMiddleware (X-ViaFoundry-Token). The proxy may forward any
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


@mcp.tool()
def download_file(report_id: str, file_path: str) -> str:
    """
    Download a file from a report and return its content as base64.
    This allows downloading files regardless of server filesystem access.
    The client can then decode and save the file locally.
    
    Args:
        report_id: The ID of the report containing the file.
        file_path: The path of the file within the report (from list_files output).
    
    Returns:
        JSON with file_name, file_content_base64, and file_size.
        The client should decode the base64 content and save to disk.
    
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
        
        # Download the file content
        # Timeout: (connect_timeout, read_timeout) in seconds
        response = requests.get(file_url, headers=via_client.auth.get_headers(), timeout=(20, 300))
        if response.status_code != 200:
            # Include detailed error info for debugging
            reason = getattr(response, 'reason', 'Unknown')
            text = getattr(response, 'text', '')
            error_detail = text[:500] if text else reason
            return json.dumps({
                "error": f"Failed to download file: HTTP {response.status_code} {reason}",
                "status_code": response.status_code,
                "detail": error_detail,
                "url": file_url
            }, indent=2)
        
        # Check file size before base64 encoding
        content_size = len(response.content)
        if content_size > MAX_FILE_SIZE_BYTES:
            size_mb = content_size / (1024 * 1024)
            max_mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
            return json.dumps({
                "error": f"File too large for download: {size_mb:.1f}MB exceeds {max_mb:.0f}MB limit",
                "hint": "Use the SDK directly for large file downloads"
            }, indent=2)
        
        # Encode content as base64
        file_content_base64 = base64.b64encode(response.content).decode('utf-8')
        
        logger.info(f"Downloaded file '{file_name}' ({content_size} bytes)")
        
        return json.dumps({
            "success": True,
            "file_name": file_name,
            "file_size": len(response.content),
            "file_content_base64": file_content_base64,
            "message": f"File downloaded successfully. Decode base64 content and save to disk."
        }, indent=2)
        
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return json.dumps({"error": str(e)})


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

@mcp.tool()
def list_runs(
    search_query: str = "",
    take: int = 10,
    skip: int = 0,
    sort: str = "dateCreated",
    order: str = "desc"
) -> str:
    """
    List and search for runs/pipeline executions in Foundry Connect.
    Supports fuzzy search by name, pagination, and sorting.
    Returns run details including ID (same as report_id), name, status, pipeline info, and dates.
    Use this to discover runs and get their IDs for use with other tools.
    """
    try:
        via_client = get_client()
        logger.info(f"Listing runs (search: '{search_query}', take: {take}, skip: {skip})")
        
        response = via_client.call(
            method="POST",
            endpoint="/api/v1/run/list",
            params={
                "take": take,
                "skip": skip,
                "sort": sort,
                "order": order
            },
            data={"searchKey": search_query}
        )
        
        return json.dumps(response, indent=2)
    except Exception as e:
        logger.error(f"Error listing runs: {e}")
        return json.dumps({"error": str(e)})


# Bench-friendly labels for raw RunStatus values (mirrors the frontend's
# getRunStatusDisplayText, but says "Failed" instead of "Error" for clarity).
_RUN_STATUS_DISPLAY = {
    "NextErr": "Failed",
    "Error": "Failed",
    "NextSuc": "Completed",
    "NextRun": "Running",
    "init": "Initializing",
    "Waiting": "Initializing",
    "Terminated": "Terminated",
    "NotSubmitted": "Not submitted",
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

        run_obj = result.get("run")
        if isinstance(run_obj, dict):
            display = _human_run_status(run_obj.get("status"))
            result["status_display"] = display
            name = run_obj.get("name")
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
_LOG_PRIORITY = [
    ".command.err", "err.log", ".command.log", "log.txt",
    ".nextflow.log", "serverlog.txt",
]


def _pick_diagnostic_log(logs):
    """From a list of {"name","content"} dicts, return (name, content) of the
    most useful non-empty log for diagnosis, or (None, None) if all are empty."""
    by_name = {
        entry.get("name"): (entry.get("content") or "")
        for entry in logs
        if isinstance(entry, dict)
    }
    for name in _LOG_PRIORITY:
        if by_name.get(name, "").strip():
            return name, by_name[name]
    for name, content in by_name.items():
        if content.strip():
            return name, content
    return None, None


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
            result = envelope(
                summary=(
                    f"No log output is available yet for run {run_id}. If it is "
                    f"still starting or running on a cluster, logs may not have "
                    f"synced — try again shortly."
                ),
                data={"logs": []},
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
                    entry.get("name") for entry in logs if isinstance(entry, dict)
                ],
            },
            next_steps=[
                f"If it failed, get_run_details(run_id='{run_id}') shows the "
                f"inputs/params that caused it.",
                "Fix the cause, then re-launch with "
                "initiate_run(run_type='resumerun') to reuse completed steps.",
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
    """Yield (name, value, type) triples from either run-input shape:
    the ViaFoundry list of {name,value,type}, or the external-pipeline
    (nf-core/Nextflow) dict of {name: {...}} that the backend returns instead."""
    if isinstance(inputs, dict):
        for name, val in inputs.items():
            if isinstance(val, dict) and "value" in val:
                yield name, val.get("value"), val.get("type")
            else:
                yield name, val, None
        return
    for inp in inputs or []:
        if isinstance(inp, dict):
            yield inp.get("name"), inp.get("value"), inp.get("type")


def _summarize_run_details(details):
    """Distill a run's full details blob into a compact, plain-language summary
    a bench scientist can read without wading through processOptions."""
    pipeline = details.get("mainPipeline") or {}
    project = details.get("project") or {}
    inputs = details.get("inputs") or []
    proc_opts = details.get("processOptions") or {}

    sample_inputs, settings, reference_paths = [], [], []
    for name, value, itype in _iter_run_inputs(inputs):
        if itype == "vmetaCollection":
            sample_inputs.append({"name": name, "dataset": value})
        elif isinstance(value, str) and value.startswith("/"):
            reference_paths.append(name)
        else:
            settings.append({"name": name, "value": value})

    return {
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
        result = envelope(
            summary=(
                f"Run {run_id} uses pipeline '{pipeline['name']}' "
                f"(v{pipeline['version']}) with {len(summary_data['settings'])} "
                f"settings and {summary_data['process_option_groups']} "
                f"process-option groups."
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


@mcp.tool()
def duplicate_run(run_id: str, project_id: int, pipeline_id: int) -> str:
    """
    Duplicate an existing run into a target project/pipeline and return the
    new run's `duplicatedRunId`. `project_id` and `pipeline_id` come from
    get_run_details on the source run (its `projectId` and `mainPipeline.id`).
    Use `duplicatedRunId` from the response when wiring into update_run or
    initiate_run. NOTE: the duplicate may DROP the vmetaCollection input (e.g.
    `reads`) and copy processOptions with empty arrays — re-add/patch them
    with update_run before initiate_run. Path inputs (references, genomes) are
    copied verbatim and may point at the source project's paths.
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


def _validate_update_run(inputs, process_options, permission, group_id):
    """Raise ValueError if the update_run body would be rejected by the server."""
    if permission is None:
        raise ValueError("'permission' is required")
    if permission == 15 and group_id is None:
        raise ValueError("'groupId' is required when permission is GroupShared (15)")
    for i, inp in enumerate(inputs or []):
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
    inputs: list,
    process_options: dict,
    permission: int,
    group_id: int = None,
) -> str:
    """
    Patch a run's inputs and processOptions (PATCH /save). `permission` is
    REQUIRED (echo it from get_run_details); `group_id` is REQUIRED only when
    `permission` is 15 (GroupShared) — otherwise it may be omitted/None. No
    input `value` may be an empty string — use "NA" or omit the input. Within
    one processOptions entry, all spreadsheet (list) columns must be equal
    length. This mutates the run; confirm with the user before calling.
    """
    try:
        _validate_update_run(inputs, process_options, permission, group_id)
        via_client = get_client()
        saved = via_client.call(
            method="PATCH",
            endpoint=f"/api/v1/run/{run_id}/save",
            data={
                "inputs": inputs,
                "processOptions": process_options,
                "permission": permission,
                "groupId": group_id,
            },
        )
        return json.dumps(saved, indent=2)
    except Exception as e:
        logger.error(f"Error updating run {run_id}: {e}")
        return json.dumps({"error": str(e)})


_VALID_RUN_TYPES = ("newrun", "resumerun", "rerun")


@mcp.tool()
def initiate_run(run_id: str, run_type: str = "newrun") -> str:
    """
    Start execution of a prepared run. run_type: 'newrun' (fresh), 'resumerun'
    (Nextflow -resume, reuses work dir/cache), or 'rerun' (new attempt, same
    params). Returns status, runUUID, localRunDir. This LAUNCHES real HPC compute
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
            existing = via_client.process.get_process(process_id)
            existing_data = serialize_response(existing)
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
            user_info = via_client.call(method="GET", endpoint="/api/auth/v1/user")
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

        from viafoundry.models.domain.process import ProcessConfig

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

@mcp.tool()
def search_datasets(dataset_id: str, filter_data: dict = None) -> str:
    """
    Search for files in a vmeta dataset with filtering, sorting, and pagination.
    
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
        
        # Call the /api/app/v1 endpoint to get all apps
        response = via_client.call(
            method="GET",
            endpoint="/api/app/v1"
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
        endpoint = f"/api/app/v1/call/{app_id}"
        
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
        "X-ViaFoundry-Hostname": "https://your-viafoundry.com",
        "X-ViaFoundry-Token": "your-personal-access-token"
      }
    }
  }
}

Security Modes:
  - Fixed Hostname: Set FRONTEND_HOSTNAME (and optionally FRONTEND_PROTOCOL,
    FRONTEND_PATH_PREFIX) to lock the server to a specific Foundry Connect instance.
    Used for production deployments.
  - Open Mode: Without FRONTEND_HOSTNAME, clients can specify any target via
    X-ViaFoundry-Hostname header (development mode, only safe for localhost).

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
        logger.info("  Client X-ViaFoundry-Hostname headers will be ignored")
        logger.info("")
        logger.info("Configure in mcp.json headers:")
        logger.info("  X-ViaFoundry-Token: your-personal-access-token")
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
        logger.info("  X-ViaFoundry-Hostname: https://your-viafoundry.com")
        logger.info("  X-ViaFoundry-Token: your-personal-access-token")
    
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
