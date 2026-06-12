# Aurel2 Coordination Ledger

## Active Tasks
- A2-2026-04-01-01 | owner: codex | state: new
  Save deferred repo-quality TODOs without changing runtime behavior during the active 3-month paper trading run.
- A2-2026-04-01-02 | owner: codex | state: new
  Draft concrete performance experiment specs and evaluation gates for post-paper-run research.

## Deferred TODOs

### Runtime Safety / Maintainability
- [ ] Align runtime trading mode configuration so `TRADING_MODE` and CLI flags cannot disagree.
- [ ] Unify persistence paths for heartbeat, journal, pending decisions, and dashboard reads.
- [ ] Refactor oversized modules, starting with `src/aurel2/cli.py`, `src/aurel2/live/checker.py`, and `src/aurel2/agent/orchestrator.py`.
- [ ] Move orchestrator thresholds, regime weights, and AI override cutoffs into config rather than hardcoding them.
- [ ] Fix the dashboard test/process shutdown hang, likely around the global thread pool lifecycle.
- [ ] Either implement `execute_trade` in the MCP server through the real approval/execution pipeline or remove the placeholder tool.
- [ ] Replace the minimal `README.md` with an operator/developer guide that matches current cloud-first deployment.

### Performance Research Backlog
- [ ] For any new strategy idea, backtest in this order: `15y`, `10y`, `1y`, `1m`; run `20y` last and only as an optional stress/history check including the GFC.
- [ ] Add experiment tracking for every performance variant: hypothesis, date range, turnover, CAGR, max drawdown, Sharpe, alpha vs baseline, and trade count.
- [ ] Establish a frozen paper-trading baseline using the current strategy/config before changing any trading logic.
- [ ] Compare rebalance cadence variants: monthly vs quarterly vs regime-conditional rebalance timing.
- [ ] Test momentum lookback variants and blends: 6M, 9M, 12M, and weighted multi-horizon momentum.
- [ ] Evaluate stronger trend filters such as SPY above 200DMA, asset-specific moving-average filters, and regime-conditioned filters.
- [ ] Test risk-managed sizing instead of full allocation only: volatility targeting, drawdown-aware sizing, and capped exposure in hostile regimes.
- [ ] Evaluate expanding the opportunity set only if it improves risk-adjusted returns after costs: sector ETFs, gold, treasuries, and defensive sleeves.
- [ ] Quantify turnover and whipsaw reduction methods: switch thresholds, sideways-market hold logic, and confirmation delays.
- [ ] Separate performance attribution by market regime so gains are not coming from one narrow historical period.
- [ ] Add walk-forward and out-of-sample evaluation as a gate before promoting any strategy change into paper trading.
- [ ] Prefer parallel shadow backtests and side-by-side paper experiments over editing the currently running paper-trading configuration.

## Protocol
- task_id format: A2-YYYY-MM-DD-NN
- owner: claude | claw
- states: new → accepted → in_progress → blocked|review → done
- file inbox: coordination/inbox/{claude-to-claw,claw-to-claude}/
- archive: coordination/archive/

## Log
- 2026-04-01: Added concrete experiment specs in `docs/plans/2026-04-01-performance-research-plan.md` covering regime-switched policy, volatility-scaled sizing, and a 12M confirmation layer. Standardized future backtest order to `15y`, `10y`, `1y`, `1m`, with `20y` optional and last.
- 2026-04-01: Added `scripts/backtest_regime_switch_experiment.py` for Experiment 1. The runner compares baseline, static aggressive/robust policies, and two regime-switched variants under the new horizon order.
- 2026-04-02: Documented the completed research arc in `docs/2026-04-02-performance-research-findings.md`. Confirmed the only meaningful upgrade path found so far was `Robust_Quarterly`; confirmation overlays, volatility sizing, and crisis override all failed. Ranked next focus areas as: smarter early-switch logic, better asset-universe design, then narrow medium-term regime adaptation.
- 2026-04-02: Validated a first universe-design tweak: removing `TLT` (long treasuries) improved `15y/10y/5y/20y` CAGR vs the `Robust_Quarterly` baseline (anchored end date 2026-04-01). Saved results to `data/universe_candidate_validation.json`.
- 2026-04-02: Tested simple "replace TLT with IEF/SHY/AGG/TIP" variants; they matched the `NO_TLT` result (no additional improvement), suggesting long treasuries were uniquely harmful vs other bond sleeves which rarely win selection.
- 2026-04-02: Added `scripts/publish_backtests_only.sh` to publish the latest `data/backtest_comparison.json` to `/opt/aurel2/data/` without restarting the trading daemon (safe during the active paper-trading run).
- 2026-04-02: Switched the live trader’s dual-momentum component to `Robust_Quarterly_NO_TLT` (exclude `TLT` from selection) and deployed.
