# WORKTREE_HANDOFF.md

owner: Claw / Aurel2 runtime
created_utc: 2026-07-19T10:46:00Z
repo: /Users/claudiu/vps-root/aurel2
service: Aurel2 paper overlay state
purpose: Cover live dirty runtime state found during heartbeat hard-gate checks.

## Current Classification

active_in_runtime: yes
money_or_trading_adjacent: yes
classification: protected_runtime_state_covered_by_handoff

## Current Dirty State

- `data/paper/overlay_state.json`: untracked Aurel2 paper overlay state file,
  mounted into the Aurel2 containers via `docker/docker-compose.yml`. Current
  contents are the empty/default overlay cap state:
  `last_accelerate_entry_date=null`, `last_force_defensive_date=null`,
  `active_lookback_override=null`, and
  `lookback_activations_by_quarter={}`.

## Required Handling

- Do not delete, stash, or revert this file without explicit Claudiu approval.
- Treat changes under `data/paper/` as trading-adjacent runtime state until
  classified by the Aurel2 owner.
- This handoff covers the exact dirty signature found on 2026-07-19. Refresh it
  if additional source, config, order, journal, or runtime files become dirty.

## Next Check

due_utc: none
owner: Claw / Aurel2 runtime owner
