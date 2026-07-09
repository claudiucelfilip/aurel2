# Goal: Edge Decomposition — find where the trading edge comes from, then rebuild Aurel2's decision layer around the answer

**Date:** 2026-07-09
**Status:** Approved, Phase 1 in progress
**Branch:** autonomous-trading

## Why

Aurel2 today is a 12-month dual-momentum rotator that is internally consistent but unloved: its holds feel wrong, its backtest has burned us before, and a scrappier sibling (live-trader) is up ~20% on real money while Aurel2 sits in paper. The paper evaluation has run its course with mixed results. Instead of another round of threshold-tweaking, this goal runs one decisive experiment — decompose live-trader's returns into *deterministic algo*, *AI discretionary layer*, and *market beta*, benchmarked against Aurel2 over the identical window — and commits in advance to acting on whichever answer comes back.

## The system map (explored 2026-07-09)

| System | Role | AI in loop | Money | Recent result |
|---|---|---|---|---|
| aurel2 | slow ETF rotator (12m DM) + execution infra | none live | paper | mixed; holding XLK since May |
| live-trader | fast ETF rotator (5/20/60d conviction score) | Claw discretionary overlay (tilts + manual trades) | **real** | ~+20% since 2026-02-11 |
| aurel2-crypto | fast crypto rotator (28d momentum, stops) | none | paper/testnet | ~−11% over 3 months |
| aurel3 | single-stock idea engine (news/sentiment) | **core** (GPT-5.x via OpenClaw) | recs only | 145 recs, no P&L tracking |

Key facts driving the hypothesis:
- live-trader's daily engine is deterministic (linear conviction score, edge threshold 4.0, 2-day cooldown). The AI shows up in: `discretionary_tilt.json` nudges, manual out-of-watchlist trades (e.g. the 2026-07-07 XLK buy), and having authored/tuned the algo itself.
- Aurel2's own lookback sweep (`data/lookback_and_vote_2026_07_07.json`) showed shorter lookbacks perform *worse* in its harness — so if LT-actual still beats Aurel2, the AI layer is the likely alpha.
- aurel2-crypto has live-trader's shape (short lookback, active rotation) but no AI layer, and is losing — a weak no-AI control in a different market.
- Both aurel2 and live-trader currently hold XLK: they disagree about the journey, not the destination.

**Hypothesis (H):** live-trader's edge = deterministic core + AI filling the gaps. If the deterministic part alone backtests no better than Aurel2, the AI layer *is* the alpha — and that is precisely what Aurel2 is missing.

## Phase 1 — the decomposition experiment

