# ViaFoundry MCP Server

Connect your AI assistant to ViaFoundry's bioinformatics workflows and data. Use natural language in Cursor, Claude Desktop, VSCode, and other MCP-compatible tools to interact with reports, pipelines, datasets, and more.

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

---

## Why uvx?

This MCP server uses **uvx** (part of the `uv` package manager) for installation. Benefits:

- ✅ **No Python environment conflicts** - runs in isolated environment
- ✅ **No path hunting** - works regardless of your Python setup (pyenv, virtualenv, system Python)
- ✅ **Always up-to-date** - fetches latest version from GitHub
- ✅ **Cross-platform** - same config works on macOS, Linux, Windows

---

## Installation

### Quick Start (Recommended)

**Install uv** (includes uvx):
```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Or with pip
pip install uv
```

That's it! You don't need to install the MCP server separately - `uvx` will handle it automatically when your IDE starts.

---

### Alternative: Traditional Installation

If you prefer pip/uv install (not needed if using uvx):

```bash
# From GitHub
pip install git+https://github.com/viascientific/viafoundry-mcp.git

# Or with uv
uv pip install git+https://github.com/viascientific/viafoundry-mcp.git

# From PyPI (coming soon)
pip install viafoundry-mcp

# For development (editable install)
git clone https://github.com/viascientific/viafoundry-mcp.git
cd viafoundry-mcp
pip install -e .
```

After installation, you'll need to find the binary path and configure your IDE manually. See the **"🔧 Using Local Installation"** section in Troubleshooting below for detailed instructions.

---

### Configure Your Credentials

Create a `.env` file with your ViaFoundry credentials:

```bash
# Create config directory (recommended location)
mkdir -p ~/.config/viafoundry-mcp

# Create .env file
cat > ~/.config/viafoundry-mcp/.env << EOF
VIAFOUNDRY_HOSTNAME=https://your-viafoundry-instance.com
VIAFOUNDRY_USERNAME=your-username
VIAFOUNDRY_PASSWORD=your-password
EOF
```

**Alternative locations** (if you prefer):
- Development: `./mcp_server/.env` (copy from `.env.example`)
- Legacy: `~/.viafoundry-mcp.env`

---

## IDE Setup

### Cursor

**Best for:** Built-in MCP support, easiest setup

1. **Open Cursor Settings**
   - Go to **Settings** → **Features** → **MCP**
   - Or use keyboard shortcut: `Cmd+,` (Mac) / `Ctrl+,` (Windows)

2. **Add ViaFoundry Server**

   **Option A: From GitHub (current)**
   ```json
   {
     "viafoundry": {
       "command": "uvx",
       "args": ["--from", "git+https://github.com/viascientific/viafoundry-mcp.git", "viafoundry-mcp"]
     }
   }
   ```

   **Option B: From PyPI (coming soon - simpler!)**
   ```json
   {
     "viafoundry": {
       "command": "uvx",
       "args": ["viafoundry-mcp"]
     }
   }
   ```

   That's it! No path hunting, no Python environment issues.

3. **Restart Cursor**
   - Close and reopen Cursor completely

4. **Verify It Works**
   - Open any chat
   - Type: "List all processes in report 3461"
   - Your AI should use ViaFoundry tools to respond!

---

### Claude Desktop (Claude Code)

**Best for:** Standalone Claude desktop app with MCP support

1. **Locate Config File**

   Find your Claude Desktop config file:
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
   - **Linux**: `~/.config/Claude/claude_desktop_config.json`

2. **Edit Configuration**

   Open the file and add:

   **Option A: From GitHub (current)**
   ```json
   {
     "mcpServers": {
       "viafoundry": {
         "command": "uvx",
         "args": ["--from", "git+https://github.com/viascientific/viafoundry-mcp.git", "viafoundry-mcp"]
       }
     }
   }
   ```

   **Option B: From PyPI (coming soon - simpler!)**
   ```json
   {
     "mcpServers": {
       "viafoundry": {
         "command": "uvx",
         "args": ["viafoundry-mcp"]
       }
     }
   }
   ```

   If you already have other servers, add ViaFoundry to the `mcpServers` object using either option above.

