# ViaFoundry MCP Server

Connect AI assistants to ViaFoundry bioinformatics workflows. Works with Cursor, Claude Desktop, and other MCP-compatible tools.

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

---

## Quick Start (Docker)

**1. Run the server:**

```bash
docker compose up --build -d
```

**2. Configure your AI client** (`~/.cursor/mcp.json` for Cursor):

```json
{
  "mcpServers": {
    "viafoundry": {
      "url": "http://127.0.0.1:8705/mcp",
      "headers": {
        "X-ViaFoundry-Hostname": "https://your-viafoundry-instance.com",
        "X-ViaFoundry-Token": "your-personal-access-token"
      }
    }
  }
}
```

**3. Restart your AI client** and start chatting!

---

## Getting Your Personal Access Token

To connect any AI assistant to ViaFoundry, you need a Personal Access Token (PAT) configured for MCP usage.

**1.** Log in to your ViaFoundry instance and click your **Profile** icon in the top-left corner. Select **Personal Access Tokens**.

**2.** Click **Create New Token**. Fill in the following:

- **Token Name** — A descriptive label (e.g., "Claude Code MCP", "Cursor MCP").
- **Expiration Date** — Choose when the token expires. Default is 30 days; maximum is 1 year.
- **Token Usage** — Select **MCP (Model Context Protocol)**. This generates a token with the required `via_mcp_` prefix for use with AI assistants.

**3.** Click **Create Token**. Your token will be displayed once — copy it immediately and store it securely. You will not be able to view it again.

**4.** After creation, ViaFoundry provides a ready-to-use MCP configuration snippet under **Usage Examples**. Use the **MCP** tab and select your client (Cursor or VS Code) from the dropdown to get a config block you can copy directly into your editor. For Claude Code and Claude Desktop, see the configuration sections below.

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

## Available Tools (41 Total)

### 📊 Report Management (8 tools)

Access and manage ViaFoundry reports and files.

| Tool                   | What It Does                                         |
| ---------------------- | ---------------------------------------------------- |
| `fetch_report`         | Get complete report data with metadata               |
| `list_processes`       | List all processes that generated output in a report |
| `list_files`           | List files (all or by specific process)              |
| `download_file`        | Download files from reports to local machine         |
| `load_file`            | View file contents directly (tabular data formatted) |
| `upload_file`          | Upload files to reports                              |
| `get_report_dirs`      | Get available directories for file uploads           |
| `get_all_report_paths` | Get all accessible file paths in a report            |

---

### 🏃 Run Management (2 tools)

Search and retrieve pipeline run information.

| Tool        | What It Does                                              |
| ----------- | --------------------------------------------------------- |
| `list_runs` | List and search for runs/pipeline executions              |
| `get_run`   | Get detailed run info by ID or name (supports fuzzy match)|

---

### 🔬 Process & Pipeline Management (9 tools)

Create, explore, and manage bioinformatics pipelines.

| Tool                        | What It Does                                        |
| --------------------------- | --------------------------------------------------- |
| `list_all_processes`        | List all processes/pipelines in ViaFoundry          |
| `get_process_details`       | Get detailed pipeline configuration and scripts     |
| `get_process_revisions`     | Get version history for a pipeline                  |
| `duplicate_process`         | Clone an existing pipeline for modification         |
| `create_process`            | Create a new custom process/pipeline                |
| `create_process_config`     | Generate process configuration helper               |
| `list_process_parameters`   | List all available parameter definitions            |
| `get_process_parameters`    | Get parameters filtered by name, type, or qualifier |
| `create_process_parameter`  | Create new parameter definition                     |

---

### 📂 Menu Group Management (3 tools)

Organize processes into logical groups.

| Tool                     | What It Does                                   |
| ------------------------ | ---------------------------------------------- |
| `create_menu_group`      | Create new menu group for organizing processes |
| `list_menu_groups`       | List all available menu groups                 |
| `get_menu_group_by_name` | Find menu group ID by name                     |

---

### 🚀 App Management & Launch (3 tools)

Discover and launch applications in ViaFoundry.

| Tool                     | What It Does                                                 |
| ------------------------ | ------------------------------------------------------------ |
| `list_apps`              | List all available applications with names, IDs, and details |
| `launch_app`             | Launch/run an application with specified parameters          |
| `discover_app_endpoints` | Discover available API endpoints (advanced/debugging)        |

---

### 🗂️ Dataset & Collection Management (6 tools)

Search and organize datasets and collections.

