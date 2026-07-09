#!/bin/bash
# Push OpenClaw cron error-runs to ntfy. Workaround for the OpenClaw 2026.6.11
# regression where failure notifications are never sent (deliveryError just
# mirrors run.error). Independent escalation path — runs via launchd, no OpenClaw.
set -u
DB="$HOME/.openclaw/state/openclaw.sqlite"
STATE="$HOME/.openclaw/state/failure_notifier_last_ts"
NTFY_TOPIC="${NTFY_TOPIC:-aurel2}"
[ -r "$DB" ] || exit 0

last=$(cat "$STATE" 2>/dev/null | tr -dc '0-9')
[ -n "$last" ] || last=0
# First run OR stale/corrupt state (>24h old): arm from now, never replay history
now_ms=$(( $(date +%s) * 1000 ))
if [ "$last" -eq 0 ] || [ "$last" -lt $((now_ms - 86400000)) ]; then
  sqlite3 "$DB" "select coalesce(max(ts),$now_ms) from cron_run_logs;" > "$STATE"
  exit 0
fi

rows=$(sqlite3 -separator '|' "$DB" \
  "select r.ts, coalesce(j.name, r.job_id), replace(substr(coalesce(nullif(r.error,''), nullif(r.summary,''), 'unknown error'),1,300), char(10), ' ')
   from cron_run_logs r left join cron_jobs j on j.job_id = r.job_id
   where r.status='error' and r.ts > $last order by r.ts asc;")

# Blast-radius cap: >5 pending failures collapse into one summary push
count=$(printf '%s\n' "$rows" | grep -c '|' 2>/dev/null)
if [ "${count:-0}" -gt 5 ]; then
  newmax=$(sqlite3 "$DB" "select max(ts) from cron_run_logs where status='error';")
  curl -s -m 10 -H "Title: OpenClaw crons: $count failures pending" \
    -H "Priority: high" -H "Tags: warning" \
    -d "$count error runs since last check — inspect cron_run_logs on Dumbo" \
    "https://ntfy.sh/$NTFY_TOPIC" >/dev/null && echo "$newmax" > "$STATE"
  exit 0
fi

maxts=$last
IFS=$'\n'
for row in $rows; do
  ts="${row%%|*}"; rest="${row#*|}"; job="${rest%%|*}"; err="${rest#*|}"
  when=$(date -r $((ts/1000)) '+%m-%d %H:%M' 2>/dev/null || echo "$ts")
  curl -s -m 10 \
    -H "Title: OpenClaw cron failed: $job" \
    -H "Priority: high" -H "Tags: warning" \
    -d "[$when] $err" \
    "https://ntfy.sh/$NTFY_TOPIC" >/dev/null || exit 0  # keep state on send failure; retry next run
  [ "$ts" -gt "$maxts" ] && maxts=$ts
  echo "$maxts" > "$STATE"
done
exit 0
