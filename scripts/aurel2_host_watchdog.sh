#!/usr/bin/env bash
set -u

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

LOCK_DIR="/tmp/aurel2-host-watchdog.lock"
LOG_PREFIX="aurel2-host-watchdog"

log() {
	printf '%s %s: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$LOG_PREFIX" "$*"
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
	log "previous run still active"
	exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT

has_active_lima_process() {
	pgrep -f '[/ ](colima|lima|limactl|qemu-system|vz)' >/dev/null 2>&1
}

clear_stale_lima_runtime_files() {
	local dir="$HOME/.colima/_lima/colima"
	local stamp

	stamp="$(date -u '+%Y%m%dT%H%M%SZ')"
	if has_active_lima_process; then
		log "not clearing stale Lima files: active Lima/Colima process exists"
		return 1
	fi

	for file in ha.sock ha.pid ssh.sock vz.pid; do
		if [ -e "$dir/$file" ]; then
			mv "$dir/$file" "$dir/$file.stale-$stamp"
			log "moved stale $dir/$file"
		fi
	done
}

start_colima() {
	log "starting Colima"
	if colima start; then
		return 0
	fi

	log "Colima start failed; clearing stale Lima runtime files and retrying"
	clear_stale_lima_runtime_files || return 1
	colima start
}

ensure_docker() {
	if docker info >/dev/null 2>&1; then
		return 0
	fi

	log "Docker unreachable"
	start_colima
	docker info >/dev/null 2>&1
}

container_exists() {
	docker inspect "$1" >/dev/null 2>&1
}

container_running() {
	[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}

container_healthy_or_no_healthcheck() {
	local status

	status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || true)"
	[ "$status" = "healthy" ] || [ "$status" = "none" ] || [ "$status" = "starting" ]
}

ensure_container() {
	local name="$1"

	if ! container_exists "$name"; then
		log "container missing: $name"
		return 1
	fi

	if ! container_running "$name"; then
		log "starting stopped container: $name"
		docker start "$name" >/dev/null
	fi

	if ! container_healthy_or_no_healthcheck "$name"; then
		log "restarting unhealthy container: $name"
		docker restart "$name" >/dev/null
	fi
}

if ! ensure_docker; then
	log "FAILED: Docker remains unreachable"
	exit 1
fi

status=0
for container in \
	aurel2-live-runner \
	aurel2-trading-aurel2-1 \
	aurel2-trading-monitor-1 \
	aurel2-trading-dashboard-1 \
	docker-aurel2-crypto-1 \
	docker-dashboard-1
do
	ensure_container "$container" || status=1
done

if [ "$status" -eq 0 ]; then
	log "ok"
else
	log "FAILED: one or more Aurel containers could not be recovered"
fi

exit "$status"
