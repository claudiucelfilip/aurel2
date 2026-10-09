# WORKTREE_HANDOFF.md

owner: Claw / Aurel2 and Sherlock runtime
created_utc: 2026-10-03T11:46:00Z
repo: /Users/claudiu/vps-root/aurel2
service: Aurel2 paper overlay state
purpose: Record ownership and operational context for the Sherlock watchdog source.

## Current Classification

active_in_runtime: yes
money_or_trading_adjacent: no
classification: validated_watchdog_hotfix

## Current Repository State

- `scripts/sherlock_gateway_watchdog.sh`: updated after the 2026-10-03
  watchdog restart interrupted cron job `474529cc735a`. The shared gateway
  stderr was growing from unrelated research-tool failures, but the watchdog
  treated all growth as proof of the historical Slack `Session is closed`
  wedge. The hotfix now counts only newly appended bytes containing that exact
  signature and defers any restart while a cron execution is running.
- The same hotfix is deployed at
  `~/.hermes/bin/sherlock_gateway_watchdog.sh`. Source and runtime copy were
  verified byte-identical on 2026-10-09 before committing the source change.

## Required Handling

- Treat the watchdog as active operational source until it is intentionally
  retired or relocated by its owner.
- Preserve the shared-gateway and in-flight-cron guards when changing this
  watchdog. Revalidate syntax and source/runtime equality before redeploying.
- New unrelated dirty source, config, order, journal, or runtime files require
  their own classification; this handoff does not cover them.

## Validation

- `bash -n scripts/sherlock_gateway_watchdog.sh`: passed on 2026-10-09.
- Source/runtime `cmp`: matched on 2026-10-09.
- LaunchAgent `org.clawdiu.sherlock-gateway-watchdog`: last exit code 0.
- Natural watchdog passes remained clean through 2026-10-09T18:13:26Z
  without restarting the healthy shared gateway.
