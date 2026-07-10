# Track A — Debt Cleanup: Build Report

**Date:** 2026-07-10
**Branch:** track-a-cleanup (based on autonomous-trading)
**Spec:** `docs/plans/2026-07-10-ai-overlay-design.md` (§Build tracks, Track A), `docs/plans/2026-07-09-edge-decomposition-goal.md` (§Phase 2 non-negotiables)

## Summary

Purged the dead multi-strategy voting machinery, unified the live/backtest config split into one canonical artifact, and added a weekly fidelity guard script. **Acceptance test passed: byte-identical decision logs before and after cleanup** over the full 2026-02-11→2026-07-09 window. Full test suite: 307 passing before, 223 passing after (84 tests removed with their now-deleted modules), zero failures at every step.

## 1. What was deleted

### Dead strategy files (never wired into the live decision path — production is DM-primary, meaning `dual_momentum`'s own signal drives every trade directly)
- `src/aurel2/strategies/mean_reversion.py` — `MeanReversionStrategy` (SPY-only RSI, structurally blind per spec)
- `src/aurel2/strategies/multi_timeframe.py` — `MultiTimeframeTrendStrategy`
- `src/aurel2/strategies/robust_quarterly.py`, `robust_quarterly_crisis.py`, `robust_quarterly_early_switch.py` — unwired quarterly-gate variants, already retired from production on 2026-05-07 per a pre-existing checker.py comment (kept, since it's accurate history)

### Standalone tools built entirely around the dead strategies (unwired from `live`/`monitor`/`check`)
- `src/aurel2/mcp/` (`server.py`, `__init__.py`) — MCP tool server exposing all 3 dead strategies; not registered with any MCP client, only reachable via now-deleted CLI commands
- `src/aurel2/engine/backtest_agent.py` — `AgentBacktestEngine`, a second, parallel backtest engine hardcoded to the 3-strategy weighted vote
- `src/aurel2/engine/ai_backtest.py` — same pattern, AI-focused variant
- `src/aurel2/engine/decision_analyzer.py` — 582-line analyzer whose only consumer was `backtest_agent.py`'s output types; nothing else imported it
- `src/aurel2/agent/eval_runner.py` — `EvalRunner`, evaluated Haiku/Sonnet/Opus against the 3-strategy signal set
- `src/aurel2/agent/advisor.py::run_advisory_check` — dead function (defined, never called), depended on `Aurel2MCPServer`
- CLI commands `agent`, `strategies`, `backtest-agent`, `eval-agent` (all standalone entry points into the above, no live-path caller)

### Dormant orchestrator toggles (`src/aurel2/agent/orchestrator.py`)
- **`REGIME_WEIGHTS`/`BASE_WEIGHTS`/`_get_dynamic_weights`/`_select_best_action`** — the weighted-vote path. With `dm_primary_enabled=True` hardcoded everywhere in production, this machinery only ever fed a gate-check (`voted_action == BUY`) inside calm-hold/sideways-hold — never the traded action/asset itself. Once mean_reversion/multi_timeframe are gone, a weighted vote over one strategy is definitionally that strategy's own vote, so `analyze()` now reads `dual_momentum`'s signal directly. Behavior-preserving by construction — confirmed by the acceptance test.
- **`calm_market_hold_threshold`/calm-hold** — both production instantiations (`Checker.__init__`, `BacktestEngine.__init__`) hardcoded this to `0.0`. Since `drawdown = abs(...)` is never negative, `drawdown < 0.0` is never true — dead at runtime in both live and backtest, confirmed by tracing every call site.
- **`min_hold_enabled`/`min_hold_days`** — hardcoded `False` in both production paths; the entire cadence-throttle branch, its `days_since_last_switch` market-context computation (`checker.py`'s `_trading_days_since` and `journal.last_switch_date()` caller, `backtest.py`'s loop-local mirror), and `_calculate_position_size` (defined but never called — position size was already hardcoded to `1.0`) all removed.
- **`dm_primary_enabled`, `use_dynamic_weights`, `use_position_sizing`, `use_regime_selection`, `routine_agreement_threshold`'s multi-strategy framing, accuracy tracking (`StrategyAccuracyRecord`, `update_accuracy`, `get_strategy_accuracies`, `_accuracy_history`)** — all removed; none had a live consumer independent of the weighted-vote path above.
- **`pilot_entry_enabled`** (`dual_momentum.py`) — hardcoded `False` in both `Checker` and `BacktestEngine`; the entire pilot-entry/scale-up/pilot-exit branch removed from `DualMomentumStrategy.generate_signal`.

**Kept, not touched** (live by default in both production paths, not on the kill list): `sideways_hold` (default `True` on the orchestrator; `Checker` doesn't override it), `correlation_guard` (default `True`). Execution/broker/notification/approval infra untouched.

## 2. Canonical config design

New module: `src/aurel2/config/canonical.py` — a frozen-dataclass `CANONICAL_CONFIG` singleton, not YAML (avoids a parse step and gets type-checking for free). Both `Checker.__init__` (`src/aurel2/live/checker.py`) and `BacktestEngine.__init__` (`src/aurel2/engine/backtest.py`) now build their `DualMomentumStrategy`/`AgentOrchestrator` instances from `CANONICAL_CONFIG.dual_momentum`/`CANONICAL_CONFIG.orchestrator` — the exact hardcoded values (`lookback_months=12`, `switch_threshold=0.02`, `cash_rate=0.0`, no-TLT universe via `EXCLUDED_ASSET_CLASSES`) that used to be independently duplicated in three places (`checker.py`, `backtest.py`'s `__init__`, and a third copy inside `backtest.py`'s `generate_comparison_json`) are now written once.

`config/default.yaml` no longer carries a stale `strategy`/`assets` block (it listed a 3-asset universe and `switch_threshold` that hadn't matched live behavior since before this cleanup) — it now only holds the settings genuinely still loaded (`broker`, `risk.transaction_cost_pct`, `logging`) plus a pointer comment to the canonical module.

`CANONICAL_CONFIG.overlay` is the structural section for Track C: tilt file path template, the three power caps (accelerate-entry cadence, lookback-override duration/frequency, force-defensive-contest cadence + universe), and majority-of-5 sampling params — structure only, matching the design doc's schema; values are placeholders for Track C to fill in.

**Known residual divergence, left unchanged (documented, not fixed):** `BacktestEngine`'s `correlation_guard`/`sideways_hold` constructor params default to `False`, while `Checker`'s canonical-config wiring leaves the orchestrator's own `True` defaults in effect. This predates the cleanup (visible in git history before this branch) and is exactly the kind of split Phase 2 non-negotiable #1 wants gone — but changing either default would move the acceptance-test replay's decisions, and the spec's acceptance bar takes precedence over fixing this now. Flagging it as a follow-up for whichever track owns further config unification.

## 3. Fidelity guard

`scripts/fidelity_guard.py` — replays the last N (default 10) live decision days from `data/{mode}/trade_journal.json` through `BacktestEngine`'s decision path (same canonical config as production) and diffs `(action, asset_symbol)` per day. Sends an ntfy alert (`src/aurel2/notifications/ntfy.py`) on ANY divergence; exits 1 on divergence, 0 otherwise.

```bash
python3 scripts/fidelity_guard.py --mode paper --days 10
python3 scripts/fidelity_guard.py --mode paper --days 10 --no-notify   # dry run, print only
```

Smoke-tested against a synthetic journal: correctly reported "no divergence" for a decision matching the replay, and correctly flagged + reported a fabricated wrong decision (exit code 1).

**Scheduling wiring added, not activated:** a `fidelity-guard` service in `docker/docker-compose.yml` under its own compose profile (mirrors the existing `dashboard` profile pattern — `docker compose up` never starts it without `--profile fidelity-guard`), plus a documented host-crontab entry in `docker/DEPLOY.md` (`0 6 * * 1 ... run --rm fidelity-guard`, Monday 06:00, ahead of the overlay's Monday pre-open cadence). No host has this cron entry installed — this is wiring only, per instructions not to activate anything.

## 4. Acceptance-test proof (non-negotiable)

**Method:** `scripts/fetch_acceptance_snapshot.py` pinned raw closes (`auto_adjust=False`) for the live 16-asset universe + SPY over 2026-02-11→2026-07-09 (with 400-day lookback buffer) to `data/acceptance/price_snapshot_2026-02-11_2026-07-09.csv` — one fixed snapshot used identically for both runs. `scripts/acceptance_replay.py` runs `BacktestEngine.run()` (monthly rebalance) over that window against the pinned snapshot and dumps a decision log (trades + per-rebalance holdings) to JSON.

- **Pre-cleanup baseline** (commit `aa662a5`, before any Track A edit): 3 trades — `2026-02-27 buy GLD`, `2026-04-30 sell GLD`, `2026-04-30 buy XLK`. Holdings: GLD (Feb–Mar), XLK (Apr–Jun). This matches the goal doc's independent narrative ("both aurel2 and live-trader currently hold XLK... holding XLK since May").
- **Post-cleanup** (after all deletions above): identical 3 trades, identical holdings sequence.
- **Diff proof:** `diff pre_cleanup.json post_cleanup_final.json` → **exit code 0, zero output.** Byte-identical.

## 5. Test results

| Stage | Result |
|---|---|
| Baseline (pre-cleanup) | 307 passed, 0 failed |
| Post-cleanup, first pass | 9 failed (all in tests exercising the exact removed code: `TestSelectBestAction`, `TestMinHoldThrottle`, one calm-hold assertion, one stale `pilot_entry_enabled` kwarg) |
| Post-cleanup, after test fixes | 223 passed, 0 failed |

Test count dropped from 307→223: 4 whole test files deleted alongside their modules (`test_mean_reversion.py`, `test_multi_timeframe.py`, `test_backtest_agent.py`, `test_mcp_server.py`), plus `TestSelectBestAction`/`TestMinHoldThrottle` classes removed from `test_agent.py` (tested methods/features that no longer exist), plus one stale kwarg fix in `test_checker_hold_reporting.py` and one rewritten assertion (`test_does_not_apply_in_bull` — its premise, "calm-hold catches this," no longer holds since calm-hold is deleted; rewritten to assert the correct new behavior).

## 6. Commits made

Run `git log --oneline track-a-cleanup` for the exact list — this report is committed as part of the final commit in the sequence. All commits are on `track-a-cleanup`, based on `autonomous-trading`, not pushed (per instructions — the orchestrator merges and pushes).

## 7. Scope notes

- Three `scripts/edge_decomposition/*.py` files (`a2_replay.py`, `ai_tilt_harness.py`, `regime_gate_matrix.py`) — cited as committed evidence in the AI-overlay design doc — passed the now-deleted `pilot_entry_enabled=False` kwarg and set now-deleted orchestrator attributes. Fixed minimally (dropped the dead kwarg/attribute-sets) so they stay importable/runnable; did not touch their actual research logic.
- One CLI command (`compare`) built mean_reversion/multi_timeframe signals purely as extra AI-advisor context. Stripped to `dual_momentum`-only, matching how the live advisor now actually receives signals (it never got 3-strategy context either, post-cleanup).
- ~20 older one-off `scripts/backtest_*.py` research scripts still reference `pilot_entry_enabled` or import the deleted strategies directly (some define their own inline copies). Left untouched — they are the "experiment remnants" the spec asked to purge conceptually; the modules they experimented on are gone, and running them now surfaces a clear `TypeError`/`ImportError` rather than silently drifting. Not part of the tested/live pipeline.
