# ViaFoundry MCP Server

Connect your AI assistant to ViaFoundry's bioinformatics workflows and data. Use natural language in Cursor, Claude Desktop, VSCode, and other MCP-compatible tools to interact with reports, pipelines, datasets, and more.

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

---

## Installation

### Step 1: Install the Package

Choose one of these methods:

**Using pip (recommended):**
```bash
pip install git+https://github.com/viascientific/viafoundry-mcp.git
```

**Using uv:**
```bash
uv pip install git+https://github.com/viascientific/viafoundry-mcp.git
```

**For development:**
```bash
git clone https://github.com/viascientific/viafoundry-mcp.git
cd viafoundry-mcp
pip install -e .
```

### Step 2: Configure Your Credentials

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

   First, find the **actual path** to the `viafoundry-mcp` command:

   ```bash
   # Find the real path (not the shim)
   python -c "import sys; print(sys.executable.replace('python', 'viafoundry-mcp'))"
   ```

   Or manually check:
   ```bash
   ls ~/.pyenv/versions/*/bin/viafoundry-mcp
   # Or for system Python:
   which -a viafoundry-mcp
   ```

   Then add this configuration using the **full path**:
   ```json
   {
     "viafoundry": {
       "command": "/full/path/to/viafoundry-mcp"
     }
   }
   ```

   **Example with pyenv:**
   ```json
   {
     "viafoundry": {
       "command": "/Users/yourusername/.pyenv/versions/3.12.2/bin/viafoundry-mcp"
     }
   }
   ```

   **⚠️ Important:** Do NOT use pyenv shims (e.g., `~/.pyenv/shims/viafoundry-mcp`) as Cursor cannot execute them properly. Use the direct path to the binary.

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
   ```json
   {
     "mcpServers": {
       "viafoundry": {
         "command": "bash",
         "args": ["-l", "-c", "viafoundry-mcp"]
       }
     }
   }
   ```

   If you already have other servers, add ViaFoundry to the `mcpServers` object:
   ```json
   {
     "mcpServers": {
       "existing-server": {
         "command": "some-other-command"
       },
       "viafoundry": {
         "command": "bash",
         "args": ["-l", "-c", "viafoundry-mcp"]
       }
     }
   }
   ```

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

   Add to your config:
   ```json
   {
     "mcpServers": [
       {
         "name": "viafoundry",
         "command": "bash",
         "args": ["-l", "-c", "viafoundry-mcp"]
       }
     ]
   }
   ```

   If you have other settings, merge it:
   ```json
   {
     "models": [...],
     "mcpServers": [
       {
         "name": "viafoundry",
         "command": "bash",
         "args": ["-l", "-c", "viafoundry-mcp"]
       }
     ]
   }
   ```

4. **Restart VSCode**
   - Reload the window: `Cmd+Shift+P` → "Developer: Reload Window"

5. **Verify It Works**
   - Open Continue chat
   - Ask: "List available ViaFoundry pipelines"

---

### Other MCP-Compatible Tools

ViaFoundry MCP works with any tool supporting the Model Context Protocol:

**Cline, Windsurf, Zed, etc:**
```json
{
  "mcpServers": {
    "viafoundry": {
      "command": "bash",
      "args": ["-l", "-c", "viafoundry-mcp"]
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

### Search Data
- "Find datasets related to 'human genome'"
- "Search for collections about cancer studies"
- "Show me details about collection 15"

---

## Available Tools (12 Total)

### 📊 Report Management (7 tools)
| Tool | What It Does |
|------|-------------|
| `fetch_report` | Get complete report data with metadata |
| `list_processes` | List all processes in a report |
| `list_files` | List files (all or by process) |
| `download_file` | Download files from reports |
| `load_file` | View file contents directly in chat |
| `upload_file` | Upload files to reports |
| `get_report_dirs` | Get available upload directories |

### 🔬 Process/Pipeline Management (2 tools)
| Tool | What It Does |
|------|-------------|
| `list_all_processes` | List all ViaFoundry pipelines |
| `get_process_details` | Get detailed pipeline information |

### 🗂️ Metadata & Dataset Search (3 tools)
| Tool | What It Does |
|------|-------------|
| `search_datasets` | Search for dataset files |
| `search_collections` | Search for dataset collections |
| `get_collection_details` | Get collection details |

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

### ❌ "Command not found: viafoundry-mcp"

**Solution:**
```bash
# Verify installation
pip install git+https://github.com/viascientific/viafoundry-mcp.git

# Check if command exists
which viafoundry-mcp

# If not found, check your Python bin directory is in PATH
echo $PATH
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
1. **Restart your IDE completely** (not just reload)
2. **Check MCP server status** in IDE settings
3. **Verify command works:**
   ```bash
   viafoundry-mcp --help
   ```
4. **Check IDE logs** for errors (usually in settings/debug panel)

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
├── mcp_server/
│   ├── server.py          # Main MCP server implementation
│   ├── .env.example       # Credentials template
│   └── __init__.py
├── pyproject.toml         # Package configuration
├── setup.py               # Setup script
├── LICENSE                # Apache 2.0 License
├── README.md              # This file
├── CHANGELOG.md           # Version history
└── test_mcp_manual.py     # Test client
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
**Version:** 1.0.0
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
