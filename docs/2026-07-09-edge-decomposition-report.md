# Edge Decomposition Report — where the trading edge actually comes from

**Date:** 2026-07-09
**Goal:** `docs/plans/2026-07-09-edge-decomposition-goal.md`
**Window:** 2026-02-11 → 2026-07-08 (live-trader's lifetime)
**Artifacts:** `data/edge_decomposition/*.json`, `scripts/edge_decomposition/*.py`

## Headline numbers

Full window, all equity normalized to 100:

| Arm | Return | Max DD | Trades |
|---|---|---|---|
| **LT-actual** (algo + AI) | **+19.1%** | **−7.0%** | 38 |
| QQQ buy-and-hold | +16.0% | −9.4% | — |
| A2-replay (Aurel2 live code) | +12.2% | −18.2% | 2 |
| LT-core (LT algo alone, tilts zeroed) | +10.2% | −8.9% | 7 |
| SPY buy-and-hold | +8.6% | −8.6% | — |
| A2-actual (paper, exists since 05-07 only) | +7.0% | −10.9% | 2 |

LT-actual final equity reconstruction (+18.99%) validated against 58 journal snapshots (mean abs diff ~0.6%) and against the live Alpaca account pulled read-only on 2026-07-09: **$120.78**, all in XLK.

## The three decision numbers

1. **(LT-actual − LT-core) = the AI layer's worth: +8.9pts full window, +12.8pts in era 2.** The hypothesis holds — the AI layer is real alpha, and it is the only thing in the study that beat QQQ.
2. **(LT-core vs A2-replay) = which core is better: era-dependent, neither dominates.** Full window A2 wins (+12.2 vs +10.2), but era 1 LT-core won huge (+12.0 vs −8.0) and era 2 A2 won huge (+19.9 vs +2.2).
3. **(A2-actual vs A2-replay) = fidelity: clean.** Zero decision divergences over the entire real overlap (2026-05-07 → 2026-07-08). The replay drove the actual production `BacktestEngine` + orchestrator classes. No backtest-vs-live bug exists in this window. Caveat: overlap is 2 months / 2 trades.

## The era discovery

live-trader's deterministic engine (`daily_runner.py`) **did not exist until ~2026-04-01**. Journal entries before then carry free-text LLM reasoning — Feb–Mar was pure AI discretion. Claw itself converged on "deterministic core + AI tilt" after two mediocre discretionary months.

| Era | LT-actual | LT-core | A2-replay | SPY | QQQ |
|---|---|---|---|---|---|
| Era 1 (02-11→03-31, choppy) | +6.7% | +12.0% | **−8.0%** | −5.8% | −5.7% |
| Era 2 (04-01→07-08, bull) | +15.0% | +2.2% | **+19.9%** | +14.4% | +21.6% |

Readings:
- **In each era a different core won.** Fast rotation (LT-core) won the chop; slow momentum (A2) won the bull and got destroyed in the chop (−8% while the market fell −5.8%, on its way to its −18.2% max DD).
- **The AI's measurable value is regime robustness, not raw alpha.** LT-actual was never the era winner but was solidly positive in both eras — that consistency (+6.7, +15.0, max DD only −7%) is what made it the only arm to beat QQQ over the full window, with lower drawdown.
- **The "embarrassing hold" was right.** Aurel2's code did +19.9% in era 2 and beat SPY/QQQ 3x over in its real paper window (+7.0% vs ~+2.3% from 05-07). The perceived missed opportunity does not survive the numbers. Aurel2's real weakness is era-1-style chop (deep drawdowns), not the bull-market holds.

## Attribution detail (LT-actual, 38 trades)

`algo`=12, `manual`=16, `unknown`=10.
- The six clean algo rotations: GLD→EEM (02-20), EEM→XLE (02-27), XLE→GLD (03-02), GLD→XLE (03-11), XLE→EEM (04-10), EEM→QQQ (04-23).
- **The entire Jun 12 – Jul 7 tail is manual** (SELL QQQ/BUY IWM, SELL IWM, BUY XLK) — broker-reconcile backfills with zero journaled reasoning; XLK is outside the 8-symbol watchlist.
- Tilt attribution is impossible historically: `discretionary_tilt.json` is not versioned (0 hits in 132 commits); only the current snapshot exists.

## Findings about live-trader itself (report-only; touching it is a non-goal)

1. **Wash-trade anomaly (real money):** 5 same-day round-trip pairs (03-19, 03-20, 03-25, 04-01, 04-02) where an unjournaled GLD leg was placed and reversed the same afternoon — real order IDs, real fills, while the agent's own logs show it believed it held XLE continuously. Looks like a duplicate-order bug burning spread, not a decision. Owner (Claw) should investigate.
2. **Observability decay:** portfolio_value logging stopped 2026-05-26; all trades since Jun 12 have no decision narrative; tilt state is unversioned. The system's auditability is degrading exactly as its manual-intervention share rises.

## Caveats

- ~5 months, one macro backdrop, 38 trades (16 manual, 10 unknown) — directional evidence, not proof.
- LT-core replay uses yfinance closes and trade-at-close convention; live ran intraday on Alpaca IEX data.
- Era 1 "LT-core" is counterfactual — the engine didn't exist yet.

## Verdict → Branch A, with Branch C's test folded in

Per the pre-committed branch table:

- H confirmed: **the AI layer is the alpha that beats the benchmark** — Branch A: build an AI discretionary overlay on Aurel2.
- But the AI's value was measured rescuing a *weak* core. Whether it adds value on top of Aurel2's *strong* (in bulls) core is untested. So Phase 2 must run the AI-matched comparison (Branch C's condition): **A2-core+AI vs LT-core+same-AI**, in the parallel paper run against live-trader.
- The overlay's highest-leverage job, per the era table: protect A2's core from era-1-style chop (drawdown control / regime awareness), not second-guess its bull-market holds.
- Open Phase 2 decision (by design): which agent/model runs the overlay — Claw (proven, same as live-trader), Claude, or the aurel3 GPT path.

## Done criteria progress

1. ✅ Decomposition report with attribution table, divergence/era analysis, stated verdict + confidence (directional, small-sample).
2. ⬜ Branch A build (AI overlay design → implementation).
3. ⬜ Parallel paper run vs live-trader with weekly scorecard.
4. ⬜ Written graduation rule.
