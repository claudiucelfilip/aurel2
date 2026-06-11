# 2026-06-11 Min-Hold Regression Check

## Context

After regenerating dashboard backtests through 2026-06-11, the new baseline was
materially worse than the previous 2026-05-07 baseline on overlapping history.
The concern was not staleness; it was whether the current algorithm had regressed.

## Fixed-End Check

All runs below end on 2026-05-07 to remove shifted-window noise.

| Window | Previous baseline | Current with min-hold | Current with min-hold disabled |
| --- | ---: | ---: | ---: |
| 5y return | 430.9% | 348.4% | 429.8% |
| 5y CAGR | 30.1% | 26.7% | 30.0% |
| 5y trades | 29 | 21 | 29 |
| 1y return | 65.0% | 64.6% | 64.6% |
| 1m return | 12.1% | -5.4% | 11.9% |

## Decision

Disable the 21-trading-day min-hold throttle by default and in live/backtest
construction. Keep the code path available as an opt-in research feature.

Keep the June 2026 autonomy fixes that are not implicated by this ablation:

- multi-timeframe uses the same no-TLT universe as dual momentum
- 2-of-3 strategy agreement is routine
- backtests continue to mirror live configuration

## Rationale

The min-hold throttle suppressed profitable rotations and violated the recent
window guardrails documented in `docs/alpha-research-findings.md`. The restored
no-min-hold 5y and 1m windows match the previous baseline closely while keeping
the live/autonomy fixes intact.

## Deployed Baseline After Fix

Commit `1fe31dc` disabled the min-hold throttle by default, regenerated the
dashboard baseline through 2026-06-11, and deployed the source change to
production.

Dashboard metadata after the fix:

- `strategy`: `DM_NO_TLT_daily_no_calm_no_minhold`
- `min_hold_enabled`: `false`
- `generated_at`: `2026-06-11`

| Window | Return | CAGR | Trades |
| --- | ---: | ---: | ---: |
| 20y | 1072.6% | 12.2% | 112 |
| 15y | 996.9% | 15.7% | 84 |
| 10y | 593.4% | 18.4% | 56 |
| 5y | 464.4% | 30.8% | 29 |
| 1y | 67.4% | 65.2% | 2 |
| 1m | 3.9% | 41.0% | 2 |

Compared with the pre-regression 2026-05-07 baseline, the long-window trade
counts are restored exactly: 20y 112, 15y 84, 10y 56, 5y 29. The 5y return is
now higher than the older 430.9% result because the new baseline includes the
additional market period through 2026-06-11. The 1m window is lower than the
older May-window result because the rolling month now includes the recent XLK
pullback, but it is no longer the broken min-hold result of -11.1%.

## Claw And Paper-Trade Impact

No evidence was found that a Claw experiment directly changed Aurel2's production
algorithm. The Aurel2-related Claw artifact was the PnL divergence alert stream,
which reported that Aurel2 had 5 losing paper sessions out of the last 5 on
2026-06-11. That alert described paper-account drawdown; it did not modify
trade-selection code.

The production regression traces to the June 8 Aurel2 commit that bundled a good
autonomy fix with the bad default min-hold throttle. The good parts kept are:

- multi-timeframe uses the same no-TLT universe as dual momentum
- 2-of-3 strategy agreement is routine
- backtests mirror the live configuration

The current paper test decisions do not appear to have been changed by the bad
min-hold rule. The paper journal shows:

- last executed paper buy: XLK on 2026-05-07
- latest checked decision: hold XLK on 2026-06-10
- account value at latest checked decision: 1067.20 from a 1000 start
- no pending paper decisions

After 2026-06-08, the recorded paper decisions were ordinary hold-XLK decisions.
Dual momentum, mean reversion, and multi-timeframe all supported holding XLK on
the latest checked entry. The normal no-min-hold path would also have held XLK,
so the bad rule mainly corrupted baseline/backtest behavior and future switch
eligibility, not the already-recorded current paper decisions.
