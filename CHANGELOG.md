# Changelog

## [1.1.0] - 2025-12-08

### Changed
- **BREAKING: Removed STDIO server** - Simplified to HTTP-only architecture
  - Deleted `server.py` (STDIO transport)
  - Renamed `viafoundry-mcp-http` command to `viafoundry-mcp`
  - HTTP server is now the only transport method
  
### Improved
- **Code consistency** - All tool handlers now use `serialize_response()` utility
  - Better handling of nested Pydantic models
  - Protection against circular references
  - Consistent JSON serialization across all 42 tools

### Migration
If upgrading from 1.0.x:
- The `viafoundry-mcp-http` command is now just `viafoundry-mcp`
- STDIO transport is no longer supported
- HTTP configuration in `mcp.json` remains the same

## [1.0.1] - 2025-11-05

### Added
- **Major Expansion: 42 New Tools Across 4 Phases** (50 tools total):

  **Phase 1 - High-Impact Read Tools** (10 tools):
  - `get_process_revisions` - Get revision history for a specific process/pipeline
  - `list_process_parameters` - List all available parameters in ViaFoundry
  - `get_pipeline_parameters` - Get parameters for a specific pipeline by ID
  - `search_canvas` - Search for canvas visualizations in ViaFoundry
  - `get_canvas_details` - Get detailed information about a specific canvas
  - `search_metadata_fields` - Search for metadata field definitions
  - `get_collection_fields` - Get metadata fields for a specific collection
  - `search_metadata_records` - Search for metadata data records
  - `get_metadata_record` - Get a specific metadata record by ID
  - `get_all_report_paths` - Get all file paths for a specific report

  **Phase 2 - Workflow Enablers** (7 tools):
  - `duplicate_process` - Duplicate an existing process/pipeline
  - `filter_process_parameters` - Filter parameters by name, qualifier, file type, or ID
  - `add_files_to_dataset` - Add files to a dataset in a collection
  - `create_collection` - Create a new collection in the metadata system
  - `create_metadata_record` - Create a new metadata record in a collection
  - `get_field_details` - Get detailed information about a metadata field
  - `get_canvas_fields` - Get all fields associated with a canvas

  **Phase 3 - Advanced Management** (11 tools):
  - `create_process_config` - Create a process configuration
  - `create_process` - Create a new process/pipeline
  - `update_process` - Update an existing process/pipeline
  - `create_process_parameter` - Create a new process parameter
  - `update_process_parameter` - Update an existing process parameter
  - `create_canvas` - Create a new canvas visualization
  - `update_canvas` - Update an existing canvas
  - `create_metadata_field` - Create a new metadata field
  - `update_metadata_field` - Update an existing metadata field
  - `update_collection` - Update a collection
  - `update_metadata_record` - Update a metadata record

  **Phase 4 - Complete Coverage** (14 tools):
  - `delete_process` - Delete a process/pipeline
  - `delete_process_parameter` - Delete a process parameter
  - `delete_collection` - Delete a collection
  - `delete_canvas` - Delete a canvas
  - `delete_metadata_field` - Delete a metadata field
  - `delete_metadata_record` - Delete a metadata record
  - `create_menu_group` - Create a new menu group
  - `list_menu_groups` - List all menu groups
  - `update_menu_group` - Update a menu group
  - `get_menu_group_by_name` - Get menu group by name

### Fixed
- **SDK Method Call Corrections**:
  - Fixed menu group methods to use `process` module (not `metadata`)
  - Fixed parameter method names: `filter_parameters`, `create_parameter`, `update_parameter`, `delete_parameter`
  - Removed 2 non-existent tools: `collect_report_files`, `process_parameter`
  - All 50 tools now verified against ViaFoundry SDK

### Improved
- **Tool Count**: Expanded from 12 to 50 tools
- **Test Coverage**: Updated integration tests with 50-tool verification
- **Code Quality**: All SDK method calls verified and corrected

### Summary
This major release brings comprehensive ViaFoundry functionality to MCP:
- **CRUD Operations**: Full create, read, update, delete support for processes, metadata, and canvases
- **Workflow Management**: Tools for duplicating processes, managing parameters, and organizing data
- **Menu Groups**: Complete menu group management (create, list, update, get by name)
- **Data Management**: Dataset creation, file management, and metadata operations
- **Quality Assurance**: All tools verified against SDK, all tests passing (11/11)

**Tool Breakdown by Module**:
- Reports: 9 tools
- Process: 19 tools
- Metadata: 22 tools

