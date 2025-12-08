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

**Option 1: Interactive Setup (Easiest)**

Run the built-in setup command:

```bash
viafoundry-mcp-setup
```

This will:
- Prompt you for your ViaFoundry hostname, username, and password
- Save credentials to `~/.config/viafoundry-mcp/.env`
- Set proper file permissions automatically

**Option 2: Manual Setup**

Create a `.env` file manually:

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
- "Create a new process based on an existing one"

### Launch Apps
- "What apps are available?"
- "Launch CellxGene"
- "Run the JupyterLab app"
- "Show me all available applications"
- "Start RStudio in cluster mode"

### Search Data
- "Find datasets related to 'human genome'"
- "Search for collections about cancer studies"
- "Show me details about collection 15"
- "Search for canvas visualizations about RNA-Seq"
- "What metadata fields does this collection have?"

### Manage Metadata
- "Search for metadata records matching 'cancer'"
- "Show me metadata record 456"
- "What metadata fields are defined in the system?"
- "Create a new metadata record for my experiment"

---

## Available Tools (40 Total)

### 📊 Report Management (8 tools)

Access and manage ViaFoundry reports and files.

| Tool | What It Does |
|------|-------------|
| `fetch_report` | Get complete report data with metadata |
| `list_processes` | List all processes that generated output in a report |
| `list_files` | List files (all or by specific process) |
| `download_file` | Download files from reports to local machine |
| `load_file` | View file contents directly (tabular data formatted) |
| `upload_file` | Upload files to reports |
| `get_report_dirs` | Get available directories for file uploads |
| `get_all_report_paths` | Get all accessible file paths in a report |

---

### 🔬 Process & Pipeline Management (10 tools)

Create, explore, and manage bioinformatics pipelines.

| Tool | What It Does |
|------|-------------|
| `list_all_processes` | List all processes/pipelines in ViaFoundry |
| `get_process_details` | Get detailed pipeline configuration and scripts |
| `get_process_revisions` | Get version history for a pipeline |
| `duplicate_process` | Clone an existing pipeline for modification |
| `create_process` | Create a new custom process/pipeline |
| `create_process_config` | Generate process configuration helper |
| `list_process_parameters` | List all available parameter definitions |
| `get_pipeline_parameters` | Get parameters for a specific pipeline |
| `create_process_parameter` | Create new parameter definition |
| `filter_process_parameters` | Filter parameters by name, type, or qualifier |

---

### 📂 Menu Group Management (3 tools)

Organize processes into logical groups.

| Tool | What It Does |
|------|-------------|
| `create_menu_group` | Create new menu group for organizing processes |
| `list_menu_groups` | List all available menu groups |
| `get_menu_group_by_name` | Find menu group ID by name |

---

### 🚀 App Management & Launch (3 tools)

Discover and launch applications in ViaFoundry.

| Tool | What It Does |
|------|-------------|
| `list_apps` | List all available applications with names, IDs, and details |
| `launch_app` | Launch/run an application with specified parameters |
| `discover_app_endpoints` | Discover available API endpoints (advanced/debugging) |

---

### 🗂️ Dataset & Collection Management (6 tools)

Search and organize datasets and collections.

| Tool | What It Does |
|------|-------------|
| `search_datasets` | Search for dataset files by name or criteria |
| `search_collections` | Search for dataset collections |
| `get_collection_details` | Get detailed collection information |
| `get_collection_fields` | Get metadata schema for a collection |
| `create_collection` | Create new dataset collection |
| `add_files_to_dataset` | Add files to existing dataset |

---

### 🎨 Canvas & Visualization Management (4 tools)

Create and explore data visualizations and dashboards.

| Tool | What It Does |
|------|-------------|
| `search_canvas` | Search for canvas visualizations/dashboards |
| `get_canvas_details` | Get detailed canvas configuration |
| `get_canvas_fields` | Get fields/schema for a canvas |
| `create_canvas` | Create new canvas visualization |

---

### 📋 Metadata Field Management (3 tools)

Define and explore metadata field schemas.

