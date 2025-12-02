#!/usr/bin/env python3
"""
ViaFoundry MCP HTTP Server

Credentials are configured in mcp.json via headers:
{
  "viafoundry": {
    "url": "http://127.0.0.1:8000/mcp",
    "headers": {
      "X-ViaFoundry-Hostname": "https://your-viafoundry.com",
      "X-ViaFoundry-Token": "your-token-here"
    }
  }
}

Run with: python -m viafoundry_mcp.http_server --port 8000
"""

import os
import json
import logging
import argparse

from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.fastmcp import FastMCP

# Import from our modules
from .client import get_client
from .config import set_credentials, HEADER_HOSTNAME, HEADER_TOKEN
from .utils import serialize_response


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('viafoundry-mcp-http')


class CredentialsMiddleware:
    """
    Middleware that extracts ViaFoundry credentials from request headers
    and stores them in context variables for use by tool handlers.
    """
    
    def __init__(self, app: ASGIApp):
        self.app = app
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            # Extract headers (they're stored as list of tuples)
            headers = dict(scope.get("headers", []))
            
            # Get credentials from headers (header names are lowercase bytes)
            hostname = headers.get(HEADER_HOSTNAME.encode(), b"").decode()
            token = headers.get(HEADER_TOKEN.encode(), b"").decode()
            
            # Set credentials in context for this request
            if hostname and token:
                set_credentials(hostname, token)
                logger.debug(f"Credentials set from headers: {hostname}")
        
        await self.app(scope, receive, send)


def create_mcp_server(stateless: bool = False) -> FastMCP:
    """
    Create and configure the FastMCP server.
    
    Args:
        stateless: If True, run in stateless mode (no session persistence).
                   Better for serverless/Lambda deployments.
    """
    return FastMCP(
        "viafoundry-mcp",
        stateless_http=stateless,
    )


# Initialize the FastMCP server (stateful by default)
mcp = create_mcp_server(stateless=False)


# ============================================================================
# Report Management Tools
# ============================================================================

@mcp.tool()
def fetch_report(report_id: str) -> str:
    """
    Fetch report data by report ID. Returns JSON data containing
    all processes, files, and metadata for the specified report.
    """
    try:
        via_client = get_client()
        logger.info(f"Fetching report {report_id}")
        report_data = via_client.reports.fetch_report_data(report_id)
        result = serialize_response(report_data)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error fetching report: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_processes(report_id: str) -> str:
    """
    List all unique processes in a report. Returns a list of process names
    that have generated output in the specified report.
    """
    try:
        via_client = get_client()
        logger.info(f"Listing processes for report {report_id}")
        report_data = via_client.reports.fetch_report_data(report_id)
        processes = via_client.reports.get_process_names(report_data)
        return json.dumps({"processes": processes}, indent=2)
    except Exception as e:
        logger.error(f"Error listing processes: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_files(report_id: str, process_name: str = None) -> str:
    """
    List files in a report. If process_name is provided, lists files for that
    specific process. Otherwise, lists all files across all processes in the report.
    Returns file metadata including file paths, sizes, and extensions.
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
        logger.error(f"Error listing files: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


@mcp.tool()
def download_file(report_id: str, file_path: str, download_dir: str = None) -> str:
    """
    Download a specific file from a report. Saves the file to the specified
    download directory (defaults to current directory). Returns the local path
    where the file was saved.
    """
    try:
        via_client = get_client()
        if download_dir is None:
            download_dir = os.getcwd()
        
        logger.info(f"Downloading file {file_path} from report {report_id}")
        
        report_data = via_client.reports.fetch_report_data(report_id)
        local_path = via_client.reports.download_file(report_data, file_path, download_dir)
        
        return json.dumps({
            "success": True,
            "local_path": local_path,
            "message": f"File downloaded successfully to {local_path}"
        }, indent=2)
    except Exception as e:
        logger.error(f"Error downloading file: {e}", exc_info=True)
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
        logger.error(f"Error loading file: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


@mcp.tool()
def upload_file(report_id: str, local_file_path: str, remote_dir: str = None) -> str:
    """
    Upload a file to a report. The file will be organized in the specified
    directory within the report. Returns upload status and file information.
    """
    try:
        via_client = get_client()
        logger.info(f"Uploading file {local_file_path} to report {report_id}")
        
        response = via_client.reports.upload_report_file(
            report_id,
            local_file_path,
            dir=remote_dir
        )
        
        result = serialize_response(response)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error uploading file: {e}", exc_info=True)
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
        logger.error(f"Error getting report directories: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


# ============================================================================
# Process/Pipeline Management Tools
# ============================================================================

@mcp.tool()
def list_all_processes() -> str:
    """
    List all processes/pipelines in ViaFoundry. Returns details including
    process ID, name, summary, and owner information.
    """
    try:
        via_client = get_client()
        logger.info("Listing all processes")
        processes = via_client.process.list_processes()
        result = serialize_response(processes)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing all processes: {e}", exc_info=True)
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
        logger.error(f"Error getting process details: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


# ============================================================================
# Metadata & Dataset Search Tools
# ============================================================================

@mcp.tool()
def search_datasets(query: str, collection_id: str = None) -> str:
    """
    Search for dataset files in ViaFoundry metadata system.
    Search by filename, collection, or other criteria.
    Returns matching dataset files with their metadata.
    """
    try:
        via_client = get_client()
        logger.info(f"Searching datasets with query: {query}" +
                   (f", collection_id: {collection_id}" if collection_id else ""))
        
        datasets = via_client.metadata.search_dataset_files(
            query=query,
            collection_id=collection_id
        )
        
        result = serialize_response(datasets)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error searching datasets: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


@mcp.tool()
def search_collections(query: str) -> str:
    """
    Search for collections in ViaFoundry metadata system.
    Collections are groups of related datasets.
    Returns matching collections with their metadata.
    """
    try:
        via_client = get_client()
        logger.info(f"Searching collections with query: {query}")
        collections = via_client.metadata.search_collections(query)
        result = serialize_response(collections)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error searching collections: {e}", exc_info=True)
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
        logger.error(f"Error getting collection details: {e}", exc_info=True)
        return json.dumps({"error": str(e)})


# ============================================================================
# Server Entry Point
# ============================================================================

def main():
    """Entry point for the HTTP MCP server."""
    parser = argparse.ArgumentParser(
        description='ViaFoundry MCP HTTP Server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Credentials are configured in ~/.cursor/mcp.json:

{
  "mcpServers": {
    "viafoundry": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "X-ViaFoundry-Hostname": "https://your-viafoundry.com",
        "X-ViaFoundry-Token": "your-personal-access-token"
      }
    }
  }
}

Examples:
  python -m viafoundry_mcp.http_server --port 8000
        """
    )
    parser.add_argument('--port', type=int, default=8000, 
                        help='Port to run the server on (default: 8000)')
    parser.add_argument('--host', type=str, default='127.0.0.1', 
                        help='Host to bind to (default: 127.0.0.1)')
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info("ViaFoundry MCP HTTP Server")
    logger.info("=" * 60)
    logger.info(f"Endpoint: http://{args.host}:{args.port}/mcp")
    logger.info("")
    logger.info("Configure credentials in mcp.json headers:")
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
    
    # Add credentials middleware
    wrapped_app = CredentialsMiddleware(mcp_app)
    
    # Add CORS middleware
    wrapped_app = CORSMiddleware(
        wrapped_app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )
    
    # Run with uvicorn
    import uvicorn
    uvicorn.run(
        wrapped_app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

