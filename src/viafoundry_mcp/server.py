#!/usr/bin/env python3
"""
ViaFoundry MCP Server

This MCP server provides access to ViaFoundry's reporting and authentication capabilities.
"""

import os
import json
import logging
from typing import Any
from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp.server.stdio import stdio_server

# Import from our modules
from .client import get_client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('viafoundry-mcp')

# Initialize the MCP server
app = Server("viafoundry-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
        Tool(
            name="fetch_report",
            description=(
                "Fetch report data by report ID. Returns JSON data containing "
                "all processes, files, and metadata for the specified report."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The ID of the report to fetch"
                    }
                },
                "required": ["report_id"]
            }
        ),
        Tool(
            name="list_processes",
            description=(
                "List all unique processes in a report. Returns a list of process names "
                "that have generated output in the specified report."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The ID of the report"
                    }
                },
                "required": ["report_id"]
            }
        ),
        Tool(
            name="list_files",
            description=(
                "List files in a report. If process_name is provided, lists files for that "
                "specific process. Otherwise, lists all files across all processes in the report. "
                "Returns file metadata including file paths, sizes, and extensions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The ID of the report"
                    },
                    "process_name": {
                        "type": "string",
                        "description": "Optional: The name of the process to list files for"
                    }
                },
                "required": ["report_id"]
            }
        ),
        Tool(
            name="download_file",
            description=(
                "Download a specific file from a report. Saves the file to the specified "
                "download directory (defaults to current directory). Returns the local path "
                "where the file was saved."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The ID of the report"
                    },
                    "file_path": {
                        "type": "string",
                        "description": "The path of the file to download (e.g., 'rsem_summary/genes_expression_expected_count.tsv')"
                    },
                    "download_dir": {
                        "type": "string",
                        "description": "Optional: Directory to save the file (defaults to current directory)"
                    }
                },
                "required": ["report_id", "file_path"]
            }
        ),
        Tool(
            name="load_file",
            description=(
                "Load and return the contents of a file from a report. For tabular files "
                "(CSV, TSV, TXT), returns a formatted table. For other files, returns raw content. "
                "Use this when you need to analyze file contents without downloading."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The ID of the report"
                    },
                    "file_path": {
                        "type": "string",
                        "description": "The path of the file to load"
                    },
                    "separator": {
                        "type": "string",
                        "description": "Optional: Separator for tabular files (defaults to tab '\\t')"
                    }
                },
                "required": ["report_id", "file_path"]
            }
        ),
        Tool(
            name="upload_file",
            description=(
                "Upload a file to a report. The file will be organized in the specified "
                "directory within the report. Returns upload status and file information."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The ID of the report"
                    },
                    "local_file_path": {
                        "type": "string",
                        "description": "The local path to the file to upload"
                    },
                    "remote_dir": {
                        "type": "string",
                        "description": "Optional: Directory name for organizing files in the report"
                    }
                },
                "required": ["report_id", "local_file_path"]
            }
        ),
        Tool(
            name="get_report_dirs",
            description=(
                "Get all available directories in a report where files can be uploaded. "
                "Returns a list of directory names that can be used with upload_file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The ID of the report"
                    }
                },
                "required": ["report_id"]
            }
        ),

        # Process Management Tools
        Tool(
            name="list_all_processes",
            description=(
                "List all processes/pipelines in ViaFoundry. Returns details including "
                "process ID, name, summary, and owner information."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_process_details",
            description=(
                "Get detailed information about a specific process/pipeline by ID. "
                "Returns complete process configuration, scripts, parameters, and metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "The ID of the process to fetch"
                    }
                },
                "required": ["process_id"]
            }
        ),
        Tool(
            name="search_datasets",
            description=(
                "Search for dataset files in ViaFoundry metadata system. "
                "Search by filename, collection, or other criteria. "
                "Returns matching dataset files with their metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (filename, path, or other criteria)"
                    },
                    "collection_id": {
                        "type": "string",
                        "description": "Optional: Filter by collection ID"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="search_collections",
            description=(
                "Search for collections in ViaFoundry metadata system. "
                "Collections are groups of related datasets. "
                "Returns matching collections with their metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (collection name or description)"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="get_collection_details",
            description=(
                "Get detailed information about a specific collection by ID. "
                "Returns collection metadata and associated datasets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "string",
                        "description": "The ID of the collection to fetch"
                    }
                },
                "required": ["collection_id"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls."""
    try:
        via_client = get_client()

        if name == "fetch_report":
            report_id = arguments["report_id"]
            logger.info(f"Fetching report {report_id}")

            report_data = via_client.reports.fetch_report_data(report_id)

            # Convert to dict for JSON serialization
            result = report_data.model_dump() if hasattr(report_data, 'model_dump') else report_data

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "list_processes":
            report_id = arguments["report_id"]
            logger.info(f"Listing processes for report {report_id}")

            report_data = via_client.reports.fetch_report_data(report_id)
            processes = via_client.reports.get_process_names(report_data)

            return [TextContent(
                type="text",
                text=json.dumps({"processes": processes}, indent=2)
            )]

        elif name == "list_files":
            report_id = arguments["report_id"]
            process_name = arguments.get("process_name")

            logger.info(f"Listing files for report {report_id}" +
                       (f", process {process_name}" if process_name else " (all processes)"))

            report_data = via_client.reports.fetch_report_data(report_id)

            if process_name:
                files_df = via_client.reports.get_file_names(report_data, process_name)
            else:
                files_df = via_client.reports.get_all_files(report_data)

            # Convert DataFrame to dict
            files_dict = files_df.to_dict(orient='records')

            return [TextContent(
                type="text",
                text=json.dumps({"files": files_dict}, indent=2)
            )]

        elif name == "download_file":
            report_id = arguments["report_id"]
            file_path = arguments["file_path"]
            download_dir = arguments.get("download_dir", os.getcwd())

            logger.info(f"Downloading file {file_path} from report {report_id}")

            report_data = via_client.reports.fetch_report_data(report_id)
            local_path = via_client.reports.download_file(
                report_data,
                file_path,
                download_dir
            )

            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "local_path": local_path,
                    "message": f"File downloaded successfully to {local_path}"
                }, indent=2)
            )]

        elif name == "load_file":
            report_id = arguments["report_id"]
            file_path = arguments["file_path"]
            separator = arguments.get("separator", "\t")

            logger.info(f"Loading file {file_path} from report {report_id}")

            report_data = via_client.reports.fetch_report_data(report_id)
            content = via_client.reports.load_file(report_data, file_path, sep=separator)

            # Check if content is a DataFrame
            if hasattr(content, 'to_dict'):
                # Convert DataFrame to dict
                result = {
                    "type": "dataframe",
                    "data": content.to_dict(orient='records'),
                    "shape": content.shape,
                    "columns": list(content.columns)
                }
            else:
                # Raw content
                result = {
                    "type": "text",
                    "content": str(content)
                }

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "upload_file":
            report_id = arguments["report_id"]
            local_file_path = arguments["local_file_path"]
            remote_dir = arguments.get("remote_dir")

            logger.info(f"Uploading file {local_file_path} to report {report_id}")

            response = via_client.reports.upload_report_file(
                report_id,
                local_file_path,
                dir=remote_dir
            )

            # Convert response to dict
            result = response.model_dump() if hasattr(response, 'model_dump') else response

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_report_dirs":
            report_id = arguments["report_id"]
            logger.info(f"Getting report directories for report {report_id}")

            directories = via_client.reports.get_report_dirs(report_id)

            return [TextContent(
                type="text",
                text=json.dumps({"directories": directories}, indent=2)
            )]

        elif name == "list_all_processes":
            logger.info("Listing all processes")

            processes = via_client.process.list_processes()

            # Convert to dict for JSON serialization
            result = processes.model_dump() if hasattr(processes, 'model_dump') else processes

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_process_details":
            process_id = arguments["process_id"]
            logger.info(f"Getting details for process {process_id}")

            process = via_client.process.get_process(process_id)

            # Convert to dict for JSON serialization
            result = process.model_dump() if hasattr(process, 'model_dump') else process

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "search_datasets":
            query = arguments["query"]
            collection_id = arguments.get("collection_id")

            logger.info(f"Searching datasets with query: {query}" +
                       (f", collection_id: {collection_id}" if collection_id else ""))

            datasets = via_client.metadata.search_dataset_files(
                query=query,
                collection_id=collection_id
            )

            # Convert to dict for JSON serialization
            result = datasets.model_dump() if hasattr(datasets, 'model_dump') else datasets

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "search_collections":
            query = arguments["query"]
            logger.info(f"Searching collections with query: {query}")

            collections = via_client.metadata.search_collections(query=query)

            # Convert to dict for JSON serialization
            result = collections.model_dump() if hasattr(collections, 'model_dump') else collections

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_collection_details":
            collection_id = arguments["collection_id"]
            logger.info(f"Getting details for collection {collection_id}")

            collection = via_client.metadata.get_collection(collection_id)

            # Convert to dict for JSON serialization
            result = collection.model_dump() if hasattr(collection, 'model_dump') else collection

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Error executing tool {name}: {str(e)}", exc_info=True)
        return [TextContent(
            type="text",
            text=json.dumps({
                "error": str(e),
                "tool": name,
                "arguments": arguments
            }, indent=2)
        )]


async def async_main():
    """Run the MCP server (async)."""
    logger.info("Starting ViaFoundry MCP Server")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


def main():
    """Entry point for the MCP server."""
    import asyncio
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
