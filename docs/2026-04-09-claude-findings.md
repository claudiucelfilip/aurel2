# Performance Research Findings — 2026-04-09

**Date:** 2026-04-09
**Continuation of:** [2026-04-02 findings](/root/aurel2/docs/2026-04-02-performance-research-findings.md)
**Handoff context:** [2026-04-09 Codex handoff](/root/aurel2/docs/2026-04-09-claude-handoff.md)

## Summary

Picked up from the Codex handoff with the goal of finding a refinement that
beats SPY across the 20y window without damaging the strong 5y/10y/15y
results. After running a deeper diagnostic, two ranking-refinement experiments,
and a clean out-of-sample validation, the conclusion is:

**The 20y lag is a structural artifact of carrying the 2008-2009 drag through
a 20-year window — not a knob we can turn with simple ranking, threshold, or
calm-hold refinements.**

The currently promoted candidate, `Robust_Quarterly_NO_TLT`, remains the
ceiling for this approach. No change to production is recommended.

## Selected candidate (unchanged)

`Robust_Quarterly_NO_TLT`. See
[robust_quarterly.py](/root/aurel2/src/aurel2/strategies/robust_quarterly.py)
and the wiring at
[checker.py](/root/aurel2/src/aurel2/live/checker.py),
[backtest.py](/root/aurel2/src/aurel2/engine/backtest.py),
[mcp/server.py](/root/aurel2/src/aurel2/mcp/server.py).

## Current performance vs SPY (anchored 2026-04-01)

| Window | Strategy CAGR | SPY CAGR | Alpha |
|---|---|---|---|
| 5y  | 32.8% | 13.5% | **+19.3%** |
| 10y | 21.7% | 12.7% | **+9.0%** |
| 15y | 16.6% | 13.4% | **+3.2%** |
| 20y | 10.0% | 10.3% | -0.3% |
| 1y  | 43.5% | 18.2% | **+25.4%** |

The 20y lag is concentrated in the 2005-2015 sub-window — once the GFC drops
out of the rolling window, the strategy is decisively ahead.

## What was investigated

### 1. Quarter-end ranking diagnostic (2013 / 2019 / 2023)

Built a per-quarter dump of the dual-momentum ranker
([diagnose_stale_defensive_ranking.py](/root/aurel2/scripts/diagnose_stale_defensive_ranking.py))
to see exactly what `Robust_Quarterly_NO_TLT`'s 12m ranker was looking at
during the failing windows.

This revealed two apparent failure modes in the dual-momentum thresholds:

- **2012-06-29** — held IEF, top equity (XLK) was tied at +0.11% gap, blocked
  by the inherited 5% defensive→equity threshold. Strategy stayed in bonds for
  one extra quarter while equity recovery had already started.
- **2018-12 → 2019-Q3** — switched from XLK to XLV at the bottom of the Q4
  2018 selloff, then the same-category 2% threshold protected XLV against XLK
  through Q1/Q2 2019 even as XLK caught up, then at Q3 2019 the pure 12m
  winner-take-all picked GLD (highest absolute momentum) instead of XLK
  (highest equity momentum).

This led to the first (incorrect) hypothesis: tighten the dual-momentum
thresholds and add an "equity-preferred" winner pick.

### 2. Ranking refinement experiment (rejected)

Built [backtest_ranking_refinement_experiment.py](/root/aurel2/scripts/backtest_ranking_refinement_experiment.py)
to test four variants:

- A. `switch_threshold` 0.02 → 0
- B. `defensive_to_equity_threshold` 0.05 → 0
- C. Prefer top equity sleeve when ≥ 0
- D. A + B + C combined

**Result:** A and B were inert — byte-identical to baseline on every standard
window. C and D actively damaged shorter windows (1y went 43.5% → -8.4%).

**Why A and B were inert** turned out to be the most important finding of the
day.

### 3. Orchestrator decision trace — the load-bearing finding

Built [trace_orchestrator_decisions.py](/root/aurel2/scripts/trace_orchestrator_decisions.py)
(used during investigation, not retained) to instrument
`AgentOrchestrator.analyze` and capture every decision across 2018-2020.

**The 2019 trap is not a dual-momentum failure — it is a calm-hold trap.**

