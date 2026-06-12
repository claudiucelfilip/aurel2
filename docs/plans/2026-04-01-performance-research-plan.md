# Performance Research Plan

**Date:** 2026-04-01

**Context:** Aurel2 is mid-way through an active 3-month paper-trading run. Do not change the live paper-trading configuration during this research. New ideas should be evaluated in shadow/backtest mode first.

**Backtest order for all new ideas:**
1. `15y` primary decision window
2. `10y`
3. `1y`
4. `1m`
5. `20y` optional, last, and treated mainly as a GFC stress/history check

**Baseline principle:** Every candidate must be compared against the current paper-trading configuration, not just against SPY.

---

## Objective

Find the next research direction most likely to improve returns without blindly increasing complexity or disrupting the active paper run.

The prior documentation suggests:
- Simple high-threshold offensive behavior worked best in recent-cycle tests.
- Lower-threshold, broader-universe behavior improved long-window performance.
- Canary/VIX/SMA-style filters were generally harmful or too regime-dependent.
- One meaningful untested area remains: position sizing overlays.

This plan turns those observations into three concrete experiment tracks.

---

## Shared Evaluation Protocol

For every experiment:
- Run the same candidate set on `15y`, `10y`, `1y`, `1m`, and optionally `20y`.
- Record:
  - CAGR
  - Total return alpha vs current paper-trading baseline
  - Max drawdown
  - Sharpe
  - Trade count
  - Turnover if available
- Add regime attribution when relevant.

### Default pass/fail gates

These are the default gates unless an experiment defines stricter ones:

1. `15y` must improve total-return alpha vs current baseline.
2. `10y` alpha vs current baseline must remain positive.
3. `1y` alpha regression vs current baseline must be no worse than `15pp`.
4. `1m` alpha regression vs current baseline must be no worse than `10pp`.
5. Max drawdown must not worsen by more than `3pp` on `15y`.
6. Trade count increase should stay below `40%` on `15y`.
7. `20y` is not a primary gate, but if run it should be reviewed as a stress-history sanity check before any promotion.

---

## Experiment 1: Regime-Switched Policy

**Priority:** Highest

### Hypothesis

The repo already contains evidence for two different winning personalities:
- a recent-cycle aggressive/offensive mode
- a longer-window robust mode

A regime-switched policy may outperform either static policy by using the aggressive mode in favorable conditions and the robust mode in stressed or transitional conditions.

### Why this is worth doing

- It builds directly on prior research instead of inventing a brand-new strategy family.
- It targets the exact conflict in the current docs: recent-period outperformance vs long-window robustness.
- It is a cleaner next step than adding more filters.

### Candidate structure

Define two static modes first:

1. `AggressiveRecent`
- Higher switch reluctance
- Simpler offensive posture
- Minimal or no defensive behavior

2. `RobustLongHorizon`
- Lower same-category threshold
- Broader universe
- Existing asymmetric equity/defensive behavior preserved

Then define switching rules using a small regime grid:
- `Bull`: use `AggressiveRecent`
- `Sideways`: test both `AggressiveRecent` and `RobustLongHorizon`
- `Bear`: use `RobustLongHorizon`

### Parameter grid

Keep the grid small:
- Regime detector:
  - drawdown only
  - SMA only
  - existing consensus regime approach
- Policy map:
  - bull/aggressive, sideways/aggressive, bear/robust
  - bull/aggressive, sideways/robust, bear/robust

Maximum target: `6` to `8` candidates, not dozens.

### Additional checks

- Regime transition months should be inspected separately.
- Verify whether gains come from a few crisis windows only.
- Confirm the switching rule does not just duplicate the robust policy most of the time.

### Pass criteria

- Best candidate passes all shared gates.
- `15y` alpha improvement is meaningful.
- `10y` degradation vs baseline is modest enough to be acceptable.
- No large blow-up in trade count around regime boundaries.

### Failure conditions

- Gains come almost entirely from optional `20y`.
- `1y` or `1m` behavior becomes unstable.
- Regime switching adds complexity without improving `15y`.

---

## Experiment 2: Volatility-Scaled Position Sizing

**Priority:** High

### Hypothesis

Keep ranking/selection logic mostly unchanged, but vary deployed exposure based on realized volatility or drawdown regime. This may improve return efficiency without changing the core asset-selection logic.

### Why this is worth doing

