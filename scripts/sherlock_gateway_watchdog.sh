#!/usr/bin/env bash
# Watchdog for the Hermes Sherlock gateway on Dumbo.
# Catches the failure launchd cannot see: the process stays alive while its
# Slack socket is permanently wedged ("Session is closed" retry loop, 2026-08-18).
set -u

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# This is a LaunchAgent loaded in the logged-in GUI domain. After a host or
# gateway restart, launchctl may no longer alias it through user/<uid> even
# though gui/<uid> owns and runs it.
SERVICE="gui/$(id -u)/ai.hermes.gateway-sherlock"
AGENT_LOG="$HOME/.hermes/profiles/sherlock/logs/agent.log"
GATEWAY_LOG="$HOME/.hermes/profiles/sherlock/logs/gateway.log"
STATE_DIR="$HOME/.hermes/state"
LAST_FIX="$STATE_DIR/sherlock-watchdog-last-restart"
LOCK_DIR="/tmp/sherlock-watchdog.lock"
NTFY_TOPIC="${NTFY_TOPIC:-aurel2}"

# Don't restart more than once per 15 min: a restart loop would be worse than silence.
COOLDOWN_S=900
# Wedged means errors in the recent window with no successful connect after them.
WINDOW_MIN=5

log() { printf '%s sherlock-watchdog: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
	log "previous run still active"
	exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT
mkdir -p "$STATE_DIR"

notify() {
	curl -s -m 10 -H "Title: $1" -H "Priority: high" -H "Tags: warning" \
		-d "$2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || true
}

# 1. Process alive at all? If launchd already has it down, let KeepAlive do its job.
if ! launchctl print "$SERVICE" >/dev/null 2>&1; then
	log "FAILED: service not loaded"
	notify "Sherlock watchdog" "ai.hermes.gateway-sherlock is not loaded on Dumbo"
	exit 1
fi

[ -r "$AGENT_LOG" ] || { log "no agent log yet"; exit 0; }

# 2. Look only at the last WINDOW_MIN minutes of log.
# BSD awk (macOS) has no mktime(), so compare the log's "YYYY-MM-DD HH:MM:SS"
# prefix lexically against a cutoff string — same ordering, no date math.
cutoff_str=$(date -v-${WINDOW_MIN}M '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "-${WINDOW_MIN} minutes" '+%Y-%m-%d %H:%M:%S')
recent=$(awk -v cutoff="$cutoff_str" '
	/^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]/ {
		keep = (substr($0, 1, 19) >= cutoff)
	}
	keep
' "$AGENT_LOG" 2>/dev/null)

fail_count=$(printf '%s\n' "$recent" | grep -c "Session is closed" 2>/dev/null || true)
fail_count=${fail_count:-0}

if [ "$fail_count" -lt 3 ]; then
	log "ok (${fail_count} socket errors in last ${WINDOW_MIN}m)"
	exit 0
fi

# 3. A reconnect after the last error means it healed itself; don't interfere.
last_err_line=$(printf '%s\n' "$recent" | grep -n "Session is closed" | tail -1 | cut -d: -f1)
last_ok_line=$(printf '%s\n' "$recent" | grep -n "Socket Mode connected" | tail -1 | cut -d: -f1)
if [ -n "${last_ok_line:-}" ] && [ -n "${last_err_line:-}" ] && [ "$last_ok_line" -gt "$last_err_line" ]; then
	log "ok (reconnected after errors)"
	exit 0
fi

# 4. Confirm Slack itself is reachable, so we restart for a stuck client, not an outage.
if ! curl -s -m 10 -o /dev/null "https://slack.com/api/api.test"; then
	log "slack.com unreachable; network problem, not a stuck client — not restarting"
	exit 0
fi

# 5. Cooldown guard.
last_fix=$(cat "$LAST_FIX" 2>/dev/null | tr -dc '0-9')
[ -n "$last_fix" ] || last_fix=0
now_s=$(date +%s)
if [ "$last_fix" -ge $((now_s - COOLDOWN_S)) ]; then
	log "wedged but restarted recently ($(( (now_s - last_fix) / 60 ))m ago); escalating instead"
	notify "Sherlock still wedged" "Restarted $(( (now_s - last_fix) / 60 ))m ago and Slack is still failing. Needs a look."
	exit 1
fi

log "wedged (${fail_count} errors, no reconnect); restarting gateway"
echo "$now_s" > "$LAST_FIX"
launchctl kickstart -k "$SERVICE" || { log "FAILED: kickstart failed"; notify "Sherlock watchdog" "kickstart failed on Dumbo"; exit 1; }

# 6. Verify the fix actually took, rather than assuming.
for _ in 1 2 3 4 5 6 7 8 9 10; do
	sleep 3
	if tail -40 "$GATEWAY_LOG" 2>/dev/null | grep -q "Socket Mode connected"; then
		log "recovered: Socket Mode connected"
		notify "Sherlock recovered" "Slack socket was wedged; gateway restarted and reconnected."
		exit 0
	fi
done

log "FAILED: restarted but no reconnect confirmed"
notify "Sherlock restart unconfirmed" "Gateway was restarted but Socket Mode did not confirm within 30s."
exit 1
