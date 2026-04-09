# Performance Research Findings

**Date:** 2026-04-02

## Summary

The performance work produced one meaningful upgrade and several failed refinements.

What worked:
- Moving from the old paper baseline to the broader, lower-threshold robust strategy.
- Adding quarterly cadence on top of that robust strategy.

What did not work:
- Short-term confirmation overlays.
- Stronger short-term override logic.
- Volatility-scaled sizing.
- Crisis-override / early defensive escape-hatch logic.

The current production candidate is therefore:
- `Robust_Quarterly_NO_TLT` (Robust Quarterly with `TLT` excluded from selection)

This is the best practical result found so far, but it does **not** yet beat `SPY` cleanly across every important window.

## Selected Candidate

The selected candidate combines:
- broad asset universe
- `12m` momentum
- `2%` switch threshold
- `0%` cash hurdle
- pilot entry disabled
- quarterly cadence
- plus: **exclude long-duration treasuries** (`TLT`) from selection

Implementation:
- [robust_quarterly.py](/root/aurel2/src/aurel2/strategies/robust_quarterly.py)

Live wiring:
- [checker.py](/root/aurel2/src/aurel2/live/checker.py)
- [server.py](/root/aurel2/src/aurel2/mcp/server.py)

Dashboard sync:
- [backtest.py](/root/aurel2/src/aurel2/engine/backtest.py)
- [dashboard.html](/root/aurel2/src/aurel2/dashboard/templates/dashboard.html)

Measurement note:
- backtest snapshots now preserve the held symbol / asset class, so offensive vs defensive vs cash attribution can be measured directly instead of being reconstructed from trades

## What Worked

### 1. Robust static policy

The robust long-horizon policy clearly improved the old baseline and outperformed the more aggressive recent-cycle mode.

Core lesson:
- broader opportunity set plus lower switch reluctance helped more than adding new filters

### 2. Quarterly cadence

Quarterly cadence was the only refinement that materially improved results without adding much complexity.

Why it likely helped:
- reduced whipsaw
- preserved medium-horizon trend capture
- avoided the repeated churn introduced by monthly or reactive overlays

Net result:
- `Robust_Quarterly` became the best practical promotion candidate

## What Failed

### 1. Confirmation layer around 12M

Mild confirmation variants changed nothing.
Stronger confirmation / override variants changed trades, but failed to improve the recent windows and often hurt longer windows.

Lesson:
- shorter-horizon information is not automatically additive here
- small tie-breakers were too weak to matter
- stronger overrides degraded the strategy

### 2. Volatility-scaled sizing

Volatility targeting and stress clamps reduced returns more than they improved the equity curve.

Lesson:
- generic exposure reduction is not the right lever if the goal is to beat `SPY`
- this system loses more from muted upside than it gains from smoother risk

### 3. Crisis override

A narrow off-cycle crisis escape-hatch was tested on top of `Robust_Quarterly`.
It did not improve the primary windows and it badly broke the recent-year result.

Lesson:
- a blunt defensive override is too coarse
- the problem is not simply "be more defensive sooner"

Reference:
- [robust_quarterly_crisis.py](/root/aurel2/src/aurel2/strategies/robust_quarterly_crisis.py)
- [backtest_robust_crisis_override_experiment.py](/root/aurel2/scripts/backtest_robust_crisis_override_experiment.py)
- [robust_crisis_override_experiment.json](/root/aurel2/data/robust_crisis_override_experiment.json)

## Current Read Versus SPY

With the dashboard now synced to anchored research windows, the current picture is:
- `5y`: Aurel2 ahead of `SPY`
- `10y`: Aurel2 ahead of `SPY`
- `15y`: Aurel2 slightly behind `SPY`
- `20y`: Aurel2 behind `SPY`
- `1y`: Aurel2 ahead, but not reliable enough to treat as expectation

Implication:
- Aurel2 has improved meaningfully
- it has not yet reached a robust "beats SPY across core windows" state

## Recommended Next Focus

Ranked by likely impact:

### 1. Smarter early-switch logic

This is the highest-upside next area.

Reason:
- quarterly cadence helped, which suggests slower trading is useful in normal markets
- the remaining weakness is likely at regime transitions and leadership breaks
- the best next change is not "more monthly trading", but selective early action when the existing quarterly policy is clearly stale

What to target:
- preserve quarterly by default
- allow early action only on strong leadership breaks, not broad crisis heuristics
- focus on switching logic, not blanket defensiveness

### 2. Better asset-universe design

This is the second most promising area.

Reason:
- the jump from the old baseline to the robust policy already showed that universe design matters
- if the strategy still trails `SPY` over `15y` and `20y`, part of the issue may be that the current sleeves are not the best set for non-US, crisis, or transition periods

What to target:
- improve offensive/defensive sleeves rather than simply adding more tickers
- test replacements and removals, not just expansion

#### Quick Result: Remove Long Treasuries (TLT)

A targeted universe tweak was validated:
- remove `BONDS_TREASURY` (`TLT`) from the dual-momentum universe (keep other bond sleeves intact)

Effect (anchored end date 2026-04-01):
- improved `15y`, `10y`, `5y`, and `20y` CAGR versus the `Robust_Quarterly` baseline
- did **not** change `1y` or `1m` in this sample (the strategy didn’t select `TLT` in those windows)

Reference:
- [backtest_universe_candidate_validation.py](/root/aurel2/scripts/backtest_universe_candidate_validation.py)
- [universe_candidate_validation.json](/root/aurel2/data/universe_candidate_validation.json)

Follow-up (replacement attempt):
- swapping `TLT` for other existing bond sleeves (`IEF`, `SHY`, `AGG`, `TIP`) produced the **same** result as simply removing it in this setup
- interpretation: `TLT` was the only bond sleeve that ever “won” the momentum race often enough to matter; other bond sleeves rarely became the top-ranked asset, so they don’t act as a replacement

Live rollout:
- on **2026-04-02**, the trader was switched to `Robust_Quarterly_NO_TLT` (mid-paper-run)

### 3. Narrow medium-term regime adaptation

This remains interesting, but it is third.

Reason:
- the broader regime-switched and crisis-override attempts both underperformed
- that means regime adaptation is easy to overdo
- if revisited, it should be narrow and local, not a full policy-switching framework

What to target:
- regime logic should modify only one part of the decision stack
- avoid replacing the full policy by regime

## Conclusion

The research result is not "add more complexity."

The strongest finding so far is:
- simple robust policy changes helped
- quarterly cadence helped
- most reactive overlays failed

So the next best attempt should be:
- `Robust_Quarterly_NO_TLT` as the baseline
- then test a **smarter early-switch rule** before testing broader regime machinery again
