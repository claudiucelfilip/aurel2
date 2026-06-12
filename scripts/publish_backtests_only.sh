#!/bin/bash
# Publish the latest dashboard backtests to production WITHOUT restarting the daemon.
#
# Why: during an active paper-trading run, we want the dashboard graphs to reflect the
# latest `data/backtest_comparison.json` without risking any runtime behavior change.
#
# This script:
# 1) Regenerates `data/backtest_comparison.json` locally (in /root/aurel2)
# 2) Copies it to `/opt/aurel2/data/backtest_comparison.json` (dashboard bind-mount source)
#
# It does NOT:
# - rsync source code/config
# - rebuild or restart Docker containers
#
# Usage:
#   bash scripts/publish_backtests_only.sh
#
# Optional:
#   AUREL2_BACKTEST_END_DATE=2026-04-01 bash scripts/publish_backtests_only.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Regenerating dashboard backtests (no deploy) ==="
python3 -m aurel2.engine.backtest

DEST_DIR="/opt/aurel2/data"
DEST_FILE="${DEST_DIR}/backtest_comparison.json"

if [ ! -d "$DEST_DIR" ]; then
  echo ""
  echo "ERROR: ${DEST_DIR} not found."
  echo "This script is intended to run on the VPS where /opt/aurel2 is deployed."
  exit 1
fi

echo ""
echo "=== Publishing backtest artifact only ==="
cp "$REPO_ROOT/data/backtest_comparison.json" "$DEST_FILE"
ls -la "$DEST_FILE"

echo ""
echo "=== Done (daemon not restarted) ==="

