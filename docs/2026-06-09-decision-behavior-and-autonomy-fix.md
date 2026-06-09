# Aurel2 Decision Behavior & the June 2026 Autonomy Fix

**Date:** 2026-06-09
**Status:** current as of this date; live = paper, holding XLK

This doc exists to stop a recurring question from being re-litigated:
**"Aurel2 keeps saying *hold XLK* — shouldn't it have sold? Didn't the metrics fail?"**
Short answer: **no.** Holding XLK is the correct call by every metric the
strategy uses, and the June 2026 bug was about *decision labeling / autonomy*,
**not** a wrong hold. Details below, with the honest caveats.

---

## 1. How Aurel2 actually decides (dual momentum)

The action driver is **dual momentum** (`strategies/dual_momentum.py`,
`dm_primary` mode). Each check it ranks every asset by **trailing 12-month
return** and:

- **Holds the asset with the highest 12-month momentum** (the leader).
- **Sells / rotates only when** either:
  1. **another asset overtakes the current holding** by more than the switch
     threshold (2%), **or**
  2. the current holding's **own 12-month momentum goes negative** (below cash) →
     move to cash.
- There are **no stop-losses** by design (research "Experiment 3" rejected them —
  they sold at the bottom of normal dips right before the rebound, the
  "V-shaped recovery trap"). The momentum signal *is* the risk manager.

The 12-month lookback is deliberately **slow**: it ignores short, shallow dips so
the system isn't whipsawed out of a strong trend. The cost is that it lags — it
will ride a holding down somewhat before the signal turns. That lag is the source
of the strategy's outperformance (see §5).

## 2. Why "hold XLK" is correct right now

Current 12-month momentum (2026-06-09):

| Asset | 12-mo momentum |
|---|---|
| **XLK (held)** | **+52.3%** ← #1 by ~9 pts |
| EEM | +43.7% |
| GLD | +28.8% |
| SPY | +24.1% |
| EFA | +18.9% |
| AGG | +5.1% |

XLK is the strongest asset by a wide margin; there is nothing better to rotate
into. For the metric to say **sell**:
- **~11% further XLK drop** → EEM would overtake it past the 2% threshold, or
- **~36% further XLK drop** → XLK's own 12-month return goes negative → to cash.

A 7% pullback from a peak (what prompted the original concern) is noise against a
+52% one-year trend. The recent live decisions (2026-06-08, 2026-06-09) are
`hold XLK`, type `routine` (auto-decided).

> **Note on `exec=False` for holds:** a "hold" has nothing to execute (you already
> own XLK). `exec=False` on a hold is a no-op, **not** a failed trade.

## 3. What the June 2026 bug actually was (and wasn't)

**Was NOT:** "it should have sold XLK and didn't." The live system correctly kept
deciding to hold XLK (it bought XLK on 2026-05-07 and held since). Holding the
leader was right.

**WAS:** a *labeling / autonomy* problem:
- The `multi_timeframe` strategy was misconfigured — its universe excluded sector
  ETFs, so it had no data for XLK and always cast a dead "switch to SPY" vote.
- Under the old **unanimity** rule, that single dead vote meant **no decision could
  ever reach agreement** → everything was tagged `non_routine` ("needs approval").
- For a **hold**, that tag was harmless (nothing to approve/execute). The latent
  danger was that a genuine **switch**, when eventually warranted, would be routed
  to the (dead) approval backend and stranded.

The timeout auto-execute (1-hour, polled every 5 min, independent of the external
approval service) is real and works — **but it only acts on buy/sell**, so it never
mattered for the stream of holds the live system was producing.

## 4. The fix (commit `d57d24d`, deploy `e216f09`, dispatch test `b1b1823`)

Four bundled changes:
1. **multi_timeframe universe fix** — give it the full no-TLT universe so its vote
   is real (no more dead "switch to SPY").
2. **2/3-majority auto-execute** — `_classify_decision` returns ROUTINE on 2-of-3
   agreement instead of requiring unanimity; only a true 3-way split needs approval.
3. **21-day min-hold throttle** — daily monitoring, ~monthly execution cadence.
4. **Backtest engine aligned to the live config** — the backtest had been running a
   *different, stale config* than live.

Plus a tested router (`checker._dispatch_decision`): a ROUTINE decision
auto-executes and **never** touches the approval path. 298 tests pass.

## 5. Backtest evidence — and an important honesty caveat

**3-month replay (same data, fix vs no-fix):**

| | Without fix | With fix |
|---|---|---|
| Return | −16.0% | +12.5% |
| Behavior | bought GLD, stuck, never rotated | GLD → rotated to **XLK** (Apr 16), held |
| Ended holding | GLD | **XLK** |

**2.5-year (2024–2026):** ~+68% (old config) → **~+130%** (fixed; Sharpe 1.60, max
DD 19.2%, 8 trades), vs SPY ~+60%.

> **CAVEAT — do not read the −16% as real-money loss.** The −16%-stuck-in-GLD was
> the **old *backtest engine's*** behavior, caused mainly by change #4 (the backtest
> ran a misconfigured strategy that bought GLD) plus the dead multi_timeframe vote.
> **Live was never in that position** — live already ran the corrected plain-DM
> config (commit `d648ff1`) and was holding **XLK**. So the −16% vs +12.5%
> demonstrates the *mechanism* of the bug and proves the backtest is now **faithful
> to live**; it does **not** mean the fix rescued ~28 points of real return. The
> winning fixed strategy *also* ends holding XLK — so today's "hold XLK" is exactly
> what the high-performing strategy does.

**What the fix actually bought:** (a) trustworthy backtests that mirror live, (b)
correct labeling + autonomy so a real switch auto-executes instead of being
stranded, (c) no over-trading.

## 6. Tested and rejected (don't re-litigate)

- **Faster momentum lookback** (6mo / 3mo vs 12mo), 2.5yr: 12mo **+133%** / Sharpe
  1.60 / 8 trades; 6mo +27% / 0.55 / 12; 3mo +22% / 0.50 / 15. In the recent dip,
  faster lookbacks **lost** money (6mo −14.5%, 3mo −4.2%) by chasing energy/commodity
  head-fakes. **Keep 12-month.**
- **Downside overlays** (trend filter / canary-to-cash), stress 2018–2026 incl.
  2020+2022 crashes: baseline **+391%** / 35.4% DD / Sharpe 0.87; +trend +179% /
  25.7% / 0.73; +canary +157% / 28.0% / 0.88. They cut drawdown but **never improve
  Sharpe** and cost multiples of return. **Don't add one** for a return-seeking goal.
  (Only consider canary if the goal flips to pure drawdown-minimization.)

## 7. Cadence (deployed)

- **Market evaluation:** once per trading day at `CHECK_TIME`.
- **Approval/timeout poll:** every 5 min — services pending approvals only, makes no
  new decisions.
- **Execution throttle:** a switch is allowed at most every **21 trading days**
  (~monthly). It can still sell to cash any day if momentum collapses.

---

*Sources: live `trade_journal.json`, `scripts/pnl_divergence_scan.py`, backtest
replays over 2024–2026 and 2018–2026, and commits `d648ff1`, `d57d24d`, `e216f09`,
`b1b1823`. Numbers are as of 2026-06-09 and will drift with the market.*
