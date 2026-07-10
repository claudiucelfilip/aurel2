# Track E — Launch Prep (Graduation Rule + Weekly Scorecard): Build Report

**Date:** 2026-07-10
**Branch:** launch-prep (based on autonomous-trading, which contains all merged Phase 2 work)
**Spec:** `docs/plans/2026-07-10-ai-overlay-design.md` §Parallel run & graduation, `docs/plans/2026-07-09-edge-decomposition-goal.md` §Done means

## Summary

Wrote the exact graduation rule the parallel run will be judged against, built the automated weekly scorecard that evaluates it, and wired (but did not activate) three new scheduled jobs onto Track A's fidelity-guard compose profile pattern: weekly overlay runner, daily event-trigger check, weekly scorecard. Full test suite: 361 passing (41 new), zero failures. Nothing in this build touches live-trader (read-only), Dumbo, or `CANONICAL_CONFIG.overlay.enabled` (stays `False`).

## 1. `docs/plans/2026-07-10-graduation-rule.md`

Operationalizes every term in the design doc's one-line graduation template. The exact pass/fail line:

> after ≥6 weeks, if A2+overlay's Sharpe ≥ live-trader's Sharpe AND max DD ≤ 1.25× live-trader's max DD AND zero containment violations → go live with real money on Aurel2, subject to Claudiu's explicit sign-off even on a clean pass.

Key operational decisions, each with a stated reason in the doc itself:

- **Run start date is an unfilled placeholder** (`<TO BE STAMPED AT LAUNCH>`) — the run hasn't started; inventing one would corrupt every downstream number.
- **Each arm's equity source is nailed down to exact fields**, discovered by reading the actual data, not assumed:
  - A2+overlay: `data/paper/trade_journal.json`'s `account_value_after`/`_before`. Heartbeat (`~/.aurel2/heartbeat.json`) does **not** carry an account value (verified against `HeartbeatInfo` in `src/aurel2/monitor/health_checker.py` — it's connectivity/circuit-breaker telemetry only) — an earlier draft of this doc claimed otherwise and was corrected before finalizing.
  - live-trader: `/Users/claudiu/.openclaw/workspace/live-trader/trades.jsonl`'s `portfolio_value` field, read-only. Verified against the actual file: 123 lines, 20 fail to parse as JSON, only 60 of the remaining 103 carry `portfolio_value`, and the file is **not** chronologically ordered (`broker_reconcile` backfills interleave). The rule specifies parsing/sorting/gap-flagging rules that account for exactly this mess, not an idealized clean series.
  - A2-bare: `BacktestEngine(overlay_enabled=False)` run with `frequency="daily"` (the only frequency that yields a full business-day equity curve — `monthly`/`quarterly` are too sparse for a 6-week Sharpe).
  - QQQ: `CachedPriceProvider` daily closes, notional buy-and-hold.
- **Containment violation is defined as 4 concrete, automatically-checkable conditions** (write outside the 4 permitted files, cap exceeded, tilt applied despite failing schema validation, decision logic beyond the 3 defined powers) — cross-referenced against the actual cap functions in `src/aurel2/overlay/state.py` and validator in `src/aurel2/overlay/schema.py`, not restated from the design doc's prose.
- **Decision matrix** covers all 5 branches from the design doc plus the ambiguous/gap cases the one-liner didn't spell out: pass, overlay<A2-bare (remove + evaluate trend-vol fallback), containment violated (halt, overrides everything else), ambiguous (extend 2 weeks once, then treat a second ambiguous result as fail), live-trader data gap (compare vs QQQ+A2-bare only, flagged).
- **Sign-off**: real-money graduation always requires Claudiu's explicit approval, stated twice (once as its own section, once repeated in the decision matrix's "Pass" row) so it can't be read as automatable.

## 2. `scripts/weekly_scorecard.py` + `src/aurel2/scorecard/`

Split into a pure, tested metrics/arms layer and a thin CLI orchestration script, following this repo's TDD conventions (`src/aurel2/overlay/` has the same pure-logic/CLI split).