3. **Restart Claude Desktop**
   - Quit Claude Desktop completely (Cmd+Q on Mac)
   - Reopen the application

4. **Verify It Works**
   - Look for a 🔌 icon or MCP indicator in the UI
   - Ask: "What reports can you access from ViaFoundry?"

---

### VSCode (with Continue Extension)

**Best for:** VSCode users with Continue AI assistant

1. **Install Continue Extension**
   - Open VSCode Extensions (Cmd+Shift+X / Ctrl+Shift+X)
   - Search for "Continue"
   - Install the Continue extension

2. **Configure Continue**

   Open Continue config file:
   - Click the Continue icon in the left sidebar
   - Click settings gear ⚙️
   - Or open: `~/.continue/config.json`

3. **Add ViaFoundry Server**

   **Option A: From GitHub (current)**
   ```json
   {
     "mcpServers": [
       {
         "name": "viafoundry",
         "command": "uvx",
         "args": ["--from", "git+https://github.com/viascientific/viafoundry-mcp.git", "viafoundry-mcp"]
       }
     ]
   }
   ```

   **Option B: From PyPI (coming soon - simpler!)**
   ```json
   {
     "mcpServers": [
       {
         "name": "viafoundry",
         "command": "uvx",
         "args": ["viafoundry-mcp"]
       }
     ]
   }
   ```

   If you have other settings, merge with your existing config.

4. **Restart VSCode**
   - Reload the window: `Cmd+Shift+P` → "Developer: Reload Window"

5. **Verify It Works**
   - Open Continue chat
   - Ask: "List available ViaFoundry pipelines"

---

### Other MCP-Compatible Tools

ViaFoundry MCP works with any tool supporting the Model Context Protocol:

**Cline, Windsurf, Zed, etc:**

**Option A: From GitHub (current)**
```json
{
  "mcpServers": {
    "viafoundry": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/viascientific/viafoundry-mcp.git", "viafoundry-mcp"]
    }
  }
}
```

**Option B: From PyPI (coming soon - simpler!)**
```json
{
  "mcpServers": {
    "viafoundry": {
      "command": "uvx",
      "args": ["viafoundry-mcp"]
    }
  }
}
```

Check your tool's documentation for the config file location.

---

## What You Can Do

Once configured, ask your AI assistant to:

### Work with Reports
- "List all processes in report 3461"
- "Show me files from the cellranger_multi process"
- "Download the web summary HTML file"
- "Load the gene expression counts file and show me the first 10 rows"
- "Upload my analysis results to report 3461"

### Explore Pipelines
- "What pipelines are available in ViaFoundry?"
- "Show me details about the RNA-Seq pipeline"
- "What parameters does process 42 need?"
- "Show me the revision history for pipeline 123"
- "List all available parameters in the system"

### Search Data
- "Find datasets related to 'human genome'"
- "Search for collections about cancer studies"
- "Show me details about collection 15"
- "Search for canvas visualizations about RNA-Seq"
- "What metadata fields does this collection have?"

### Explore Metadata
- "Search for metadata records matching 'cancer'"
- "Show me metadata record 456"
- "What metadata fields are defined in the system?"

---

## Available Tools (22 Total)

### 📊 Report Management (8 tools)
| Tool | What It Does |
|------|-------------|
| `fetch_report` | Get complete report data with metadata |
| `list_processes` | List all processes in a report |
| `list_files` | List files (all or by process) |
| `download_file` | Download files from reports |
| `load_file` | View file contents directly in chat |
| `upload_file` | Upload files to reports |
| `get_report_dirs` | Get available upload directories |
| `get_all_report_paths` | Get all file paths in a report |

### 🔬 Process/Pipeline Management (5 tools)
| Tool | What It Does |
|------|-------------|
| `list_all_processes` | List all ViaFoundry pipelines |
| `get_process_details` | Get detailed pipeline information |
| `get_process_revisions` | Get version history for a pipeline |
| `list_process_parameters` | List all available parameters |
| `get_pipeline_parameters` | Get parameters for a specific pipeline |

### 🎨 Canvas & Visualizations (2 tools)
| Tool | What It Does |
|------|-------------|
| `search_canvas` | Search for visualizations/dashboards |
| `get_canvas_details` | Get detailed canvas information |

