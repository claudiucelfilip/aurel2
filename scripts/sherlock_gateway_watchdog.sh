#!/usr/bin/env bash
# Watchdog for the Hermes Sherlock gateway on Dumbo.
# Catches the failure seen 2026-08-27 that left Sherlock silently deaf for 12h:
# after a network blip the Slack Socket Mode aiohttp session gets stuck and
# never self-heals, looping "Session is closed" every ~15s. The process stays
# alive, so launchd KeepAlive and `gateway status` both report healthy while
# no Slack message is ever received. Only the log reveals it.
set -u

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROFILE_DIR="$HOME/.hermes/profiles/sherlock"
ERR_LOG="$HOME/.hermes/logs/gateway.error.log"
GW_LOG="$PROFILE_DIR/logs/gateway.log"
LOCK_DIR="/tmp/sherlock-watchdog.lock"
STATE_DIR="$HOME/.hermes/state"
LAST_FIX="$STATE_DIR/sherlock-watchdog-last-restart"
NTFY_TOPIC="${NTFY_TOPIC:-aurel2}"
COOLDOWN_S=900
SERVICE="ai.hermes.gateway" # one gateway serves all profiles since Hermes 0.21
# gateway.error.log is raw stderr with NO timestamps. Measure only newly
# appended bytes that contain the known stuck-loop signature; unrelated agent
# tool errors also reach this shared stderr and must never trigger a restart.
PROBE_S=45
GROWTH_THRESHOLD_B=1500

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

# Restart the gateway. The service lives in the gui/ domain, NOT user/ —
# `user/<uid>/...` fails with "Could not find service in domain".
restart_gateway() {
	launchctl kickstart -k "gui/$(id -u)/$SERVICE" 2>/dev/null \
		|| launchctl kickstart -k "user/$(id -u)/$SERVICE" 2>/dev/null \
		|| return 1
}

# Sample signature-bearing growth over PROBE_S. A stuck gateway writes the
# "Session is closed" retry line continuously. Other stderr growth is ignored.
stuck_log_growth() {
	local a b start
	a=$(stat -f%z "$ERR_LOG" 2>/dev/null || echo 0)
	sleep "$PROBE_S"
	b=$(stat -f%z "$ERR_LOG" 2>/dev/null || echo 0)
	# Log rotation/truncation shows as negative growth; treat as no signal.
	if [ "$b" -le "$a" ]; then
		echo 0
		return
	fi
	start=$((a + 1))
	tail -c "+$start" "$ERR_LOG" 2>/dev/null \
		| grep -E 'Session is closed' 2>/dev/null \
		| wc -c \
		| tr -dc '0-9'
}

# Scale the observed growth to bytes/min for a stable threshold.
growth_per_min() {
	local g; g=$(stuck_log_growth); [ -n "$g" ] || g=0
	echo $(( g * 60 / PROBE_S ))
}

# A watchdog restart must not kill scheduled work. Cron run state is durable
# and readable even when Slack's socket is unhealthy, so defer until the next
# watchdog pass if any profile currently owns a running execution.
cron_in_flight() {
	local profile
	for profile in default claw sherlock; do
		if hermes --profile "$profile" cron runs --limit 50 2>/dev/null \
			| awk '$2 == "running" { found=1 } END { exit(found ? 0 : 1) }'; then
			return 0
		fi
	done
	return 1
}

status=0

# --- Check 1: the gateway process exists at all ---
if ! pgrep -f "hermes gateway run --external-supervisor" >/dev/null 2>&1; then
	log "gateway process not running"
	last_fix=$(tr -dc '0-9' < "$LAST_FIX" 2>/dev/null); [ -n "$last_fix" ] || last_fix=0
	now_s=$(date +%s)
	if [ "$last_fix" -ge $((now_s - COOLDOWN_S)) ]; then
		log "restarted recently; escalating"
		notify "Sherlock gateway down" "Process still missing after a restart $(( (now_s - last_fix) / 60 ))m ago."
		exit 1
	fi
	echo "$now_s" > "$LAST_FIX"
	restart_gateway
	sleep 20
	if pgrep -f "hermes gateway run --external-supervisor" >/dev/null 2>&1; then
		log "recovered: gateway process back"
		notify "Sherlock recovered" "Gateway process was missing; restarted and is running."
	else
		log "FAILED: gateway process still missing after restart"
		notify "Sherlock gateway down" "Restarted but the gateway process is still not running."
		status=1
	fi
	exit "$status"
fi

# --- Check 2: the stuck-session loop (the silent killer) ---
# Only meaningful when Dumbo itself is online; a real outage is not Sherlock's fault.
if curl -s -m 10 -o /dev/null "https://slack.com/api/api.test"; then
	recent=$(growth_per_min)
	if [ "${recent:-0}" -ge "$GROWTH_THRESHOLD_B" ]; then
		log "Slack socket stuck (error log growing ${recent} B/min)"
		if cron_in_flight; then
			log "cron execution in flight; deferring gateway restart"
			exit 0
		fi
		last_fix=$(tr -dc '0-9' < "$LAST_FIX" 2>/dev/null); [ -n "$last_fix" ] || last_fix=0
		now_s=$(date +%s)
		if [ "$last_fix" -ge $((now_s - COOLDOWN_S)) ]; then
			log "restarted recently; escalating"
			notify "Sherlock Slack stuck" "Socket Mode still dead after a restart $(( (now_s - last_fix) / 60 ))m ago. Needs a look."
			exit 1
		fi
		echo "$now_s" > "$LAST_FIX"
		log "restarting gateway"
		restart_gateway
		sleep 25
		if [ "$(growth_per_min)" -lt "$GROWTH_THRESHOLD_B" ]; then
			log "recovered: Slack Socket Mode reconnected"
			notify "Sherlock recovered" "Slack socket was stuck in a dead retry loop; restarted and reconnected."
		else
			log "FAILED: Slack still not connected after restart"
			notify "Sherlock Slack down" "Gateway restarted but Slack Socket Mode is still not connected."
			status=1
		fi
		exit "$status"
	fi
fi

[ "$status" -eq 0 ] && log "ok"
exit "$status"
