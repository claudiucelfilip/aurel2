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
