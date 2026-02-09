# Aurel2 Trading Daemon
# Multi-stage build for smaller image

# --- Build stage ---
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster package management
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Copy project files
COPY pyproject.toml .
COPY README.md .
COPY src/ src/

# Create virtual environment and install dependencies
RUN uv venv /app/.venv
RUN . /app/.venv/bin/activate && uv pip install ".[broker,dashboard]"

# --- Runtime stage ---
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies including Node.js for Claude CLI and Docker CLI for gateway restart
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y nodejs docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI via npm
RUN npm install -g @anthropic-ai/claude-code

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Copy config and pre-computed data
COPY config/ /app/config/
COPY data/backtest_comparison.json /app/data/backtest_comparison.json

# Set up environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1

# Create directories for persistent data
RUN mkdir -p /root/.aurel2 /root/.claude

# Health check - verify heartbeat file is recent
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "from pathlib import Path; import json; import time; \
        hb = Path('/root/.aurel2/heartbeat.json'); \
        exit(0 if hb.exists() and time.time() - json.loads(hb.read_text())['timestamp'] < 180 else 1)"

# Default command: run the live trading daemon in paper mode
ENTRYPOINT ["python", "-m", "aurel2.cli"]
CMD ["live", "--paper"]