| Tool                     | What It Does                                 |
| ------------------------ | -------------------------------------------- |
| `search_datasets`        | Search for dataset files by name or criteria |
| `search_collections`     | Search for dataset collections               |
| `get_collection_details` | Get detailed collection information          |
| `get_collection_fields`  | Get metadata schema for a collection         |
| `create_collection`      | Create new dataset collection                |
| `add_files_to_dataset`   | Add files to existing dataset                |

---

### 🎨 Canvas & Visualization Management (4 tools)

Create and explore data visualizations and dashboards.

| Tool                 | What It Does                                |
| -------------------- | ------------------------------------------- |
| `search_canvas`      | Search for canvas visualizations/dashboards |
| `get_canvas_details` | Get detailed canvas configuration           |
| `get_canvas_fields`  | Get fields/schema for a canvas              |
| `create_canvas`      | Create new canvas visualization             |

---

### 📋 Metadata Field Management (3 tools)

Define and explore metadata field schemas.

| Tool                     | What It Does                         |
| ------------------------ | ------------------------------------ |
| `search_metadata_fields` | Search metadata field definitions    |
| `get_field_details`      | Get detailed field information       |
| `create_metadata_field`  | Create new metadata field definition |

---

### 📝 Metadata Record Management (3 tools)

Manage metadata data records.

| Tool                      | What It Does                       |
| ------------------------- | ---------------------------------- |
| `search_metadata_records` | Search for metadata data records   |
| `get_metadata_record`     | Get specific metadata record by ID |
| `create_metadata_record`  | Create new metadata data record    |

---

## Example Usage

```
You: "List all processes in report 3461"
AI: Found 2 processes: cellranger_multi, scRNA_Analysis_Module

You: "Show files in cellranger_multi"
AI: Found 3 files: web_summary.html (6MB), vdj_b_annotations.csv...

You: "Load the gene expression file"
AI: [displays table with gene expression data]

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

## Configuration

### Claude Code

Claude Code supports MCP servers at two levels — project-scoped (shared with your team) and global (available across all your projects).

**Option A: CLI command** — The fastest way to add the ViaFoundry MCP server:

```bash
# Add globally (available in all projects)
claude mcp add --transport http --scope user viafoundry \
  --header "X-ViaFoundry-Token: via_mcp_your-personal-access-token" \
  https://mcp.viafoundry.com/mcp

# Or add to the current project only
claude mcp add --transport http --scope project viafoundry \
  --header "X-ViaFoundry-Token: via_mcp_your-personal-access-token" \
  https://mcp.viafoundry.com/mcp
```

**Option B: Manual configuration** — Add the config JSON directly to the appropriate file:

*Project-level* — Create a `.mcp.json` file in your project root. This makes ViaFoundry tools available to anyone who opens this project in Claude Code:

```json
{
  "mcpServers": {
    "viafoundry": {
      "type": "http",
      "url": "https://mcp.viafoundry.com/mcp",
      "headers": {
        "X-ViaFoundry-Token": "via_mcp_your-personal-access-token"
      }
    }
  }
}
```

*Global* — Add the same configuration to `~/.claude.json` to make ViaFoundry tools available across all your projects:

```json
{
  "mcpServers": {
    "viafoundry": {
      "type": "http",
      "url": "https://mcp.viafoundry.com/mcp",
      "headers": {
        "X-ViaFoundry-Token": "via_mcp_your-personal-access-token"
      }
    }
  }
}
```

> **Note:** The MCP server URL depends on your ViaFoundry deployment. For GCP-hosted instances, use `https://mcp.gcp.viafoundry.com/mcp` instead. Check with your ViaFoundry administrator for the correct URL.

### Claude Desktop

Edit the Claude Desktop configuration file:

- **Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "viafoundry": {
      "url": "https://mcp.viafoundry.com/mcp",
      "headers": {
        "X-ViaFoundry-Token": "via_mcp_your-personal-access-token"
      }
    }
  }
}
```

Restart Claude Desktop after saving for the changes to take effect.

### Cursor / VS Code

ViaFoundry generates a ready-to-use config snippet when you create your token — select **Cursor** or **VS Code** from the dropdown and click **Copy Code**. Paste it into:

- **Cursor:** `~/.cursor/mcp.json`
- **VS Code:** Your MCP extension config file

### Custom Port

```bash
PORT=9000 docker compose up
```

### All Client Config Locations

| Client                   | Config File                                                       |
| ------------------------ | ----------------------------------------------------------------- |
| Claude Code (project)    | `.mcp.json` (project root)                                        |
| Claude Code (global)     | `~/.claude.json`                                                   |
| Claude Desktop (Mac)     | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json`                     |
| Cursor                   | `~/.cursor/mcp.json`                                              |
| VSCode Continue          | `~/.continue/config.json`                                         |

