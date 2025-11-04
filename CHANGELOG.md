# Changelog

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