Five arms, all over the identical window (2026-02-11 → present, live-trader's lifetime), all benchmark-adjusted:

| Arm | What it is | Source |
|---|---|---|
| 1. LT-core | live-trader's conviction score alone — no tilts, no manual trades | replay `daily_runner.py` logic verbatim |
| 2. LT-actual | what really happened, AI interventions included | `trades.jsonl` (manual trades attributable via reconcile) |
| 3. A2-actual | Aurel2's real paper record | trade journal |
| 4. A2-replay | Aurel2's live decision code replayed over the same window | actual `checker.py`/`dual_momentum.py` path |
| 5. Benchmarks | SPY and QQQ buy-and-hold | price data |

The three numbers that decide everything:
- **(2)−(1) = the AI layer's worth.** Positive and meaningful → H holds.
- **(1) vs (4) = which deterministic core is better** on this window.
- **(3) vs (4) = Aurel2's own fidelity check.** If its paper record and its replay disagree, the backtest-vs-live bug class is caught red-handed with a concrete diff to chase.

**Fidelity rule:** every replay runs the *actual live decision code* of each system — no parallel reimplementations. The same code path becomes both the backtest engine and the live engine, structurally killing the backtest-vs-live mismatch class.

**Deliverable:** one decomposition report — attribution table, per-decision divergence log (every date LT-core and LT-actual diverge, and what the intervention was), and a stated confidence level given the small sample.

## Phase 2 — pre-committed branch table

Whatever the decomposition says, we act:

- **Branch A — AI layer wins** ((2)−(1) meaningfully positive; cores roughly tie). Build an AI discretionary overlay for Aurel2 (tilt file, caps, staleness gates — the pattern live-trader proved). **Which agent/model runs it is an open Phase 2 decision** — candidates: Claw (same OpenClaw agent as live-trader, proven), Claude, or the aurel3 GPT path. The decomposition report (what *kind* of interventions added value) informs the choice.
- **Branch B — LT's deterministic core wins** ((1) beats (4) clearly, AI adds little). **Retire Aurel2's algo entirely** — a better algo means the old one goes. The short-lookback conviction score gets properly backtested across regimes before promotion.
- **Branch C — Aurel2's core wins and AI adds little.** Unlikely per current data. The comparison must be **AI-matched**: Aurel2+AI vs LT-core+same-AI, so cores are compared fairly rather than crediting one side's overlay. Then ship legibility/fidelity fixes and consider going live as-is.
- **Branch D — nothing beats QQQ buy-and-hold.** Start from scratch on strategy; keep the infra and aurel3 idea-flow.

Mixed verdicts combine branches. Most likely per current evidence: LT-core worse than A2-core, yet LT-actual best overall → strong signal to add AI to Aurel2 (Branch A).

**Every branch ends the same way:** the chosen configuration runs in paper **parallel against live-trader** (same window, same referee metrics, weekly scorecard) for 6–8 weeks minimum. **Going live with real money on Aurel2 (or its successor) is the explicit graduation step** if it holds up.

## Risks (accepted and named)

1. **Tiny sample.** ~16 LT trades, one bullish regime. Directional evidence, not proof — hence the parallel run as the real test.
2. **Attribution fuzziness.** Some AI value may hide in timing, not just overrides; the replay must log every LT-core/LT-actual divergence, not just trades.
3. **Regime luck.** Feb–Jul 2026 favored risk-on rotation. The QQQ arm and alpha-vs-SPY numbers guard against crowning beta as skill.
4. **Fidelity rabbit hole.** If (3) vs (4) diverge, the investigation is time-boxed — a finding in the report, fixed only if it changes the verdict.

## Non-goals

- Touching live-trader — it keeps running untouched, on real money.
- The four-system merge (follow-on goal; aurel3 fits as idea-feed into the winner, aurel2-crypto inherits the verdict later).
- Building the AI overlay before the decomposition says to.

## Phase 2 non-negotiables (added 2026-07-09, after Phase 1 findings)

1. **One wiring, shared by live and backtest.** Purge or quarantine dormant config paths (REGIME_WEIGHTS/weighted-vote, calm-hold, min-hold, pilot entry, unwired robust_quarterly strategies — experiment remnants). Whatever survives lives behind a single canonical config artifact loaded by BOTH the live checker and the backtest engine. The live universe/thresholds hardcoded in `checker.py` vs the divergent `config/default.yaml` is exactly the split to eliminate.
2. **Continuous fidelity guard.** The Phase-1 actual-vs-replay diff becomes a scheduled weekly job: replay the last N live decision days through the backtest engine, alert via ntfy on any divergence. Backtest≠live must be structurally unable to persist unnoticed.
3. **Promotion path.** A winning variant ships as a change to the shared config/code path (never a backtest-script-only construct), passes the fidelity guard, then earns real money via the 6–8-week paper parallel run against live-trader.
4. **Overlay containment.** The AI overlay's only write-surface is a schema-validated, capped, versioned tilt interface; it cannot introduce decision logic, place orders, or touch config. (Lesson from Claw's unauthorized, untracked `daily_runner.py`.)

## Done means

1. Decomposition report exists with attribution table, divergence log, and a stated verdict + confidence.
2. The matching branch has been chosen and its build executed.
3. The chosen configuration runs in paper parallel against live-trader with a weekly scorecard.
4. A pre-agreed graduation rule is written down (e.g. "after 6–8 weeks, if within X of LT risk-adjusted, go live with real money").
