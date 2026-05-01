# Aurel2 handoff for Claude Code

Date: 2026-04-09
Owner context: Claudiu
Repo: /root/aurel2

## Current promoted paper candidate
- `Robust_Quarterly_NO_TLT`
- This is not just research-only, it is wired into:
  - `src/aurel2/engine/backtest.py`
  - `src/aurel2/live/checker.py`
  - `src/aurel2/mcp/server.py`

## Current validated status
Using `data/backtest_comparison.json` and current repo state:
- 20y: 10.0% CAGR vs SPY 10.3% (still loses)
- 15y: 16.6% vs 13.4%
- 10y: 21.7% vs 12.7%
- 5y: 32.8% vs 13.5%
- 1y: ahead, but low-confidence window

Conclusion:
- Best current candidate is strong on 15y/10y/5y
- Still does not clear the bar of beating SPY across all major windows
- Main unresolved issue is 20y lag

## Important user preference / constraint
Claudiu explicitly said:
- Treat GFC-style meltdown as once-in-a-lifetime
- Do NOT over-optimize for crisis survival if it damages normal bull/bear markets
- Preference is higher gains and fewer missed opportunities in normal conditions, even if that means less special handling for global financial meltdowns

This constraint matters. Do not propose “fixes” that mainly improve 2008/2009 at the cost of 10y/5y / normal-market upside capture.

## What we investigated

### 1. Universe tweak: remove TLT
Validated and already promoted.
Results from `data/universe_candidate_validation.json`:
- 20y baseline: 8.9%
- 20y NO_TLT: 10.0%
- also improved 15y / 10y / 5y

Interpretation:
- Removing long-duration treasuries was a real improvement, not a crisis-overfit trick.
- Replacing TLT with IEF/SHY/AGG/TIP produced effectively the same result as simply removing it.

### 2. Broad early-switch experiments
See `data/robust_early_switch_experiment.json`.

Headline:
- Early-switch variants improved 20y dramatically:
  - 12.8% vs SPY 10.3%
- BUT they degraded the intervals that matter more:
  - 15y: 16.6% -> 16.1%
  - 10y: 21.7% -> 18.6%
  - 5y: 32.8% -> 24.4%

Interpretation:
- These variants are the wrong trade.
- They look like classic overreaction / crisis-recovery optimization.
- Reject this family as a promotion path.

### 3. Calm-hold experiment
See `data/calm_hold_experiment.json`.

Disabling calm hold did NOT help enough and often made results worse.
- `NO_TLT_calm_hold_OFF` underperformed `NO_TLT_calm_hold_5pct` on 15y / 10y / 5y.

Interpretation:
- “Let it trade more freely” is not the missing ingredient.

### 4. Forensic analysis of 20y lag
We broke down the worst lag years for `Robust_Quarterly_NO_TLT`.
Worst annual alpha drag vs SPY:
- 2009: -23.2
- 2007: -22.1
- 2013: -20.3
- 2019: -20.2
- 2011: -12.4
- 2012: -12.1

Important takeaway:
- The problem is NOT only GFC.
- 2013 and 2019 are key non-GFC underperformance years.
- This supports the idea that normal-market upside capture is the bigger opportunity.

### 5. Holdings during worst non-GFC lag periods
Observed holdings from a direct rerun with snapshot attribution:
- 2013-03-29 -> IEF
- 2013-06-28 -> XLF
- 2013-09-30 -> XLF
- 2013-12-31 -> XLF
- 2019-03-29 -> XLV
- 2019-06-28 -> XLV
- 2019-09-30 -> GLD
- 2019-12-31 -> GLD
- 2023-03-31 -> XLE
- 2023-06-30 -> XLE
- 2023-09-29 -> XLK
- 2023-12-29 -> XLK

Interpretation:
- 2013: too defensive too long at the start, then partial recovery through XLF
- 2019: got stuck in defensive winners (XLV, GLD) while tech/risk assets ripped
- 2023: was late, but eventually corrected to XLK; less severe than 2013/2019

Core failure mode hypothesis:
- The strategy can get stuck in stale defensive winners and admit risk-on leadership too slowly.

