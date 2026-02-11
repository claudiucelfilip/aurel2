#!/bin/bash
# Deploy aurel2 to production (from /root/aurel2 → /opt/aurel2)
# Runs tests first, then syncs code and rebuilds Docker containers.
# Rebuilds aurel2 + dashboard containers only.

set -euo pipefail

echo "=== Running tests ==="
python3 -m pytest tests/ -x -q --tb=short

echo ""
echo "=== Syncing source code to /opt/aurel2/ ==="
rsync -a --delete \
  --exclude='.git' \
  --exclude='data/paper/' \
  --exclude='data/live/' \
  --exclude='data/price_cache/' \
  --exclude='data/archive/' \
  /root/aurel2/src /root/aurel2/scripts /root/aurel2/config \
  /root/aurel2/docker/Dockerfile /root/aurel2/pyproject.toml \
  /opt/aurel2/

# Sync backtest data separately (not --delete, just update)
cp /root/aurel2/data/backtest_comparison.json /opt/aurel2/data/ 2>/dev/null || true

echo "=== Building + restarting containers (aurel2 + dashboard) ==="
cd /opt/aurel2/docker
docker compose build aurel2 dashboard
docker compose up -d aurel2 dashboard

echo ""
echo "=== Deploy complete ==="
docker compose ps aurel2 dashboard
