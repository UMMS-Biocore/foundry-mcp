# ViaFoundry MCP Server - Hosted Deployment Plan

This document outlines how to deploy ViaFoundry MCP as a hosted web service, similar to Seqera's approach (https://mcp.seqera.io/mcp).

## Current vs. Hosted Architecture

### Current: Local Installation (stdio)
```
User's IDE → viafoundry-mcp command → stdin/stdout → ViaFoundry API
```

### Target: Hosted Service (HTTP/SSE)
```
User's IDE → HTTPS → mcp.viafoundry.com → ViaFoundry API
              ↓
         OAuth/API Key
```

---

## Implementation Plan

### Phase 1: Add HTTP/SSE Transport (Week 1)

#### 1.1 Create Web Server Module

**File: `mcp_server/web_server.py`**

```python
#!/usr/bin/env python3
"""
ViaFoundry MCP Web Server

HTTP/SSE endpoint for hosted deployment.
"""

import os
import logging
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import Response, JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from mcp.server.sse import sse_server
import uvicorn

from .server import app as mcp_app, load_env_file

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('viafoundry-mcp-web')

# Load environment
load_env_file()


async def health_check(request):
    """Health check endpoint."""
    return JSONResponse({
        "status": "healthy",
        "service": "viafoundry-mcp",
        "version": "0.1.0"
    })


async def mcp_endpoint(request):
    """Main MCP endpoint using SSE transport."""

    # Validate authentication
    auth_header = request.headers.get("authorization", "")
    if not validate_auth(auth_header):
        return Response("Unauthorized", status_code=401)

    # Handle MCP over SSE
    async with sse_server() as (read, write):
        await mcp_app.run(
            read,
            write,
            mcp_app.create_initialization_options()
        )


def validate_auth(auth_header: str) -> bool:
    """Validate API key or OAuth token."""

    # Option 1: Simple API key validation
    expected_key = os.getenv("MCP_API_KEY")
    if expected_key and auth_header == f"Bearer {expected_key}":
        return True

    # Option 2: OAuth token validation (implement later)
    # token = auth_header.replace("Bearer ", "")
    # return validate_oauth_token(token)

    return False


# Create Starlette app
web_app = Starlette(
    debug=False,
    routes=[
        Route("/", health_check),
        Route("/health", health_check),
        Route("/mcp", mcp_endpoint, methods=["GET", "POST"]),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
    ]
)


def main():
    """Run the web server."""
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")

    logger.info(f"Starting ViaFoundry MCP Web Server on {host}:{port}")

    uvicorn.run(
        web_app,
        host=host,
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
```

#### 1.2 Update pyproject.toml

Add new entry point and dependencies:

```toml
[project.scripts]
viafoundry-mcp = "mcp_server.server:main"
viafoundry-mcp-web = "mcp_server.web_server:main"  # NEW

[project]
dependencies = [
    "mcp>=1.0.0",
    "python-dotenv>=1.0.0",
    "viafoundry_sdk>=1.0.0",
    "uvicorn>=0.23.1",         # NEW
    "starlette>=0.27.0",        # NEW
]

[project.optional-dependencies]
web = [
    "gunicorn>=21.0.0",
    "python-multipart>=0.0.9",
]
```

---

### Phase 2: Containerization (Week 2)

#### 2.1 Create Dockerfile

**File: `Dockerfile`**

```dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy package files
COPY pyproject.toml setup.py MANIFEST.in ./
COPY mcp_server/ ./mcp_server/
COPY LICENSE README.md ./

# Install package
RUN pip install --no-cache-dir -e ".[web]"

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/health')"

# Run web server
CMD ["viafoundry-mcp-web"]
```

#### 2.2 Create docker-compose.yml

**File: `docker-compose.yml`**