---

## Security

The MCP server has two security modes to prevent misuse as an open proxy:

### Open Mode (Development/Localhost)

In open mode, clients can specify any ViaFoundry instance via the `X-ViaFoundry-Hostname` header. This is the default when:

- Running standalone with `docker compose up` in the `mcp/` directory
- `FRONTEND_HOSTNAME` environment variable is not set
- `FRONTEND_HOSTNAME` is a localhost address (`localhost`, `127.0.0.1`, `0.0.0.0`, etc.)

**Client configuration (open mode):**

```json
{
  "mcpServers": {
    "viafoundry": {
      "url": "http://127.0.0.1:8705/mcp",
      "headers": {
        "X-ViaFoundry-Hostname": "https://your-viafoundry.com",
        "X-ViaFoundry-Token": "via_mcp_your-token"
      }
    }
  }
}
```

### Fixed Hostname Mode (Production)

In production deployments, the server locks to a specific ViaFoundry instance, ignoring client-provided `X-ViaFoundry-Hostname` headers. This prevents the server from being used as an open proxy.

**Enabled when** `FRONTEND_HOSTNAME` is set to a non-localhost value (typically from ViaFoundry's `.env` file):

```bash
FRONTEND_PROTOCOL=https
FRONTEND_HOSTNAME="prod.viafoundry.com"
FRONTEND_PATH_PREFIX="/beta"
```

This constructs the fixed hostname: `https://prod.viafoundry.com/beta`

**Client configuration (fixed hostname mode):**

```json
{
  "mcpServers": {
    "viafoundry": {
      "url": "https://your-mcp-server.com/mcp",
      "headers": {
        "X-ViaFoundry-Token": "via_mcp_your-token"
      }
    }
  }
}
```

> **Note:** In fixed hostname mode, clients only need to provide `X-ViaFoundry-Token`. The `X-ViaFoundry-Hostname` header is ignored.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `FRONTEND_HOSTNAME` | Target ViaFoundry hostname. If set (non-localhost), enables fixed hostname mode | None (open mode) |
| `FRONTEND_PROTOCOL` | Protocol for fixed hostname | `https` |
| `FRONTEND_PATH_PREFIX` | Path prefix for fixed hostname (e.g., `/beta`) | None |

---

## Cloud Deployment (HTTPS)

> **Security Note:** When deploying to cloud platforms, use fixed hostname mode to prevent the MCP server from being used as an open proxy. Set `FRONTEND_HOSTNAME` to your ViaFoundry instance.

### Google Cloud Run

```bash
gcloud run deploy viafoundry-mcp \
  --source . \
  --port 8705 \
  --set-env-vars="FRONTEND_HOSTNAME=your-viafoundry.com,FRONTEND_PROTOCOL=https"
```

### Fly.io

```bash
fly launch
fly secrets set FRONTEND_HOSTNAME=your-viafoundry.com FRONTEND_PROTOCOL=https
```

Then update your client config with the HTTPS URL:

```json
{
  "viafoundry": {
    "url": "https://your-app.fly.dev/mcp",
    "headers": {
      "X-ViaFoundry-Token": "via_mcp_your-token"
    }
  }
}
```

---

## Alternative: Local Installation (without Docker)

```bash
# Install
pip install git+https://github.com/viascientific/viafoundry-mcp.git

# Run server
viafoundry-mcp --port 8705
```

---

## Development

```bash
git clone https://github.com/viascientific/viafoundry-mcp.git
cd viafoundry-mcp
pip install -e ".[dev]"
```

### Project Structure

```
viafoundry-mcp/
├── src/viafoundry_mcp/
│   ├── server.py        # MCP HTTP server
│   ├── client.py        # ViaFoundry client
│   ├── config.py        # Configuration
│   └── utils.py         # Utility functions
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Troubleshooting

**Server not responding?**

- Check container is running: `docker ps`
- Check logs: `docker logs viafoundry-mcp`

**Authentication failed?**

- Verify your token is valid in ViaFoundry web UI
- Check `X-ViaFoundry-Hostname` includes `https://`

**Tools not showing in IDE?**

- Restart IDE completely (quit and reopen)
- Verify mcp.json syntax is valid JSON

---

## License

Apache 2.0 - see [LICENSE](LICENSE)
