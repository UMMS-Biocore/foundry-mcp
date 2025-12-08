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
      "url": "http://127.0.0.1:8000/mcp",
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

## Getting Your Token

1. Log in to your ViaFoundry instance
2. Go to **Profile** → **Personal Access Tokens**
3. Create a new token and copy it

> **Note:** MCP tokens must start with the `via_mcp_` prefix. If your token doesn't have this prefix, be sure to generate a new MCP token from ViaFoundry.

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

### Custom Port

```bash
PORT=9000 docker compose up
```

### Client Config Locations

| Client | Config File |
|--------|-------------|
| Cursor | `~/.cursor/mcp.json` |
| Claude Desktop (Mac) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| VSCode Continue | `~/.continue/config.json` |

---

## Cloud Deployment (HTTPS)

### Google Cloud Run

```bash
gcloud run deploy viafoundry-mcp --source . --port 8000
```

### Fly.io

```bash
fly launch
```

Then update your client config with the HTTPS URL:

```json
{
  "viafoundry": {
    "url": "https://your-app.fly.dev/mcp",
    "headers": { ... }
  }
}
```

---

## Alternative: Local Installation (without Docker)

```bash
# Install
pip install git+https://github.com/viascientific/viafoundry-mcp.git

# Run HTTP server
viafoundry-mcp-http --port 8000
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
│   ├── server.py        # MCP server (stdio)
│   ├── http_server.py   # MCP server (HTTP)
│   ├── client.py        # ViaFoundry client
│   └── config.py        # Configuration
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