```yaml
version: '3.8'

services:
  viafoundry-mcp:
    build: .
    image: viafoundry-mcp:latest
    ports:
      - "8000:8000"
    environment:
      - PORT=8000
      - HOST=0.0.0.0
      - VIAFOUNDRY_HOSTNAME=${VIAFOUNDRY_HOSTNAME}
      - VIAFOUNDRY_USERNAME=${VIAFOUNDRY_USERNAME}
      - VIAFOUNDRY_PASSWORD=${VIAFOUNDRY_PASSWORD}
      - MCP_API_KEY=${MCP_API_KEY}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

#### 2.3 Create .env.production

```env
# Production environment variables
VIAFOUNDRY_HOSTNAME=https://viafoundry.com
VIAFOUNDRY_USERNAME=service-account
VIAFOUNDRY_PASSWORD=secure-password
MCP_API_KEY=your-secure-api-key-here
PORT=8000
HOST=0.0.0.0
```

---

### Phase 3: Cloud Deployment Options (Week 3)

#### Option A: Railway.app (Easiest)

1. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git push origin main
   ```

2. **Deploy to Railway**
   - Go to https://railway.app
   - "New Project" → "Deploy from GitHub"
   - Select your repository
   - Railway auto-detects Dockerfile
   - Add environment variables in Railway dashboard
   - Get URL: `https://viafoundry-mcp.railway.app`

3. **Cost**: ~$5-10/month

#### Option B: Render.com

1. **Create render.yaml**

```yaml
services:
  - type: web
    name: viafoundry-mcp
    env: docker
    plan: starter
    healthCheckPath: /health
    envVars:
      - key: VIAFOUNDRY_HOSTNAME
        sync: false
      - key: VIAFOUNDRY_USERNAME
        sync: false
      - key: VIAFOUNDRY_PASSWORD
        sync: false
      - key: MCP_API_KEY
        generateValue: true
```

2. **Deploy**
   - Go to https://render.com
   - "New" → "Blueprint"
   - Connect GitHub repository
   - Add environment variables
   - Get URL: `https://viafoundry-mcp.onrender.com`

3. **Cost**: Free tier available, $7/month for production

#### Option C: Fly.io (Most Control)

1. **Install Fly CLI**
   ```bash
   curl -L https://fly.io/install.sh | sh
   ```

2. **Initialize Fly app**
   ```bash
   fly launch --name viafoundry-mcp
   ```

3. **Deploy**
   ```bash
   fly deploy
   fly secrets set VIAFOUNDRY_HOSTNAME=https://viafoundry.com
   fly secrets set VIAFOUNDRY_USERNAME=your-user
   fly secrets set VIAFOUNDRY_PASSWORD=your-pass
   fly secrets set MCP_API_KEY=your-key
   ```

4. **Get URL**: `https://viafoundry-mcp.fly.dev`

5. **Cost**: ~$3-5/month

#### Option D: AWS (Enterprise)

1. **Build and push to ECR**
   ```bash
   aws ecr create-repository --repository-name viafoundry-mcp
   docker build -t viafoundry-mcp .
   docker tag viafoundry-mcp:latest {account}.dkr.ecr.{region}.amazonaws.com/viafoundry-mcp
   docker push {account}.dkr.ecr.{region}.amazonaws.com/viafoundry-mcp
   ```

2. **Deploy to ECS Fargate**
   - Create ECS cluster
   - Create task definition with your container
   - Create service with Application Load Balancer
   - Add SSL certificate via ACM
   - Configure Route53 for custom domain

3. **Cost**: ~$30-50/month

---

### Phase 4: Authentication & Security (Week 4)

#### 4.1 API Key Management

**Create API key system:**