| Tool | What It Does |
|------|-------------|
| `search_metadata_fields` | Search metadata field definitions |
| `get_field_details` | Get detailed field information |
| `create_metadata_field` | Create new metadata field definition |

---

### 📝 Metadata Record Management (3 tools)

Manage metadata data records.

| Tool | What It Does |
|------|-------------|
| `search_metadata_records` | Search for metadata data records |
| `get_metadata_record` | Get specific metadata record by ID |
| `create_metadata_record` | Create new metadata data record |

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

You: "Load the first CSV file and show me the first 5 rows"

AI: Here are the first 5 rows of the VDJ B cell annotations:
    [Table with barcode, contig_id, chain, v_gene, d_gene, j_gene columns...]
```

---

### Launching Applications
```
You: "What apps are available in ViaFoundry?"

AI: Let me list the available applications for you.
    [Uses list_apps()]

    Available applications:
    1. CellxGene (ID: 42) - Interactive single-cell data visualization
    2. JupyterLab (ID: 23) - Interactive notebook environment
    3. RStudio (ID: 15) - R statistical computing environment
    4. IGV (ID: 8) - Integrative Genomics Viewer
    ...

You: "Launch CellxGene for me"

AI: I'll launch CellxGene for you.
    [Uses list_apps(search="CellxGene") to find the app]
    Found: CellxGene (ID: 42)

    [Uses launch_app(app_id="42", run_type="standalone")]

    ✓ CellxGene has been launched successfully!
    You can access it once it's ready.

You: "Can you start RStudio in cluster mode?"

AI: Sure! Let me launch RStudio on the cluster.
    [Uses list_apps(search="RStudio")]
    Found: RStudio (ID: 15)

    [Uses launch_app(app_id="15", run_type="cluster")]

    ✓ RStudio launched in cluster mode!
```

---

### Creating Custom Pipelines
```
You: "I need to create a new process that runs STAR alignment. Can you help?"

AI: I'll help you create a STAR alignment process. First, let me get a list
    of available menu groups so we can organize it properly.

    [Lists menu groups]

You: "Put it in the RNA-Seq menu group"

AI: Perfect. Now, what input parameters do you need? Typically STAR needs:
    - FASTQ files (input)
    - Genome index directory (input)
    - Output directory (output)

You: "Yes, that's right. Also add a threads parameter"

AI: I'll create the process configuration with those parameters...
    [Creates and shows configuration]

    Would you like me to create this process now?
```

---

### Exploring Research Data
```
You: "Find all datasets related to breast cancer from 2024"

AI: Found 8 datasets matching 'breast cancer' from 2024:
    1. breast_cancer_rnaseq_cohort1_2024.bam
    2. breast_cancer_chipseq_h3k27ac_2024.bed
    3. BRCA_patient_samples_2024.fastq
    ...

You: "Show me the collection details for the RNA-seq dataset"

AI: Collection: Breast Cancer RNA-Seq Cohort 1 (2024)
    - ID: 1245
    - 120 patient samples
    - Created: 2024-03-15
    - Owner: oncology_research_team
    - Description: Comprehensive transcriptome analysis of breast cancer patients
    - Fields: patient_id, tissue_type, tumor_stage, collection_date, treatment_status

You: "What metadata fields are available for this collection?"

AI: The collection has 12 metadata fields:
    [Lists fields with types and descriptions]
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

# View (sanitized) to verify format
cat ~/.config/viafoundry-mcp/.env | sed 's/PASSWORD=.*/PASSWORD=***/'
```

---

### ❌ ".env file not found"

The server checks these locations **in order**:
1. `~/.config/viafoundry-mcp/.env` ← **Recommended**
2. `./mcp_server/.env` (development)
3. Current directory `.env`
4. Parent directory `mcp_server/.env`
5. `~/.viafoundry-mcp.env` (legacy)

**Solution 1: Use the setup command (easiest)**
```bash
viafoundry-mcp-setup
```

**Solution 2: Create manually**
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
- 🛡️ **Read and create operations** - Safe, non-destructive operations only

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