The orchestrator's calm-market-hold logic
([orchestrator.py:639-662](/root/aurel2/src/aurel2/agent/orchestrator.py))
suppresses any DM-signaled switch when SPY drawdown is below 5% AND held
asset's 12m momentum is positive. Its escape hatch fires only when held
momentum goes negative. The trace showed:

| Quarter | Held | DM picks | DD | Action |
|---|---|---|---|---|
| 2018-12-31 | XLK | XLV | 14.0% | BUY XLV (DD high → calm-hold off) |
| 2019-03-29 | XLV | XLV | 2.4% | hold (DM held) |
| 2019-06-28 | XLV | XLV | 0.5% | hold (DM held) |
| 2019-09-30 | XLV | GLD | 1.3% | BUY GLD (XLV momentum went negative → escape fires) |
| **2019-12-31** | **GLD** | **XLK** | **0.3%** | **HOLD GLD — calm-hold blocks the fix** |

Crucially, the same calm-hold mechanism that traps the strategy in XLV/GLD in
2019 is what makes the 1y window +43.5%: at Q1 2025 the strategy entered GLD
and calm-hold rode the entire gold rally for four quarters. The strategy's
performance is anchored to whatever asset is selected when a calm window
starts.

This explains why the threshold tweaks (variants A/B) were inert: they would
only matter at moments when DM is allowed to switch, and calm-hold blocks
those moments. They explain why C/D were destructive: they changed DM's
winner pick at the moments calm-hold *did* let DM through, and those moments
are the wrong ones to override.

### 4. Calm-hold release experiment

Built [backtest_calm_hold_release_experiment.py](/root/aurel2/scripts/backtest_calm_hold_release_experiment.py)
to test refinements to the calm-hold release condition (without touching DM):

- `CalmOff` — disable calm-hold entirely
- `Gap10/15/20` — also release when winner_mom > held_mom + N
- `TimeBound4` — release after 4 consecutive calm-hold suppressions

**Result:** `Gap20` improved 20y CAGR from 10.0% → 12.0% (+2.0%) by firing
exactly 7 times across 20 years on defensible large-momentum-gap trades.
However, it cost 1.5-2.4% CAGR on 10y/5y because the GLD→XLK fire it forces
in 2019-Q4 is the same trade as the 2020-Q1 COVID hedge — gold inadvertently
protected the strategy through March 2020.

The 2019 GLD position is simultaneously the 2019 alpha drag *and* the COVID
shield. There is no version of "fix 2019" that doesn't undo the COVID
protection.

### 5. Out-of-sample validation — definitive failure

Built [oos_validate_calm_hold_release.py](/root/aurel2/scripts/oos_validate_calm_hold_release.py)
to test whether any gap-release threshold is robust across regimes. Split the
data 2005-2015 (in-sample, used for tuning) and 2015-2026 (held out, partially
contaminated since 2015-2026 had been inspected during this session).

| | Baseline | Gap5 (IS winner) | Gap20 |
|---|---|---|---|
| **IS** (2005-2015) CAGR | -1.6% | +6.4% (+8.0% vs base) | +2.7% (+4.3% vs base) |
| **OOS** (2015-2026) CAGR | **+21.7%** | +15.1% (-6.6% vs base) | +20.2% (-1.5% vs base) |

Three failure signatures:

1. **Hyperparameter unstable across regimes.** IS prefers Gap5 (loosest, most
   aggressive release). OOS prefers Gap40+ (effectively no rule).
2. **Baseline's negative alpha is concentrated in IS half.** In OOS, baseline
   beats SPY by +9.0%. The 20y -0.3% alpha is *not* spread across time — it
   is mechanically explained by the 2008-2009 GFC drag still being inside the
   20y window.
3. **The IS-chosen rule loses 6.6% CAGR OOS.** Classic overfit signature — fix
   one regime, break the next.

This is the same mechanism behind every refinement that has been rejected
across the past two months of research:
short-term confirmation, volatility-scaled sizing, crisis override,
stale-defensive override, broad early-switch, narrow stale-defensive override,
ranking refinement, calm-hold release. They all try to fix the 2005-2015
regime and damage the post-2015 regime.