```python
# mcp_server/auth.py

import secrets
import hashlib
from typing import Optional
import json
from pathlib import Path

class APIKeyManager:
    def __init__(self, keys_file: str = "api_keys.json"):
        self.keys_file = Path(keys_file)
        self.keys = self._load_keys()

    def _load_keys(self) -> dict:
        if self.keys_file.exists():
            return json.loads(self.keys_file.read_text())
        return {}

    def _save_keys(self):
        self.keys_file.write_text(json.dumps(self.keys, indent=2))

    def generate_key(self, name: str) -> str:
        """Generate a new API key."""
        key = f"vf_{''.join(secrets.token_urlsafe(32))}"
        key_hash = hashlib.sha256(key.encode()).hexdigest()

        self.keys[key_hash] = {
            "name": name,
            "created": datetime.now().isoformat()
        }
        self._save_keys()

        return key

    def validate_key(self, key: str) -> bool:
        """Validate an API key."""
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        return key_hash in self.keys
```

#### 4.2 OAuth Integration (Optional)

```python
# mcp_server/oauth.py

from authlib.integrations.starlette_client import OAuth

oauth = OAuth()

oauth.register(
    name='viafoundry',
    client_id='your-client-id',
    client_secret='your-client-secret',
    server_metadata_url='https://viafoundry.com/.well-known/oauth-authorization-server',
    client_kwargs={'scope': 'read:reports'}
)
```

---

### Phase 5: Monitoring & Observability (Week 5)

#### 5.1 Add Metrics

```python
from prometheus_client import Counter, Histogram
import time

REQUEST_COUNT = Counter('mcp_requests_total', 'Total MCP requests')
REQUEST_DURATION = Histogram('mcp_request_duration_seconds', 'MCP request duration')

@REQUEST_DURATION.time()
async def mcp_endpoint(request):
    REQUEST_COUNT.inc()
    # ... existing code
```

#### 5.2 Add Logging

```python
import structlog

logger = structlog.get_logger()

logger.info("mcp_request",
    user_id=user_id,
    tool=tool_name,
    duration=duration
)
```

---

## Configuration for Users

### Hosted Service Usage

**In Cursor/Claude Desktop:**

```json
{
  "viafoundry": {
    "url": "https://mcp.viafoundry.com/mcp",
    "headers": {
      "Authorization": "Bearer vf_your_api_key_here"
    }
  }
}
```

### API Key Generation

Users get keys via:

1. **Web Dashboard**: Build a simple dashboard at `https://mcp.viafoundry.com/dashboard`
2. **CLI Tool**: `viafoundry-mcp generate-key`
3. **Email**: Send keys via secure email link

---

## Cost Comparison

| Platform | Setup Time | Monthly Cost | Scalability | Control |
|----------|-----------|--------------|-------------|---------|
| Railway | 10 min | $5-10 | Auto | Low |
| Render | 15 min | $7 (Free tier) | Auto | Low |
| Fly.io | 20 min | $3-5 | Manual | Medium |
| AWS ECS | 2 hours | $30-50 | Full | High |

---

## Recommended Approach

### MVP (Month 1-2)
1. ✅ Keep local installation as primary method
2. ✅ Build HTTP/SSE support
3. ✅ Deploy to Railway/Render as beta
4. ✅ Invite 5-10 beta testers

### Production (Month 3-4)
1. ✅ Add API key management
2. ✅ Deploy to dedicated infrastructure
3. ✅ Add monitoring and alerts
4. ✅ Public launch

### Scale (Month 5+)
1. ✅ Add OAuth support
2. ✅ Build user dashboard
3. ✅ Add usage analytics
4. ✅ Consider multi-region deployment

---

## Next Steps

To start implementing:

```bash
# 1. Create web server module
touch mcp_server/web_server.py

# 2. Update dependencies
pip install uvicorn starlette

# 3. Test locally
viafoundry-mcp-web

# 4. Test with curl
curl http://localhost:8000/health

# 5. Create Dockerfile
# 6. Deploy to Railway (easiest)
```

---

## Support Hybrid Model

Keep both options:

```python
# Local: Fast, private, offline
viafoundry-mcp

# Hosted: Managed, always on, shared
https://mcp.viafoundry.com/mcp
```

Users choose based on their needs!