### 🗂️ Metadata Collections & Datasets (3 tools)
| Tool | What It Does |
|------|-------------|
| `search_datasets` | Search for dataset files |
| `search_collections` | Search for dataset collections |
| `get_collection_details` | Get collection details |

### 📋 Metadata Fields & Schema (2 tools)
| Tool | What It Does |
|------|-------------|
| `search_metadata_fields` | Search metadata field definitions |
| `get_collection_fields` | Get fields for a collection |

### 📝 Metadata Records (2 tools)
| Tool | What It Does |
|------|-------------|
| `search_metadata_records` | Search metadata data records |
| `get_metadata_record` | Get specific metadata record |

---

## Example Conversations

### Analyzing Report Data
```
You: "List all processes in report 3461"

AI: I found 2 processes in report 3461:
    1. cellranger_multi
    2. scRNA_Analysis_Module

You: "Show me the files in cellranger_multi"

AI: Found 3 files:
    - all_all_web_summary.html (6.09 MB)
    - all_all_vdj_b_filtered_contig_annotations.csv (0.90 MB)
    - all_all_vdj_t_filtered_contig_annotations.csv (4.52 MB)

You: "Download the web summary report"

AI: ✓ Downloaded to: /Users/you/Downloads/all_all_web_summary.html
```

### Exploring Pipelines
```
You: "What bioinformatics pipelines are available?"

AI: Found 25 processes including:
    - RNA-Seq Analysis
    - ChIP-Seq Pipeline
    - ATAC-Seq Workflow
    - Single Cell RNA-Seq
    - Variant Calling Pipeline
    ...

You: "Tell me about the RNA-Seq pipeline"

AI: RNA-Seq Analysis (Process ID: 42)
    - Type: Nextflow workflow
    - Parameters: genome, fastq_files, output_dir, quality_threshold
    - Owner: admin
    - Created: 2025-01-15
```

### Finding Research Data
```
You: "Find datasets with 'cancer' in the name"

AI: Found 12 datasets:
    - breast_cancer_rnaseq_2025.bam
    - lung_cancer_chipseq.bed
    - prostate_cancer_samples.fastq
    ...

You: "Show me the breast cancer collection"

AI: Collection: Breast Cancer RNA-Seq Study
    - 120 samples
    - Created: 2025-01-15
    - Owner: research_team
    - Description: Comprehensive breast cancer transcriptome analysis
```

---

## Troubleshooting

### ❌ "Command not found: uvx"

**Solution:**
```bash
# Install uv (includes uvx)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with pip
pip install uv

# Verify installation
uvx --version
```

---

### ❌ "Authentication failed"

**Checklist:**
- ✓ `.env` file exists in one of the supported locations
- ✓ `VIAFOUNDRY_HOSTNAME` includes `https://`
- ✓ Username and password are correct
- ✓ You can log in to ViaFoundry web interface

**Debug:**
```bash
# Check if .env file exists
ls -la ~/.config/viafoundry-mcp/.env

# Test connection (if you have the SDK)
python -c "from viafoundry.client import ViaFoundryClient; print('✓ SDK imports OK')"
```

---

### ❌ ".env file not found"

The server checks these locations **in order**:
1. `~/.config/viafoundry-mcp/.env` ← **Recommended**
2. `./mcp_server/.env` (development)
3. Current directory `.env`
4. Parent directory `mcp_server/.env`
5. `~/.viafoundry-mcp.env` (legacy)

**Create in recommended location:**
```bash
mkdir -p ~/.config/viafoundry-mcp
cat > ~/.config/viafoundry-mcp/.env << EOF
VIAFOUNDRY_HOSTNAME=https://your-instance.com
VIAFOUNDRY_USERNAME=your-username
VIAFOUNDRY_PASSWORD=your-password
EOF
```

---

### ❌ "No tools showing up in IDE"

**Solutions:**
1. **Verify uv is installed:** `uvx --version`
2. **Check configuration** matches the examples above exactly
3. **Restart your IDE completely** (quit and reopen, not just reload)
4. **Check IDE logs** for errors (usually in settings/debug panel)
5. **Test the server manually:**
   ```bash
   uvx --from git+https://github.com/viascientific/viafoundry-mcp.git viafoundry-mcp
   ```

