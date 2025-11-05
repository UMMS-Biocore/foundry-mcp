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
        ),

        # Phase 1 Tools - Process Management
        Tool(
            name="get_process_revisions",
            description=(
                "Get revision history for a specific process/pipeline. "
                "Returns all versions and their changes over time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "The ID of the process to get revisions for"
                    }
                },
                "required": ["process_id"]
            }
        ),
        Tool(
            name="list_process_parameters",
            description=(
                "List all available parameters in ViaFoundry. "
                "Returns parameter definitions including name, type, and constraints."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="get_pipeline_parameters",
            description=(
                "Get parameters for a specific pipeline by ID. "
                "Returns the parameter configuration for the pipeline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pipeline_id": {
                        "type": "string",
                        "description": "The ID of the pipeline to get parameters for"
                    }
                },
                "required": ["pipeline_id"]
            }
        ),

        # Phase 1 Tools - Metadata Canvas
        Tool(
            name="search_canvas",
            description=(
                "Search for canvas visualizations in ViaFoundry. "
                "Canvas objects represent data visualizations and dashboards. "
                "Returns matching canvas items with their metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for canvas name or description"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="get_canvas_details",
            description=(
                "Get detailed information about a specific canvas by ID. "
                "Returns canvas configuration, fields, and visualization settings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canvas_id": {
                        "type": "string",
                        "description": "The ID of the canvas to fetch"
                    }
                },
                "required": ["canvas_id"]
            }
        ),

        # Phase 1 Tools - Metadata Fields
        Tool(
            name="search_metadata_fields",
            description=(
                "Search for metadata field definitions in ViaFoundry. "
                "Fields define the schema for metadata records. "
                "Returns matching field definitions with their types and constraints."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for field name or description"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="get_collection_fields",
            description=(
                "Get metadata fields associated with a specific collection. "
                "Returns the schema/structure of metadata for the collection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "string",
                        "description": "The ID of the collection to get fields for"
                    }
                },
                "required": ["collection_id"]
            }
        ),

        # Phase 1 Tools - Metadata Data Records
        Tool(
            name="search_metadata_records",
            description=(
                "Search for metadata records (data entries) in ViaFoundry. "
                "Metadata records contain actual data values for defined fields. "
                "Returns matching records with their field values."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for metadata records"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="get_metadata_record",
            description=(
                "Get a specific metadata record by ID. "
                "Returns the complete metadata record with all field values."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_id": {
                        "type": "string",
                        "description": "The ID of the metadata record to fetch"
                    }
                },
                "required": ["data_id"]
            }
        ),

        # Phase 1 Tools - Reports
        Tool(
            name="get_all_report_paths",
            description=(
                "Get all file paths (routePaths) for a specific report. "
                "Returns a comprehensive list of all accessible file paths in the report."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "The ID of the report to get paths for"
                    }
                },
                "required": ["report_id"]
            }
        ),

        # Phase 2 Tools - Workflow Enablers
        Tool(
            name="duplicate_process",
            description=(
                "Duplicate/clone an existing process/pipeline. "
                "Creates a copy of the specified process that can be modified independently."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "The ID of the process to duplicate"
                    }
                },
                "required": ["process_id"]
            }
        ),
        Tool(
            name="filter_process_parameters",
            description=(
                "Filter parameters by name, qualifier, file type, or ID. "
                "Returns parameters matching the specified criteria."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Optional: Filter by parameter name"
                    },
                    "qualifier": {
                        "type": "string",
                        "description": "Optional: Filter by qualifier (e.g., 'input', 'output')"
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Optional: Filter by file type (e.g., 'fastq', 'bam')"
                    },
                    "id": {
                        "type": "string",
                        "description": "Optional: Filter by parameter ID"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="add_files_to_dataset",
            description=(
                "Add files to an existing dataset. "
                "Associates specified files with a dataset collection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dataset_id": {
                        "type": "string",
                        "description": "The ID of the dataset to add files to"
                    },
                    "file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Array of file IDs to add to the dataset"
                    }
                },
                "required": ["dataset_id", "file_ids"]
            }
        ),
        Tool(
            name="create_collection",
            description=(
                "Create a new dataset collection. "
                "Collections group related datasets together for organization."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_data": {
                        "type": "object",
                        "description": "Collection configuration (name, description, etc.)"
                    }
                },
                "required": ["collection_data"]
            }
        ),
        Tool(
            name="create_metadata_record",
            description=(
                "Create a new metadata data record. "
                "Adds a new entry with field values to the metadata system."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_record": {
                        "type": "object",
                        "description": "Metadata record with field values"
                    }
                },
                "required": ["data_record"]
            }
        ),
        Tool(
            name="get_field_details",
            description=(
                "Get detailed information about a specific metadata field. "
                "Returns field definition, type, constraints, and usage."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": "string",
                        "description": "The ID of the field to fetch"
                    }
                },
                "required": ["field_id"]
            }
        ),
        Tool(
            name="get_canvas_fields",
            description=(
                "Get metadata fields associated with a specific canvas. "
                "Returns the schema/structure used by the canvas visualization."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canvas_id": {
                        "type": "string",
                        "description": "The ID of the canvas to get fields for"
                    }
                },
                "required": ["canvas_id"]
            }
        ),

        # Phase 3 Tools - Advanced Management
        Tool(
            name="create_process_config",
            description=(
                "Generate a full process configuration using menu group and parameters. "
                "Creates a complete process definition ready for creation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Process name"
                    },
                    "menu_group_name": {
                        "type": "string",
                        "description": "Menu group name for organization"
                    },
                    "input_params": {
                        "type": "array",
                        "description": "Input parameter definitions"
                    },
                    "output_params": {
                        "type": "array",
                        "description": "Output parameter definitions"
                    },
                    "summary": {
                        "type": "string",
                        "description": "Process summary/description"
                    },
                    "script_body": {
                        "type": "string",
                        "description": "Process script body"
                    }
                },
                "required": ["name", "menu_group_name", "script_body"]
            }
        ),
        Tool(
            name="create_process",
            description=(
                "Create a new custom process/pipeline. "
                "Requires complete process configuration including scripts and parameters."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "process_data": {
                        "type": "object",
                        "description": "Complete process configuration"
                    }
                },
                "required": ["process_data"]
            }
        ),
        Tool(
            name="update_process",
            description=(
                "Update an existing process/pipeline. "
                "Modifies process configuration, scripts, or parameters."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "The ID of the process to update"
                    },
                    "process_data": {
                        "type": "object",
                        "description": "Updated process configuration"
                    }
                },
                "required": ["process_id", "process_data"]
            }
        ),
        Tool(
            name="create_process_parameter",
            description=(
                "Create a new parameter for processes. "
                "Defines a new parameter that can be used across multiple processes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "parameter_data": {
                        "type": "object",
                        "description": "Parameter definition (name, type, constraints)"
                    }
                },
                "required": ["parameter_data"]
            }
        ),
        Tool(
            name="update_process_parameter",
            description=(
                "Update an existing process parameter. "
                "Modifies parameter definition, type, or constraints."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "parameter_id": {
                        "type": "string",
                        "description": "The ID of the parameter to update"
                    },
                    "parameter_data": {
                        "type": "object",
                        "description": "Updated parameter definition"
                    }
                },
                "required": ["parameter_id", "parameter_data"]
            }
        ),
        Tool(
            name="create_canvas",
            description=(
                "Create a new canvas visualization/dashboard. "
                "Defines a new data visualization or analysis dashboard."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canvas_data": {
                        "type": "object",
                        "description": "Canvas configuration and visualization settings"
                    }
                },
                "required": ["canvas_data"]
            }
        ),
        Tool(
            name="update_canvas",
            description=(
                "Update an existing canvas visualization. "
                "Modifies canvas configuration or visualization settings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canvas_id": {
                        "type": "string",
                        "description": "The ID of the canvas to update"
                    },
                    "canvas_data": {
                        "type": "object",
                        "description": "Updated canvas configuration"
                    }
                },
                "required": ["canvas_id", "canvas_data"]
            }
        ),
        Tool(
            name="create_metadata_field",
            description=(
                "Create a new metadata field definition. "
                "Defines a new field that can be used in metadata records."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "field_data": {
                        "type": "object",
                        "description": "Field definition (name, type, constraints)"
                    }
                },
                "required": ["field_data"]
            }
        ),
        Tool(
            name="update_metadata_field",
            description=(
                "Update an existing metadata field definition. "
                "Modifies field type, constraints, or other properties."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": "string",
                        "description": "The ID of the field to update"
                    },
                    "field_data": {
                        "type": "object",
                        "description": "Updated field definition"
                    }
                },
                "required": ["field_id", "field_data"]
            }
        ),
        Tool(
            name="update_collection",
            description=(
                "Update an existing collection. "
                "Modifies collection name, description, or configuration."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "string",
                        "description": "The ID of the collection to update"
                    },
                    "collection_data": {
                        "type": "object",
                        "description": "Updated collection data"
                    }
                },
                "required": ["collection_id", "collection_data"]
            }
        ),
        Tool(
            name="update_metadata_record",
            description=(
                "Update an existing metadata record. "
                "Modifies field values in a metadata data record."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_id": {
                        "type": "string",
                        "description": "The ID of the metadata record to update"
                    },
                    "data_record": {
                        "type": "object",
                        "description": "Updated record data"
                    }
                },
                "required": ["data_id", "data_record"]
            }
        ),

        # Phase 4 Tools - Complete Coverage
        Tool(
            name="delete_process",
            description=(
                "Delete a process/pipeline. "
                "Permanently removes the specified process. Use with caution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "The ID of the process to delete"
                    }
                },
                "required": ["process_id"]
            }
        ),
        Tool(
            name="delete_process_parameter",
            description=(
                "Delete a process parameter. "
                "Permanently removes the specified parameter. Use with caution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "parameter_id": {
                        "type": "string",
                        "description": "The ID of the parameter to delete"
                    }
                },
                "required": ["parameter_id"]
            }
        ),
        Tool(
            name="delete_collection",
            description=(
                "Delete a collection. "
                "Permanently removes the specified collection. Use with caution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "string",
                        "description": "The ID of the collection to delete"
                    }
                },
                "required": ["collection_id"]
            }
        ),
        Tool(
            name="delete_canvas",
            description=(
                "Delete a canvas visualization. "
                "Permanently removes the specified canvas. Use with caution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "canvas_id": {
                        "type": "string",
                        "description": "The ID of the canvas to delete"
                    }
                },
                "required": ["canvas_id"]
            }
        ),
        Tool(
            name="delete_metadata_field",
            description=(
                "Delete a metadata field definition. "
                "Permanently removes the specified field. Use with caution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": "string",
                        "description": "The ID of the field to delete"
                    }
                },
                "required": ["field_id"]
            }
        ),
        Tool(
            name="delete_metadata_record",
            description=(
                "Delete a metadata record. "
                "Permanently removes the specified data record. Use with caution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "data_id": {
                        "type": "string",
                        "description": "The ID of the metadata record to delete"
                    }
                },
                "required": ["data_id"]
            }
        ),
        Tool(
            name="create_menu_group",
            description=(
                "Create a new menu group for organizing processes. "
                "Menu groups help organize processes in the UI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "menu_name": {
                        "type": "string",
                        "description": "Name of the menu group to create"
                    }
                },
                "required": ["menu_name"]
            }
        ),
        Tool(
            name="list_menu_groups",
            description=(
                "List all menu groups in ViaFoundry. "
                "Returns all available menu groups used for process organization."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="update_menu_group",
            description=(
                "Update an existing menu group. "
                "Modifies menu group name or properties."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "menu_group_id": {
                        "type": "string",
                        "description": "The ID of the menu group to update"
                    },
                    "menu_name": {
                        "type": "string",
                        "description": "New menu group name"
                    }
                },
                "required": ["menu_group_id", "menu_name"]
            }
        ),
        Tool(
            name="get_menu_group_by_name",
            description=(
                "Find a menu group by its name. "
                "Returns the menu group ID for the specified name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_name": {
                        "type": "string",
                        "description": "Name of the menu group to find"
                    }
                },
                "required": ["group_name"]
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

        # Phase 1 Tools - Process Management
        elif name == "get_process_revisions":
            process_id = arguments["process_id"]
            logger.info(f"Getting revisions for process {process_id}")

            revisions = via_client.process.get_process_revisions(process_id)

            # Convert to dict for JSON serialization
            result = revisions.model_dump() if hasattr(revisions, 'model_dump') else revisions

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "list_process_parameters":
            logger.info("Listing all process parameters")

            parameters = via_client.process.list_parameters()

            # Convert to dict for JSON serialization
            result = parameters.model_dump() if hasattr(parameters, 'model_dump') else parameters

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_pipeline_parameters":
            pipeline_id = arguments["pipeline_id"]
            logger.info(f"Getting parameters for pipeline {pipeline_id}")

            parameters = via_client.process.get_pipeline_parameters(pipeline_id)

            # Convert to dict for JSON serialization
            result = parameters.model_dump() if hasattr(parameters, 'model_dump') else parameters

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        # Phase 1 Tools - Metadata Canvas
        elif name == "search_canvas":
            query = arguments["query"]
            logger.info(f"Searching canvas with query: {query}")

            canvas_results = via_client.metadata.search_canvas(query=query)

            # Convert to dict for JSON serialization
            result = canvas_results.model_dump() if hasattr(canvas_results, 'model_dump') else canvas_results

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_canvas_details":
            canvas_id = arguments["canvas_id"]
            logger.info(f"Getting details for canvas {canvas_id}")

            canvas = via_client.metadata.get_canvas(canvas_id)

            # Convert to dict for JSON serialization
            result = canvas.model_dump() if hasattr(canvas, 'model_dump') else canvas

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        # Phase 1 Tools - Metadata Fields
        elif name == "search_metadata_fields":
            query = arguments["query"]
            logger.info(f"Searching metadata fields with query: {query}")

            fields = via_client.metadata.search_fields(query=query)

            # Convert to dict for JSON serialization
            result = fields.model_dump() if hasattr(fields, 'model_dump') else fields

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_collection_fields":
            collection_id = arguments["collection_id"]
            logger.info(f"Getting fields for collection {collection_id}")

            fields = via_client.metadata.get_collection_fields(collection_id)

            # Convert to dict for JSON serialization
            result = fields.model_dump() if hasattr(fields, 'model_dump') else fields

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        # Phase 1 Tools - Metadata Data Records
        elif name == "search_metadata_records":
            query = arguments["query"]
            logger.info(f"Searching metadata records with query: {query}")

            records = via_client.metadata.search_data(query=query)

            # Convert to dict for JSON serialization
            result = records.model_dump() if hasattr(records, 'model_dump') else records

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        elif name == "get_metadata_record":
            data_id = arguments["data_id"]
            logger.info(f"Getting metadata record {data_id}")

            record = via_client.metadata.get_data(data_id)

            # Convert to dict for JSON serialization
            result = record.model_dump() if hasattr(record, 'model_dump') else record

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        # Phase 1 Tools - Reports
        elif name == "get_all_report_paths":
            report_id = arguments["report_id"]
            logger.info(f"Getting all paths for report {report_id}")

            paths = via_client.reports.get_all_report_paths(report_id)

            # Convert to dict for JSON serialization
            result = paths.model_dump() if hasattr(paths, 'model_dump') else paths

            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]

        # Phase 2 Tools - Workflow Enablers (8 tools)
        elif name == "duplicate_process":
            process_id = arguments["process_id"]
            new_name = arguments.get("new_name")
            logger.info(f"Duplicating process {process_id}")

            duplicated = via_client.process.duplicate_process(process_id, new_name)
            result = duplicated.model_dump() if hasattr(duplicated, 'model_dump') else duplicated

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "filter_process_parameters":
            parameters = arguments.get("parameters", {})
            filters = arguments.get("filters", {})
            logger.info(f"Filtering process parameters")

            filtered = via_client.process.filter_parameters(parameters, filters)
            result = filtered.model_dump() if hasattr(filtered, 'model_dump') else filtered

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "add_files_to_dataset":
            collection_id = arguments["collection_id"]
            file_paths = arguments["file_paths"]
            archive_dir = arguments.get("archive_dir")
            logger.info(f"Adding {len(file_paths)} files to dataset in collection {collection_id}")

            result_obj = via_client.metadata.add_files_to_dataset(collection_id, file_paths, archive_dir)
            result = result_obj.model_dump() if hasattr(result_obj, 'model_dump') else result_obj

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_collection":
            name = arguments["name"]
            description = arguments.get("description")
            logger.info(f"Creating collection: {name}")

            collection = via_client.metadata.create_collection(name, description)
            result = collection.model_dump() if hasattr(collection, 'model_dump') else collection

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_metadata_record":
            collection_id = arguments["collection_id"]
            data = arguments["data"]
            logger.info(f"Creating metadata record in collection {collection_id}")

            record = via_client.metadata.create_data(collection_id, data)
            result = record.model_dump() if hasattr(record, 'model_dump') else record

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_field_details":
            field_id = arguments["field_id"]
            logger.info(f"Getting field details for field {field_id}")

            field = via_client.metadata.get_field(field_id)
            result = field.model_dump() if hasattr(field, 'model_dump') else field

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_canvas_fields":
            canvas_id = arguments["canvas_id"]
            logger.info(f"Getting fields for canvas {canvas_id}")

            fields = via_client.metadata.get_canvas_fields(canvas_id)
            result = fields.model_dump() if hasattr(fields, 'model_dump') else fields

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Phase 3 Tools - Advanced Management (12 tools)
        elif name == "create_process_config":
            process_id = arguments["process_id"]
            config = arguments["config"]
            logger.info(f"Creating process config for process {process_id}")

            result_obj = via_client.process.create_process_config(process_id, config)
            result = result_obj.model_dump() if hasattr(result_obj, 'model_dump') else result_obj

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_process":
            name = arguments["name"]
            process_data = arguments.get("process_data", {})
            logger.info(f"Creating process: {name}")

            process = via_client.process.create_process(name, **process_data)
            result = process.model_dump() if hasattr(process, 'model_dump') else process

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "update_process":
            process_id = arguments["process_id"]
            process_data = arguments["process_data"]
            logger.info(f"Updating process {process_id}")

            updated = via_client.process.update_process(process_id, process_data)
            result = updated.model_dump() if hasattr(updated, 'model_dump') else updated

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_process_parameter":
            process_id = arguments["process_id"]
            parameter_data = arguments["parameter_data"]
            logger.info(f"Creating process parameter for process {process_id}")

            param = via_client.process.create_parameter(parameter_data)
            result = param.model_dump() if hasattr(param, 'model_dump') else param

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "update_process_parameter":
            parameter_id = arguments["parameter_id"]
            parameter_data = arguments["parameter_data"]
            logger.info(f"Updating process parameter {parameter_id}")

            updated = via_client.process.update_parameter(parameter_id, parameter_data)
            result = updated.model_dump() if hasattr(updated, 'model_dump') else updated

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_canvas":
            canvas_data = arguments["canvas_data"]
            logger.info(f"Creating canvas")

            canvas = via_client.metadata.create_canvas(canvas_data)
            result = canvas.model_dump() if hasattr(canvas, 'model_dump') else canvas

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "update_canvas":
            canvas_id = arguments["canvas_id"]
            canvas_data = arguments["canvas_data"]
            logger.info(f"Updating canvas {canvas_id}")

            updated = via_client.metadata.update_canvas(canvas_id, canvas_data)
            result = updated.model_dump() if hasattr(updated, 'model_dump') else updated

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_metadata_field":
            field_data = arguments["field_data"]
            logger.info(f"Creating metadata field")

            field = via_client.metadata.create_field(field_data)
            result = field.model_dump() if hasattr(field, 'model_dump') else field

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "update_metadata_field":
            field_id = arguments["field_id"]
            field_data = arguments["field_data"]
            logger.info(f"Updating metadata field {field_id}")

            updated = via_client.metadata.update_field(field_id, field_data)
            result = updated.model_dump() if hasattr(updated, 'model_dump') else updated

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "update_collection":
            collection_id = arguments["collection_id"]
            collection_data = arguments["collection_data"]
            logger.info(f"Updating collection {collection_id}")

            updated = via_client.metadata.update_collection(collection_id, collection_data)
            result = updated.model_dump() if hasattr(updated, 'model_dump') else updated

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "update_metadata_record":
            record_id = arguments["record_id"]
            data = arguments["data"]
            logger.info(f"Updating metadata record {record_id}")

            updated = via_client.metadata.update_data(record_id, data)
            result = updated.model_dump() if hasattr(updated, 'model_dump') else updated

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # Phase 4 Tools - Complete Coverage (14 tools)
        elif name == "delete_process":
            process_id = arguments["process_id"]
            logger.info(f"Deleting process {process_id}")

            result_obj = via_client.process.delete_process(process_id)
            result = result_obj.model_dump() if hasattr(result_obj, 'model_dump') else result_obj

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "delete_process_parameter":
            parameter_id = arguments["parameter_id"]
            logger.info(f"Deleting process parameter {parameter_id}")

            result_obj = via_client.process.delete_parameter(parameter_id)
            result = result_obj.model_dump() if hasattr(result_obj, 'model_dump') else result_obj

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "delete_collection":
            collection_id = arguments["collection_id"]
            logger.info(f"Deleting collection {collection_id}")

            result_obj = via_client.metadata.delete_collection(collection_id)
            result = result_obj.model_dump() if hasattr(result_obj, 'model_dump') else result_obj

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "delete_canvas":
            canvas_id = arguments["canvas_id"]
            logger.info(f"Deleting canvas {canvas_id}")

            result_obj = via_client.metadata.delete_canvas(canvas_id)
            result = result_obj.model_dump() if hasattr(result_obj, 'model_dump') else result_obj

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "delete_metadata_field":
            field_id = arguments["field_id"]
            logger.info(f"Deleting metadata field {field_id}")

            result_obj = via_client.metadata.delete_field(field_id)
            result = result_obj.model_dump() if hasattr(result_obj, 'model_dump') else result_obj

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "delete_metadata_record":
            record_id = arguments["record_id"]
            logger.info(f"Deleting metadata record {record_id}")

            result_obj = via_client.metadata.delete_data(record_id)
            result = result_obj.model_dump() if hasattr(result_obj, 'model_dump') else result_obj

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "create_menu_group":
            menu_data = arguments["menu_data"]
            logger.info(f"Creating menu group")

            menu = via_client.process.create_menu_group(menu_data)
            result = menu.model_dump() if hasattr(menu, 'model_dump') else menu

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "list_menu_groups":
            filters = arguments.get("filters", {})
            logger.info(f"Listing menu groups")

            menus = via_client.process.list_menu_groups(**filters)
            result = menus.model_dump() if hasattr(menus, 'model_dump') else menus

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "update_menu_group":
            menu_id = arguments["menu_id"]
            menu_data = arguments["menu_data"]
            logger.info(f"Updating menu group {menu_id}")

            updated = via_client.process.update_menu_group(menu_id, menu_data)
            result = updated.model_dump() if hasattr(updated, 'model_dump') else updated

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_menu_group_by_name":
            name = arguments["name"]
            logger.info(f"Getting menu group by name: {name}")

            menu = via_client.process.get_menu_group_by_name(name)
            result = menu.model_dump() if hasattr(menu, 'model_dump') else menu

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

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
