#!/bin/bash
# Setup Claude Code authentication for Docker deployment
# Run this script on your LOCAL machine before deploying

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_CONFIG_DIR="$SCRIPT_DIR/claude-config"

echo "=== Claude Code Auth Setup for Docker ==="
echo ""

# Check if Claude CLI is installed
if ! command -v claude &> /dev/null; then
    echo "Error: Claude Code CLI not installed"
    echo "Install it from: https://claude.ai/download"
    exit 1
fi

# Check if already authenticated
echo "Checking Claude authentication status..."
if claude --version &> /dev/null; then
    echo "Claude CLI is installed"
else
    echo "Error: Claude CLI not working"
    exit 1
fi

# Authenticate if needed
echo ""
echo "Testing authentication..."
if claude -p "Say 'auth ok'" --output-format text 2>/dev/null | grep -q "ok"; then
    echo "Already authenticated!"
else
    echo "Not authenticated. Starting login..."
    claude login
    echo ""
    echo "Testing authentication again..."
    if ! claude -p "Say 'auth ok'" --output-format text 2>/dev/null | grep -q "ok"; then
        echo "Error: Authentication failed"
        exit 1
    fi
    echo "Authentication successful!"
fi

# Copy auth config
echo ""
echo "Copying Claude config to $CLAUDE_CONFIG_DIR..."
mkdir -p "$CLAUDE_CONFIG_DIR"

# Copy the essential auth files
if [ -d "$HOME/.claude" ]; then
    # Copy auth-related files (be selective for security)
    cp -r "$HOME/.claude/"* "$CLAUDE_CONFIG_DIR/" 2>/dev/null || true

    # Remove any local settings that shouldn't be in Docker
    rm -f "$CLAUDE_CONFIG_DIR/settings.local.json" 2>/dev/null || true

    echo "Config copied successfully!"
    echo ""
    echo "Contents:"
    ls -la "$CLAUDE_CONFIG_DIR"
else
    echo "Error: ~/.claude directory not found"
    exit 1
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "The claude-config directory is ready for Docker."
echo "Make sure .env has: CLAUDE_CONFIG_PATH=./claude-config"
echo ""
echo "To deploy:"
echo "  cd docker"
echo "  docker compose up -d"