---

### 🔧 Using Local Installation (Alternative to uvx)

If you installed with pip/uv and want to use the local installation instead of uvx:

**Step 1: Find the correct binary path**

```bash
# Method 1: Using Python (most reliable)
python -c "import sys; print(sys.executable.replace('python', 'viafoundry-mcp'))"

# Method 2: For pyenv users - find the actual binary (not the shim!)
ls ~/.pyenv/versions/*/bin/viafoundry-mcp

# Method 3: Check all locations
which -a viafoundry-mcp
```

**Step 2: Use the full path in your IDE config**

**Cursor:**
```json
{
  "viafoundry": {
    "command": "/Users/yourusername/.pyenv/versions/3.12.2/bin/viafoundry-mcp"
  }
}
```

**Claude Desktop / VSCode Continue:**
```json
{
  "mcpServers": {
    "viafoundry": {
      "command": "/Users/yourusername/.pyenv/versions/3.12.2/bin/viafoundry-mcp"
    }
  }
}
```

**⚠️ Important for pyenv users:**
- **DO NOT use** `~/.pyenv/shims/viafoundry-mcp` - shims are wrapper scripts that don't work in IDEs
- **DO use** the actual binary path: `~/.pyenv/versions/X.X.X/bin/viafoundry-mcp`
- Replace `X.X.X` with your Python version (e.g., `3.12.2`)

**Step 3: Restart your IDE**

---

### ❌ "Tools work but returns errors"

**Common issues:**
- Wrong report ID format (should be a number like `3461`)
- File path doesn't exist in report
- Insufficient permissions on ViaFoundry
- Network issues connecting to ViaFoundry instance

**Check credentials have access:**
- Log in to ViaFoundry web UI
- Verify you can see the reports/data you're querying

---

## Development & Contributing

### Run Tests
```bash
# Clone repository
git clone https://github.com/viascientific/viafoundry-mcp.git
cd viafoundry-mcp

# Install with dev dependencies
pip install -e ".[dev]"

# Run manual test
python test_mcp_manual.py
```

### Project Structure
```
viafoundry-mcp/
├── src/
│   └── viafoundry_mcp/
│       ├── server.py          # Main MCP server implementation
│       ├── config.py          # Configuration management
│       └── __init__.py
├── pyproject.toml             # Package configuration
├── setup.py                   # Setup script
├── LICENSE                    # Apache 2.0 License
├── README.md                  # This file
├── CHANGELOG.md               # Version history
└── test_mcp_manual.py         # Test client
```

### Contributing
We welcome contributions! Please:
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes with tests
4. Submit a pull request

---

## Security & Privacy

- 🔐 **Credentials** stored locally in `.env` files (never committed to git)
- 🔑 **Bearer token** authentication with ViaFoundry
- ♻️ **Auto token refresh** for long-running sessions
- ✅ **Path validation** for all file operations
- 🚫 **No telemetry** - your data stays between you and ViaFoundry

---

## Technical Details

**Package:** `viafoundry-mcp`
**Version:** 1.0.1
**Python:** 3.9+
**Protocol:** MCP (Model Context Protocol)
**Transport:** stdio (local), HTTP/SSE (hosted - coming soon)

**Dependencies:**
- `mcp>=1.0.0` - MCP SDK
- `viafoundry_sdk>=1.0.0` - ViaFoundry API client
- `python-dotenv>=1.0.0` - Environment management

---

## Resources

- 📖 **Documentation**: [GitHub README](https://github.com/viascientific/viafoundry-mcp)
- 🐛 **Issues**: [GitHub Issues](https://github.com/viascientific/viafoundry-mcp/issues)
- 📦 **ViaFoundry SDK**: [PyPI Package](https://pypi.org/project/viafoundry_sdk/)
- 🌐 **MCP Protocol**: [modelcontextprotocol.io](https://modelcontextprotocol.io/)
- 📋 **Changelog**: [CHANGELOG.md](CHANGELOG.md)

---

## License

Apache License 2.0 - see [LICENSE](LICENSE) file for details.

---

**Built with ❤️ for the ViaFoundry community**
