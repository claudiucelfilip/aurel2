#!/bin/bash
# Deploy aurel2 to production (from /root/aurel2 → /opt/aurel2)
# Runs tests first, then syncs code and rebuilds Docker containers.
# Rebuilds aurel2 + dashboard containers only.

set -euo pipefail

echo "=== Running tests ==="
python3 -m pytest tests/ -x -q --tb=short

echo ""
if [ "${SKIP_BACKTEST_REGEN:-0}" = "1" ]; then
    echo "=== Skipping dashboard backtest regeneration (SKIP_BACKTEST_REGEN=1) ==="
else
    echo "=== Regenerating dashboard backtests ==="
    python3 -m aurel2.engine.backtest
fi

# Check if dashboard needs a full rebuild (deps or Dockerfile changed)
REBUILD_DASHBOARD=false
if ! diff -q /root/aurel2/pyproject.toml /opt/aurel2/pyproject.toml >/dev/null 2>&1; then
    REBUILD_DASHBOARD=true
fi
if ! diff -q /root/aurel2/docker/Dockerfile /opt/aurel2/docker/Dockerfile >/dev/null 2>&1; then
    REBUILD_DASHBOARD=true
fi

echo ""
echo "=== Syncing source code to /opt/aurel2/ ==="
# Sync top-level dirs and pyproject
rsync -a --delete \
  --exclude='.git' \
  /root/aurel2/src /root/aurel2/scripts /root/aurel2/config \
  /root/aurel2/pyproject.toml \
  /opt/aurel2/

# Sync docker config files INTO /opt/aurel2/docker/ (not /opt/aurel2/!) — the
# previous rsync syntax silently put them at /opt/aurel2/{Dockerfile,docker-compose.yml},
# leaving the actual /opt/aurel2/docker/ stale on every deploy.
mkdir -p /opt/aurel2/docker/
cp /root/aurel2/docker/Dockerfile          /opt/aurel2/docker/Dockerfile
cp /root/aurel2/docker/docker-compose.yml  /opt/aurel2/docker/docker-compose.yml
cp /root/aurel2/docker/.env.example        /opt/aurel2/docker/.env.example

# Ensure mode-partitioned data dirs exist on host (bind mounts target these)
mkdir -p /opt/aurel2/data/paper /opt/aurel2/data/live /opt/aurel2/data/archive

# Sync backtest data separately (not --delete, just update)
cp /root/aurel2/data/backtest_comparison.json /opt/aurel2/data/ 2>/dev/null || true

cd /opt/aurel2/docker
if [ "$REBUILD_DASHBOARD" = true ]; then
    # Deps/Dockerfile changed: a real image rebuild via compose is required.
    # NOTE: this needs a valid /opt/aurel2/docker/.env (compose interpolates it).
    echo "=== Rebuilding images (deps changed) — requires valid .env ==="
    docker compose build aurel2 dashboard
    docker compose up -d aurel2 dashboard
else
    # Code-only change: src is bind-mounted into the containers, so a plain restart
    # reloads it. Use `docker restart` (not compose) so deploys work even when
    # /opt/aurel2/docker/.env is missing/broken — the container keeps its loaded env.
    echo "=== Restarting aurel2 to reload bind-mounted code (no compose/.env needed) ==="
    docker restart aurel2-trading-aurel2-1
    echo "Dashboard: source bind-mounted, auto-reloading (no restart needed)"
fi

echo ""
echo "=== Deploy complete ==="
docker ps --filter name=aurel2-trading- --format 'table {{.Names}}\t{{.Status}}'