```
src/aurel2/scorecard/
  metrics.py    cumulative_return, max_drawdown, sharpe_ratio, alpha_vs_benchmark
                — pure functions over (date, value) series, no I/O
  arms.py       live_trader_daily_series, paper_journal_daily_series, overlay_accounting
                — parses real file formats (jsonl / journal.json) into daily series
scripts/
  weekly_scorecard.py   CLI: wires arms.py + metrics.py + BacktestEngine (A2-bare) +
                         CachedPriceProvider (QQQ) + NtfyNotifier, writes
                         data/{mode}/scorecard_history.jsonl + scorecard_latest.json
```

**Metrics (exact formulas, matching the graduation rule doc):**
- Sharpe: daily simple returns, `mean/std(ddof=1) * sqrt(252)`, 0% risk-free, `None` below 10 daily returns (not a noisy number from 3 data points), `0.0` for zero-volatility series (no divide-by-zero).
- Max DD: peak-to-trough on the full run-to-date daily series, positive fraction.
- Alpha: cumulative-return spread vs QQQ, `None` if either side has no data (never a false zero).

**Graceful degradation, verified by test and by a live smoke test:** any arm with zero data reports `{"gap": true, ...: null}` rather than crashing. Ran the script for real against this repo's actual (empty) `data/paper/` and the real read-only live-trader file — both correctly degraded to `DATA GAP`, while the A2-bare shadow replay and QQQ arms (which only need price data) computed real numbers end-to-end, including a live network price fetch. Output artifacts from that smoke run were deleted afterward — no fabricated history ships in this branch.

**Bug caught by the smoke test, fixed before it shipped:** `CachedPriceProvider.get_prices` returns a 400-day lookback buffer before the requested start date by design (other callers, like `fidelity_guard.py`, want that buffer for momentum calculations). The first version of `qqq_series` didn't filter back down to `[start, end]`, so `trading_days_elapsed` and QQQ's cumulative return were computed over ~a year instead of the actual run window. Fixed by explicitly filtering the provider's output before use — caught only because the script was actually run, not just unit-tested (the unit tests mock the provider and wouldn't have seen this).

**Overlay accounting** (`overlay_accounting` in `arms.py`) reads `overlay_activity` rows straight from the trade journal (same shape `src/aurel2/overlay/integration.py` writes: `power`/`status`/`reason`/`detail`) — applied/ignored counts plus the full per-decision activity list. Regime-view/sample-agreement history comes from `claw_shadow.jsonl`'s `parsed` field, filtered to the run window.

**CLI:** `python3 scripts/weekly_scorecard.py --mode paper --run-start YYYY-MM-DD [--no-notify]`.

**Tests:** `tests/test_scorecard_metrics.py` (17), `tests/test_scorecard_arms.py` (13), `tests/test_weekly_scorecard.py` (5) — 35 total, TDD'd for the pure metrics/arms layer (watched red before writing `metrics.py`/`arms.py`), tests-after for the thin CLI orchestration (`build_scorecard`, `format_ntfy_summary`) since that layer is glue over already-tested pieces, verified against real behavior via the smoke test above and via monkeypatched network/BacktestEngine calls in the orchestration tests.

## 3. Scheduling wiring (code/docs only, not activated)

New script: `scripts/run_weekly_overlay.py` — the production entrypoint the design doc's "weekly overlay runner" cron entry actually needs, since `src/aurel2/overlay/runner.py`'s existing CLI only accepts a pre-built context pack file, and nothing before this wired pack assembly to live data. Built from the same live decision path `checker.py` and `fidelity_guard.py` already use (`DualMomentumStrategy` + `CachedPriceProvider` + `CANONICAL_CONFIG`), so it stays consistent with the fidelity rule rather than reimplementing signal generation.