## What this means

The 20y -0.3% alpha is not a tunable knob. It is a structural artifact of
carrying the 2008-2009 drawdown drag through a 20-year window. As the GFC
ages out of the window over the next 2-3 years, the 20y alpha will mechanically
improve without any change to the strategy.

This matches Claudiu's stated preference exactly:

> Treat GFC-style meltdown as once-in-a-lifetime. Do NOT over-optimize for
> crisis survival if it damages normal bull/bear markets.

The data now confirms that this isn't just a preference — it is a hard
constraint. Any rule that helps the GFC-affected window damages the post-2015
window in proportion.

## What did NOT work (this session)

| Approach | Why it failed |
|---|---|
| Tighter DM same-category threshold (0.02 → 0) | Inert: calm-hold blocks the resulting switches |
| Tighter DM defensive-to-equity threshold (0.05 → 0) | Inert: same reason |
| Equity-preferred winner pick in DM | Hurts shorter windows; misses non-equity rallies (e.g. 2025 gold) |
| `CalmOff` (disable calm-hold) | Trades 5y/10y/15y for marginal 20y improvement |
| Gap10/15 (lower release thresholds) | Too many fires — degrade 10y/5y materially |
| Gap20 | Looked promising on standard windows but **failed OOS** |
| Time-bounded release after N quarters | Mostly inert — rarely fires |
| Bond-only release (held=fixed_income, winner=equity) | Recognized as overfit before running |

## What worked (held over from prior research)

Already in production:

- Quarterly cadence (instead of monthly)
- Robust thresholds (broader universe, lower switch reluctance)
- Removal of TLT from selection

Nothing new from this session's experiments survived OOS validation.

## Recommendation

**Stop tuning Robust_Quarterly_NO_TLT for the 20y window. Accept it as the
ceiling for this approach.**

The strategy beats SPY by +3% to +25% across every window from 1y through
15y. The 20y -0.3% is structural and time-decaying. Continuing to refine it
will either fail OOS or break the windows the strategy currently dominates.

If a future research pass wants to revisit this, the productive search space
is **not** more refinements to dual-momentum or the orchestrator. It is
something genuinely different:

1. **Different lookback windows.** The 12m lookback is canonical but never
   genuinely re-tested at scale. A 9m or 6m lookback may behave very
   differently. (Note: prior research said 12m was best, but that was tested
   on the same overfit-prone in-sample data.)
2. **Equal-weighted top-N** instead of single-asset winner-take-all. Reduces
   the cost of being wrong on one ranker call.
3. **A second strategy run in parallel** via the orchestrator's multi-vote
   mode (currently disabled by `dm_primary_enabled=True`). The orchestrator
   already has the machinery; it's just not being used.
4. **A genuinely different ranking metric**: risk-adjusted momentum
   (Sharpe over lookback), trend-strength, or breadth.

Each of these is a structural change rather than a parameter tweak, which is
the consistent pattern of what has worked vs failed in this codebase.

Until then, the strategy is in a healthy state — promoted, beating SPY across
every meaningful window — and should be left alone.

## Code and data artifacts

**Kept (research record):**
- [scripts/diagnose_stale_defensive_ranking.py](/root/aurel2/scripts/diagnose_stale_defensive_ranking.py) — quarter-end ranker dump
- [scripts/backtest_calm_hold_release_experiment.py](/root/aurel2/scripts/backtest_calm_hold_release_experiment.py) — main calm-hold variant sweep
- [scripts/oos_validate_calm_hold_release.py](/root/aurel2/scripts/oos_validate_calm_hold_release.py) — reusable OOS validation pattern
- [data/calm_hold_release_experiment.json](/root/aurel2/data/calm_hold_release_experiment.json)
- [data/oos_calm_hold_release.json](/root/aurel2/data/oos_calm_hold_release.json)

**Discarded:** ranking_refinement and bond_escape experiments, one-off trace
scripts. They were either superseded or never run.

## Bottom line

`Robust_Quarterly_NO_TLT` stays. The 20y bar is structural and not worth
chasing further at the parameter level. Any future improvement attempt should
be a structural change (different lookback, different ranking metric, parallel
strategy) and must pass an OOS test before being considered.