### 6. Narrow stale-defensive override experiment
Created script:
- `scripts/backtest_stale_defensive_override_experiment.py`
Output:
- `data/stale_defensive_override_experiment.json`

This experiment tried a narrow off-cycle override only when:
- current holding is defensive (gold / bonds / healthcare)
- an offensive leader clearly dominates by threshold

Result: failed badly.
- 2013_focus: baseline +10.7% vs override -0.8%
- 2019_focus: baseline +4.0% vs override -5.4%
- 2023_focus: baseline +21.3% vs override +20.0% with much worse drawdown
- 10y: 21.7% -> 19.2%
- 5y: 32.8% -> 28.6%

All tested parameter variants collapsed to effectively the same bad outcome.

Interpretation:
- Diagnosis may be directionally right, but this proposed fix is wrong.
- Naive off-cycle jumps from defensive winners to offensive leaders introduce bad timing / churn and degrade core windows.
- Reject this family too.

## Code / repo changes already made today
Committed:
- `9f8c0f9` `fix: clarify hold reporting and snapshot attribution`

What that commit did:
- clarified HOLD reporting so current holding and candidate asset are not conflated
- added journal fields for decision symbol vs current holding symbol
- added snapshot attribution fields (`holding_symbol`, `holding_asset_class`)
- added tests for reporting behavior

This commit is real and should stay.

## Important non-committed artifacts currently present
Uncommitted / likely Claude-local tooling residue in repo:
- `CLAUDE.md` modified
- `.claude/settings.local.json` modified
- `.codex` untracked

Experiment artifacts currently uncommitted:
- `scripts/backtest_stale_defensive_override_experiment.py`
- `data/stale_defensive_override_experiment.json`

Decide whether to keep these as documentation/research artifacts or discard them.
My recommendation:
- keep the JSON result as evidence if useful
- keep the script only if you want a record of rejected experiments
- do NOT mix Claude/Codex tooling residue into strategy commits

## Recommended next steps for Claude Code
Do NOT retry broad early-switch or stale-defensive off-cycle overrides. Those paths have already failed in meaningful ways.

More promising next search space:

### A. Ranking / sleeve design inside normal risk-on markets
This is the highest-priority area.
Focus on why defensive winners persist too long in trailing ranking, and whether the offensive sleeves are too coarse.

Candidate directions:
1. Improve offensive sleeve design / representation
   - e.g. re-think whether current sleeves (`SPY`, `XLK`, `XLF`, `EFA`, `EEM`, etc.) are the right set
   - test whether a different offensive sleeve mix improves 2013/2019 without harming 10y/5y
2. Narrow ranking/tie-break refinement
   - not broad switching logic
   - use small ranking improvements that prefer stronger persistent equity leadership when 12m leaders are close
   - must preserve quarterly simplicity as much as possible
3. Inspect defensive sleeve persistence
   - why XLV/GLD/IEF remain winners so long in certain windows
   - whether 12m lookback alone is over-rewarding stale defensive leaders in post-risk-off recovery phases

### B. If testing anything reactive, keep it very constrained
Only entertain this if ranking/sleeve work fails.
Any reactive idea must pass this bar:
- no material degradation on 15y / 10y / 5y
- no obvious drawdown blowout
- must be justified as normal-market upside capture, not crisis-special handling

## Recommended immediate task for Claude
1. Read:
- `docs/2026-04-02-performance-research-findings.md`
- `data/backtest_comparison.json`
- `data/universe_candidate_validation.json`
- `data/robust_early_switch_experiment.json`
- `data/calm_hold_experiment.json`
- `data/stale_defensive_override_experiment.json`

2. Confirm current best candidate remains `Robust_Quarterly_NO_TLT`

3. Start a new research pass focused on:
- ranking / sleeve design improvements for normal-market upside capture
- explicitly avoiding crisis-overfit fixes

4. Prefer experiments that are:
- simple
- explainable
- compatible with the user preference above

## Bottom line
Current state is not blocked, but the obvious “fix the stale defensive lag with off-cycle overrides” path has now been tested twice in different forms and should be considered a dead end unless there is a radically different design.

The baton pass should start from:
- keep `Robust_Quarterly_NO_TLT`
- stop trying broad switching hacks
- search in ranking / sleeve design instead
