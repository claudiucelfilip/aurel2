#!/usr/bin/env bash
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DOCKER="/usr/local/bin/docker"
REPO="/Users/claudiu/vps-root/aurel2"
TRADING_CONTAINER="aurel2-trading-aurel2-1"
MONITOR_CONTAINER="aurel2-trading-monitor-1"
REAL_CONTAINER="aurel2-real"

section() {
	printf '\n== %s ==\n' "$1"
}

section "host watchdog"
/root/aurel2/scripts/aurel2_host_watchdog.sh

section "containers"
"$DOCKER" ps --format '{{.Names}} {{.Status}}'

section "trading log"
"$DOCKER" logs "$TRADING_CONTAINER" --tail 30 2>&1

section "monitor log"
"$DOCKER" logs "$MONITOR_CONTAINER" --tail 20 2>&1

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

section "paper state"
"$DOCKER" exec "$TRADING_CONTAINER" python -c '
import json
import os
from datetime import datetime, timezone

paper = "/app/data/paper"
pending_path = f"{paper}/pending_decisions.json"
journal_path = f"{paper}/trade_journal.json"

with open(pending_path, encoding="utf-8") as handle:
    pending = json.load(handle)
with open(journal_path, encoding="utf-8") as handle:
    journal = json.load(handle)

latest = journal[-1] if isinstance(journal, list) and journal else None
latest_summary = None
if isinstance(latest, dict):
    keys = (
        "timestamp",
        "entry_type",
        "decision_type",
        "action",
        "decision_symbol",
        "current_holding_symbol",
        "executed",
        "execution_error",
        "account_value_before",
        "account_value_after",
    )
    latest_summary = {key: latest.get(key) for key in keys}

result = {
    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    "pending_decisions": pending,
    "journal_entries": len(journal) if isinstance(journal, list) else None,
    "journal_mtime_utc": datetime.fromtimestamp(
        os.path.getmtime(journal_path), timezone.utc
    ).isoformat(),
    "latest_journal_entry": latest_summary,
}
print(json.dumps(result, sort_keys=True))
'