- `build_pack_for_today(mode, as_of)` — loads prices, current holding (from the trade journal's latest executed switch), computes the deterministic signal, calls `build_frozen_context_pack` (the frozen-v2 production entrypoint from Track B/Integration).
- `check_event_trigger(mode, as_of)` — computes the 3-day % move on the currently-held asset from cached prices and wraps `should_event_trigger` (Track C's pure predicate). CLI flag `--check-event-trigger-only`, exit code `3` if triggered (for a cron chain), `0` otherwise.
- Main CLI calls `run_overlay_decision` (Track C) to write + git-auto-commit the tilt.

Both verified to run end-to-end against real price data with no crash (pack assembly, event-trigger check with an empty journal). The CLI-shelling majority-of-5 sampling itself (`run_overlay_decision`) was not invoked in this verification — that's Track C's tested code, unchanged here.

**docker-compose.yml** — 3 new services on the same profile pattern as Track A's `fidelity-guard` (`docker compose up` never starts them without `--profile <name>`):

| Service | Profile | Entrypoint | Purpose |
|---|---|---|---|
| `overlay-runner` | `overlay-runner` | `run_weekly_overlay.py` | weekly tilt generation |
| `overlay-event-check` | `overlay-event-check` | `run_weekly_overlay.py --check-event-trigger-only` | daily >5%/3d check |
| `weekly-scorecard` | `weekly-scorecard` | `weekly_scorecard.py` | weekly referee |

`weekly-scorecard`'s `RUN_START` env var is required with no default (`${RUN_START:?...}`) — an invented default would silently corrupt every metric, so compose refuses to start without it. Config validated with `docker compose config` (both the unset-var error path and a populated run render cleanly).

**Documented (inactive) cron entries**, `docker/DEPLOY.md`:
```
# Weekly overlay — Monday 08:30 ET, ahead of pre-open:
30 8 * * 1 cd /opt/aurel2 && docker compose --profile overlay-runner run --rm overlay-runner

# Daily event-trigger check — every weekday 08:00 ET, chains into overlay-runner on trigger (exit 3):
0 8 * * 1-5 cd /opt/aurel2 && \
  docker compose --profile overlay-event-check run --rm overlay-event-check; \
  [ $? -eq 3 ] && docker compose --profile overlay-runner run --rm overlay-runner

# Weekly scorecard — Friday after close ET:
0 17 * * 5 cd /opt/aurel2 && docker compose --profile weekly-scorecard run --rm \
  -e RUN_START=<run-start-date> weekly-scorecard
```

**Known gap, documented rather than silently patched over:** `weekly-scorecard`'s live-trader arm reads an absolute host path (`/Users/claudiu/.openclaw/workspace/live-trader/trades.jsonl`) that isn't mounted into the container as configured. Flagged inline in `docker-compose.yml` and `DEPLOY.md` — whoever activates this on Dumbo needs to add that read-only bind mount, or the live-trader arm will report as a permanent data gap (which the graduation rule already handles gracefully, just not silently).

**Explicitly not activated:** no host crontab entry was installed anywhere, `CANONICAL_CONFIG.overlay.enabled` stays `False`, no container was started against a real host.

## Test results

```
361 passed in 2.10s
```

41 new tests across `test_scorecard_metrics.py` (17), `test_scorecard_arms.py` (13), `test_weekly_scorecard.py` (5), `test_run_weekly_overlay.py` (6). No pre-existing tests touched or broken.

## Files touched

- Added: `docs/plans/2026-07-10-graduation-rule.md`, `src/aurel2/scorecard/{__init__.py,metrics.py,arms.py}`, `scripts/weekly_scorecard.py`, `scripts/run_weekly_overlay.py`, `tests/test_scorecard_metrics.py`, `tests/test_scorecard_arms.py`, `tests/test_weekly_scorecard.py`, `tests/test_run_weekly_overlay.py`
- Modified: `docker/docker-compose.yml` (+3 profiled services), `docker/DEPLOY.md` (+2 sections)
- Untouched: `/Users/claudiu/.openclaw/workspace/live-trader/*` (read-only throughout), Dumbo, `CANONICAL_CONFIG.overlay.enabled`