- It is one of the clearest untested ideas left in prior notes.
- It changes exposure size rather than signal direction, which reduces strategy-risk compared with changing ranking logic.
- It has a plausible path to better Sharpe and lower drawdown while preserving upside.

### Candidate structure

Start from the current paper-trading baseline strategy and add a sizing overlay only.

### Parameter grid

Realized-vol lookback:
- `20d`
- `60d`

Target-vol bands:
- `10%`
- `12%`
- `14%`

Exposure caps:
- `1.0x`
- `0.75x`

Floor exposure:
- `0.50x`
- `0.75x`

Start with no leverage. Only scale down, not up.

### Candidate examples

1. Mild scaling:
- `position_size = clamp(target_vol / realized_vol, 0.75, 1.0)`

2. Stronger scaling:
- `position_size = clamp(target_vol / realized_vol, 0.50, 1.0)`

3. Drawdown-aware overlay:
- full size in normal conditions
- reduced size after crossing a drawdown threshold

### Additional checks

- Measure whether reduced drawdown meaningfully improves compounding or just lowers returns.
- Inspect whether the overlay cuts exposure precisely when momentum is already strongest.
- Compare results against both baseline and the best static strategy candidate from Experiment 1.

### Pass criteria

- Improves `15y` alpha or materially improves `15y` Sharpe/max drawdown while keeping alpha near baseline.
- Does not significantly damage `10y`.
- `1y` and `1m` remain competitive enough for current-market relevance.

### Failure conditions

- It simply smooths the equity curve while giving up too much CAGR.
- It becomes another hidden trend filter that exits too early and re-enters too late.

---

## Experiment 3: Confirmation Layer Around 12M Momentum

**Priority:** Medium

### Hypothesis

Pure 12M momentum may react too slowly to reversals, but fully switching to shorter lookbacks creates whipsaw. A confirmation layer may improve timing near decision boundaries without replacing the base 12M logic.

### Why this is worth doing

- Prior docs suggest `9m` is promising but unstable as a full replacement.
- This tests a more surgical use of shorter-horizon information.
- It is less invasive than moving the entire strategy to 9M or 6M.

### Candidate structure

Base signal:
- 12M momentum remains primary

Confirmation rule only triggers when the 12M spread is near the threshold:
- If 12M spread is clearly decisive: use 12M only
- If 12M spread is near the switching boundary: require confirmation from shorter-term signal

### Parameter grid

Boundary band:
- within `2pp`
- within `4pp`

Confirmation source:
- `3m > 12m direction agrees`
- `6m > 12m direction agrees`
- `9m > 12m direction agrees`

Confirmation logic:
- single shorter-horizon confirmation
- majority of `3m/6m/9m`

Keep candidate count low:
- ideally `4` to `6` total variants

### Additional checks

- Count how many trades were blocked or advanced by confirmation.
- Determine whether the benefit appears in reversals or just in random churn.
- Compare specifically on windows where 12M was historically weakest.

### Pass criteria

- Better `15y` than baseline.
- No serious `10y` collapse.
- Trade count and turnover do not jump sharply.
- `1y` and `1m` remain sensible.

### Failure conditions

- It behaves like a disguised shorter-lookback strategy.
- It increases noise trading without improving long-horizon alpha.

---

## Deprioritized Ideas

These should not be the next main track unless new evidence appears:

- More VIX/canary crash filters
- More SMA-only trend filters
- Large defensive-universe expansion
- More feature stacking in a single composite strategy

Reason:
- Prior research already found these to be weak, mixed, or too regime-dependent.

---

## Recommended Execution Order

1. Implement a reusable evaluation script/protocol for:
   - `15y`, `10y`, `1y`, `1m`, optional `20y`
   - baseline comparison
   - common output table
2. Run Experiment 1: Regime-Switched Policy
3. If Experiment 1 is inconclusive, run Experiment 2: Volatility-Scaled Position Sizing
4. Only then run Experiment 3: Confirmation Layer

Reason:
- Experiment 1 has the highest expected information value because it reconciles the strongest existing findings.
- Experiment 2 is the cleanest genuinely new idea.
- Experiment 3 is promising but easier to overfit if done too early.

---

## Immediate Next Task

Build the new research acceptance script around Experiment 1 first.

That script should:
- encode the new horizon order
- compare against the current paper-trading baseline
- support a small candidate grid
- emit a compact table plus machine-readable results
- keep `20y` optional and explicitly labeled as stress/history only

Implemented as:
- `scripts/backtest_regime_switch_experiment.py`
