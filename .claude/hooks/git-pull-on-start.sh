#!/bin/bash
# Pull latest code when a Claude Code session starts.
# Runs a fast-forward only pull to avoid merge conflicts.
cd "$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
git pull --ff-only --quiet 2>/dev/null
exit 0
