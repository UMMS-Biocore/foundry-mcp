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
import html
import re
import tempfile
import os
import requests

from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# Import from our modules
from .client import get_client
from .config import (
    set_credentials, validate_credentials, get_fixed_hostname, get_credentials,
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
    run-input shape: the ViaFoundry list of {name,value,type,...}, or the
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
    REQUIRED (echo it from get_run_details(run_id, verbose=True)); `group_id`
    is REQUIRED only when `permission` is 15 (GroupShared) — otherwise it
    may be omitted/None. No input `value` may be an empty string — use "NA"
    or omit the input. Within one processOptions entry, all spreadsheet
    (list) columns must be equal length. This mutates the run; confirm with
    the user before calling.
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
# Pipeline Discovery Tools
# ============================================================================

_PIPELINE_LIST_ENDPOINT = "/api/pipeline/v1/"

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
    (unlike GET /pipeline/v1/{id}, which decodes), so decode them here.
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
                "filter": f"pipelineId:eq={pipeline_id},status:eq=NextSuc",
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
                "verbose=True), then duplicate_run + update_run to adapt it."
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
        data={"dir": remote_dir},
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
            method="GET", endpoint="/api/configurations/v1/user")
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


def _preflight_dataset(via_client, sample_inputs):
    """Resolve each sample input's dataset and return (check, sample_names)."""
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

    if empty:
        return _preflight_check(
            "samples", "fail",
            f"Sample dataset for {', '.join(empty)} has no files.",
            "Add samples with prepare_samples(...) or pick a dataset that has "
            "them, then update_run.",
        ), names
    return _preflight_check(
        "samples", "pass", f"{len(names)} sample(s) attached."), names


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
        dataset_check, dataset_names = _preflight_dataset(via_client, sample_inputs)
        checks.append(dataset_check)

        # Empty / placeholder values.
        blanks = [i["name"] for i in inputs
                  if isinstance(i["value"], str)
                  and i["value"].strip() in _PREFLIGHT_PLACEHOLDERS]
        checks.append(
            _preflight_check(
                "empty_inputs", "fail",
                f"{len(blanks)} input(s) have no value: {', '.join(blanks)}.",
                "Set them with update_run — the pipeline treats these as "
                "missing and may skip steps rather than fail loudly.",
            ) if blanks else
            _preflight_check("empty_inputs", "pass", "All inputs have values.")
        )

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
            "get_run_details(run_id='<example run>', verbose=True) — the full "
            "editable config that update_run needs.",
            "duplicate_run(run_id='<example run>', project_id=..., "
            "pipeline_id=...) — clone the working run.",
            "update_run(...) on the NEW run to apply the user's answers.",
            "initiate_run(run_id='<new run>') — launching uses HPC compute, so "
            "confirm with the user before this step.",
        ],
    },
    {
        "id": "diagnose_a_failure",
        "name": "Work out why a run failed",
        "when": "A run shows as Failed, or the user says a run did not work.",
        "steps": [
            "get_run(run_id='<id>') — confirm the status and which attempt is "
            "current.",
            "get_run_log(run_id='<id>') — returns the log that actually carries "
            "the failure, already tailed to the end where errors live.",
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
            "get_all_report_paths(report_id='<run id>') — the run id IS the "
            "report id.",
            "fetch_report(report_id='<run id>') — the report contents.",
            "list_files / load_file / download_file for specific outputs, and "
            "list_apps + launch_app to open an interactive viewer.",
        ],
    },
]

_RECIPE_TOPICS = {
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
    pipeline — it returns the tool chain for the three journeys people arrive
    with: starting an analysis from a goal, working out why a run failed, and
    finding results.

    Args:
        topic: Optional. Narrow to one journey, e.g. "start", "failed",
            "results". An unrecognised topic returns all of them.
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
                f"Then: duplicate_run(run_id='{example['id']}', project_id=..., "
                f"pipeline_id={pipeline.get('id', pipeline_id)}) -> update_run "
                "on the new run to apply the answers -> initiate_run to launch.",
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
