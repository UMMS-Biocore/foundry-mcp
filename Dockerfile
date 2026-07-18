# =============================================================================
# ViaFoundry MCP Server - Docker Image
# =============================================================================
# Multi-stage build for smaller, secure images
#
# Build:  docker build -t viafoundry-mcp .
# Run:    docker run -p 8705:8705 viafoundry-mcp
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Build
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# git is required to fetch foundry-sdk from GitHub.
# Use HTTPS Debian mirrors first: some networks (e.g. proxied build hosts) filter
# cleartext apt over HTTP:80 while allowing HTTPS; ca-certificates ship in the base.
RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \
        [ -f "$f" ] && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; \
    done; \
    apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install build dependencies
RUN pip install --no-cache-dir build wheel

# Copy only what's needed for the build
COPY pyproject.toml setup.py README.md ./
COPY src/ ./src/

# Build the wheel
RUN python -m build --wheel

# Build the foundry-sdk dependency into a wheel. The repo is public, so no
# token or auth is required. Pinned for reproducibility.
ARG SDK_GIT_REF=e5baa08546ea
RUN pip wheel --no-deps --wheel-dir /build/dist \
         "git+https://github.com/UMMS-Biocore/foundry-sdk.git@${SDK_GIT_REF}"

# -----------------------------------------------------------------------------
# Stage 2: Runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Security: Create non-root user
RUN groupadd --gid 1000 mcp && \
    useradd --uid 1000 --gid mcp --shell /bin/bash --create-home mcp

WORKDIR /app

# Install the mcp + viafoundry_sdk wheels from the builder stage
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Create writable directory and log file for the viafoundry SDK
RUN chown -R mcp:mcp /app && \
    touch /app/viafoundry_errors.log && \
    chown mcp:mcp /app/viafoundry_errors.log

# Switch to non-root user
USER mcp

# Default port
ENV PORT=8705
EXPOSE 8705

# Health check (TCP port check - MCP endpoint requires special headers)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.settimeout(5); s.connect(('localhost', ${PORT})); s.close()" || exit 1

# Run the HTTP server (bind to 0.0.0.0 for Docker)
CMD ["sh", "-c", "viafoundry-mcp --host 0.0.0.0 --port ${PORT}"]