**Total: 50 verified, working tools**

## [1.0.0] - 2025-11-04

### Added
- **Process/Pipeline Management Tools** (2 new tools):
  - `list_all_processes` - List all processes/pipelines in ViaFoundry
  - `get_process_details` - Get detailed information about a specific process by ID

- **Metadata & Dataset Search Tools** (3 new tools):
  - `search_datasets` - Search for dataset files in ViaFoundry metadata system
  - `search_collections` - Search for collections in ViaFoundry metadata system
  - `get_collection_details` - Get detailed information about a specific collection by ID

- **Interactive Setup**:
  - `viafoundry-mcp-setup` command for easy credential configuration
  - Interactive prompts when credentials are missing
  - Automatic .env file creation in recommended location

- **Test Suite**:
  - Unit tests for configuration management
  - Unit tests for client management
  - Integration tests for MCP tools
  - 11 test cases with pytest

### Changed
- **Code Reorganization**:
  - Renamed `mcp_server/` to `src/viafoundry_mcp/`
  - Split code into modular files: `server.py`, `client.py`, `config.py`
  - Created proper `test/` directory structure
  - Better separation of concerns

- **Documentation**:
  - Completely rewritten README with user-centric approach
  - Step-by-step IDE setup for Cursor, Claude Code, VSCode
  - Comprehensive troubleshooting section
  - Real-world example conversations

- **Tools**:
  - Expanded from 7 tools to 12 tools total
  - Categorized tools by function (Reports, Processes, Metadata)

### Summary
This release completes the ViaFoundry MCP implementation with:
- Full ViaFoundry API coverage (12 tools)
- Interactive credential management
- Modular, maintainable codebase
- Comprehensive test suite
- User-friendly documentation

## [0.1.0] - 2025-11-03

### Added
- Initial release of ViaFoundry MCP Server
- 7 MCP tools for ViaFoundry interaction:
  - fetch_report
  - list_processes
  - list_files
  - download_file
  - load_file
  - upload_file
  - get_report_dirs
- Automatic .env file detection from multiple locations
- Support for installation from GitHub
- UV package manager compatibility
- Comprehensive documentation (README, QUICKSTART, README-MCP)

### Changed
- **BREAKING**: Removed bundled viafoundry-sdk folder
  - SDK now installed automatically from PyPI
  - Cleaner package structure
  - Easier maintenance and updates

- **IMPROVED**: Simplified IDE configuration
  - No longer need to pass credentials in IDE config
  - Credentials automatically loaded from .env file
  - Multiple .env file locations supported:
    1. `~/.config/viafoundry-mcp/.env` (recommended)
    2. `mcp_server/.env` (development)
    3. `~/.viafoundry-mcp.env` (legacy)

### Fixed
- Environment variable loading from various working directories
- Package installation compatibility issues

### Security
- Credentials stored only in .env files (git-ignored)
- No credentials in configuration files
- Bearer token authentication with ViaFoundry

## Installation

### From GitHub
```bash
pip install git+https://github.com/viascientific/viafoundry-mcp.git
```

### From PyPI (Coming Soon)
```bash
pip install viafoundry-mcp
```

### Development
```bash
git clone https://github.com/viascientific/viafoundry-mcp.git
cd viafoundry-mcp
pip install -e .
```

## Migration Guide

### Upgrading from Pre-release

If you had the bundled SDK version:

1. **Remove old installation**:
   ```bash
   pip uninstall viafoundry-mcp viafoundry_sdk
   ```

2. **Install new version**:
   ```bash
   pip install git+https://github.com/viascientific/viafoundry-mcp.git
   ```

3. **Update IDE configuration** (simplified):
   ```json
   {
     "viafoundry": {
       "command": "viafoundry-mcp"
     }
   }
   ```

4. **Move credentials** (optional, for cleaner setup):
   ```bash
   mkdir -p ~/.config/viafoundry-mcp
   mv mcp_server/.env ~/.config/viafoundry-mcp/.env
   ```

## What Changed?

### Before (Complex Configuration)
```json
{
  "viafoundry": {
    "command": "viafoundry-mcp",
    "env": {
      "VIAFOUNDRY_HOSTNAME": "https://...",
      "VIAFOUNDRY_USERNAME": "...",
      "VIAFOUNDRY_PASSWORD": "..."
    }
  }
}
```

### After (Simple Configuration)
```json
{
  "viafoundry": {
    "command": "viafoundry-mcp"
  }
}
```

Credentials are now automatically loaded from your .env file!
