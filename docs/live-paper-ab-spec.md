# Live vs Paper A/B Spec

Date: 2026-03-25

## Goal
Run a **real structural A/B test**:
- **Paper (Aurel2):** baseline benchmark
- **Live ($100 challenge):** separate experimental system

## Paper (Benchmark) — `mode=paper`
- Strategy set: **Dual Momentum only**
- Orchestration profile:
  - No dynamic strategy weighting
  - No regime-based strategy selection
  - No correlation guard
  - No sideways-hold override
  - No calm-market hold override
- Purpose: stable, comparable benchmark track

## Live (Experiment) — `mode=live`
- Strategy set: **Dual Momentum + Mean Reversion + Multi-Timeframe**
- Orchestration profile:
  - Weighted multi-strategy voting enabled
  - Regime-adaptive weighting enabled
  - Existing safeguards remain active
- Purpose: test whether adaptive multi-signal behavior outperforms baseline in real execution

## Comparison Window Rules
1. Keep profiles fixed for test window (no moving target).
2. Compare weekly on:
   - Return
   - Max drawdown
   - Turnover (# switches/trades)
   - Decision quality (agreement/override + outcome)
3. Only promote paper changes to live by explicit decision, not by default.

## Code Hooks
- `src/aurel2/live/checker.py`: mode-specific strategy/orchestrator profile split
- `src/aurel2/agent/orchestrator.py`: `dm_primary_enabled` flag for profile-level behavior
