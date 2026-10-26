#!/usr/bin/env bash
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DOCKER="/usr/local/bin/docker"
REPO="/Users/claudiu/vps-root/aurel2"
# Paper stack shut down 2026-10-26; only the real daemon is reported.
REAL_CONTAINER="aurel2-real"

section() {
	printf '\n== %s ==\n' "$1"
}

section "host watchdog"
/root/aurel2/scripts/aurel2_host_watchdog.sh

section "containers"
"$DOCKER" ps --format '{{.Names}} {{.Status}}'

section "real log"
"$DOCKER" logs "$REAL_CONTAINER" --tail 30 2>&1 | grep -v heartbeat || true

section "real state"
"$DOCKER" exec "$REAL_CONTAINER" python -c '
import json, os
live = "/app/data/live"
out = {}
for name in ("pending_decisions.json", "trade_journal.json", "heartbeat.json"):
    path = f"{live}/{name}"
    if not os.path.exists(path):
        out[name] = None
        continue
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if name == "trade_journal.json" and isinstance(data, list):
        last = data[-1] if data else {}
        out[name] = {"entries": len(data), "latest": {k: last.get(k) for k in ("timestamp", "action", "decision_symbol", "current_holding_symbol", "executed", "execution_error", "account_value_after")}}
    else:
        out[name] = data
print(json.dumps(out, sort_keys=True, default=str))
' 2>&1 || true

section "repository"
git -C "$REPO" status --short
git -C "$REPO" log --oneline -5
