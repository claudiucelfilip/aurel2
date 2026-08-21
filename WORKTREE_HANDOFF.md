# WORKTREE_HANDOFF.md

owner: Claw / Aurel2 and Sherlock runtime
created_utc: 2026-07-19T10:46:00Z
repo: /Users/claudiu/vps-root/aurel2
service: Aurel2 paper overlay state
purpose: Record ownership and operational context for the Sherlock watchdog source.

## Current Classification

active_in_runtime: yes
money_or_trading_adjacent: no
classification: committed_runtime_support_source

## Current Repository State

- `scripts/sherlock_gateway_watchdog.sh`: tracked runtime watchdog added on
  2026-08-18 for the Hermes Sherlock Slack gateway. It detects repeated
  `Session is closed` loops, applies a guarded launchd restart with a 15-minute
  cooldown, verifies reconnection, and sends ntfy alerts on real failures.
- The watchdog and this handoff were committed together; this document does
  not cover any future dirty files.

## Required Handling

- Treat the watchdog as active operational source until it is intentionally
  retired or relocated by its owner.
- New dirty source, config, order, journal, or runtime files require their own
  classification; this handoff does not cover them.

## Next Check

due_utc: none
owner: Claw / Aurel2 runtime owner
