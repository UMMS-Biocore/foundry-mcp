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


---

## Available Tools

| Tool | Description |
|------|-------------|
| `fetch_report` | Get report data with metadata |
| `list_processes` | List processes in a report |
| `list_files` | List files (all or by process) |
| `download_file` | Download files from reports |
| `load_file` | View file contents in chat |
| `upload_file` | Upload files to reports |
| `get_report_dirs` | Get upload directories |
| `list_all_processes` | List all ViaFoundry pipelines |
| `get_process_details` | Get pipeline details |
| `search_datasets` | Search dataset files |
| `search_collections` | Search collections |
| `get_collection_details` | Get collection details |

---

## Example Usage

```
You: "List all processes in report 3461"
AI: Found 2 processes: cellranger_multi, scRNA_Analysis_Module

You: "Show files in cellranger_multi"
AI: Found 3 files: web_summary.html (6MB), vdj_b_annotations.csv...

You: "Load the gene expression file"
AI: [displays table with gene expression data]
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
