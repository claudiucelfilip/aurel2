# AI Overlay Design — Aurel2 Phase 2

**Date:** 2026-07-10
**Status:** Approved design, build starting
**Goal:** `docs/plans/2026-07-09-edge-decomposition-goal.md` (Branch A, non-negotiables §Phase 2)
**Evidence:** `docs/2026-07-09-edge-decomposition-report.md`, `data/edge_decomposition/*` (all committed)

## Architecture (evidence-settled, one line each)

- **Spine:** A2's live 12m dual-momentum, ungated, unchanged (best 15y record; gates proven inert-or-harmful — `regime_gate_matrix.json`).
- **Overlay operator:** **Fable 5** via `claude --model claude-fable-5 -p` CLI (won the 4-model blind bake-off — `model_bakeoff.json`). Claw shadow-scored weekly, read-only.
- **Interface:** capped **transition powers** (score-nudges proven inert vs 12m momentum gaps — `ai_tilt_replay.json`).
- **Fallback:** trend-vol gate (+1pp/−3.9pp DD over 15y) if the overlay fails the parallel run.
- **Benchmark:** live-trader untouched, real money, the race opponent.

## The tilt interface (the overlay's ONLY write surface)

File: `data/{mode}/overlay_tilt.json`, schema-validated on read, git-versioned on every write (auto-commit), applied by the orchestrator, journaled into the trade journal with full reasoning.

```json
{
  "as_of": "2026-07-14",
  "expires": "2026-07-21",            // hard TTL: stale tilt = no tilt
  "regime_view": "risk_on|mixed|risk_off",
  "confidence": 0.0,
  "powers": {
    "accelerate_entry": {"symbol": "QQQ|null"},   // power 1
    "lookback_override_months": null,              // power 2: 3|6|null
    "force_defensive_contest": false               // power 3
  },
  "reasoning": "...",
  "samples": 5, "sample_agreement": 0.8            // majority-vote metadata
}
```

## The three transition powers + caps

1. **Accelerate entry** — enter the asset the core itself would buy if current short-term trends persist (computed by projecting the ranking forward). The AI may only accelerate the core's *own* next pick, never introduce a symbol. Cap: max 1 acceleration per 21 days; position enters at normal size; journaled as `overlay_accelerated`.
   *Evidence: the Apr-23-vs-May-12 QQQ gap was most of live-trader's era-2 alpha; blind Fable 5 called it Apr 20.*
2. **Temporary lookback override** — in sustained risk_off view, the ranking runs at 3m or 6m instead of 12m. Cap: max 30 consecutive trading days per activation, max 2 activations per quarter; auto-reverts on expiry or risk_on.
   *Evidence: short lookbacks cut 15y max DD 33%→22% (`lookback_and_vote`), but destroy bull returns — so only ever temporary and regime-conditional.*
3. **Force defensive contest** — one-off: run today's ranking restricted to {GLD, AGG, SHY, IEF, TIP, CASH} vs the held asset with equal thresholds; if a defensive wins, rotate. Cap: max 1 per 14 days.
   *Evidence: 2026 chop — no threshold discount could make defense win the open contest vs XLK's +50pp; a restricted contest could.*

**Hard containment (non-negotiable #4):** the overlay cannot place orders, touch config, modify code, or act outside this file. Malformed/expired/schema-invalid tilt = no-op. Every applied AND ignored tilt journaled.

## Decision protocol

- Cadence: weekly (Monday pre-open) + event-triggered re-run if held asset moves >5% in 3 days.
- **Majority-of-5 sampling:** 5 independent CLI calls; regime_view by majority; a power activates only if ≥3/5 samples agree on it; biases/params averaged. *(Evidence: 4/11 chop weeks flipped view across resamples — single samples are coin-flips in chop.)*
- **Deterministic context packs:** pinned raw closes (not auto-adjusted), snapshot committed alongside the tilt. *(Evidence: yfinance auto-adjust drift broke cache reproducibility.)*
- Blindness discipline in prompt retained from harness.

## Context pack (single ablation step BEFORE build freeze)

One offline research step, no iteration after launch: run the 22-week blind harness with v1/v2/v3, pick the sharpest, freeze.

- **v1 (done):** prices, momentum table, vol, position, core signal.
- **v2:** + per-asset RSI/z-scores (16 assets), momentum-spread alarm (1–3m vs 12m divergence per asset).
- **v3:** + breadth (% above 50/200dma), vol rate-of-change, GLD/TLT vs equity relative strength, bond-ETF spreads, self-awareness block (drawdown from peak, days held, core's projected next action).

Judgment metrics (not P&L): earlier Feb risk-off, Apr risk-on preserved, fewer "mixed", higher resample stability, Claw-agreement. Features that don't sharpen → cut.

## Build tracks (parallelizable)

- **Track A — debt cleanup:** purge dead ensemble stubs (SPY-only RSI path, unwired robust_quarterly), dormant orchestrator toggles (REGIME_WEIGHTS/vote path, calm-hold, min-hold, pilot); ONE canonical config artifact loaded by both checker.py and BacktestEngine; weekly fidelity guard (replay last N live days vs actual, ntfy on divergence).
- **Track B — senses + ablation:** implement v2/v3 features point-in-time-clean; run the single ablation step; output the frozen context pack spec.
- **Track C — overlay engine:** tilt file schema + validator + git-autoversion; the three powers in the orchestrator behind the interface; majority-of-5 runner (claude CLI); journaling; Claw shadow-scorer (read-only weekly capture of what Claw would do — via its market_check output or a codex CLI call — logged, never applied).
- **Integration:** C consumes B's frozen pack; A's config artifact carries the overlay settings; full-system backtest replay over Feb–Jul 2026 as acceptance test (fidelity rule: same code path live and backtest).

## Parallel run & graduation

- 6–8 weeks, paper: A2+overlay vs live-trader (real) vs A2-bare (shadow replay) vs QQQ.
- Weekly scorecard (auto, ntfy + dashboard): return, alpha vs QQQ, max DD, overlay interventions + their per-decision outcome, sample agreement rates.
- **Graduation rule (write EXACTLY before run start; template):** after ≥6 weeks, if A2+overlay's risk-adjusted return (Sharpe) ≥ live-trader's and max DD ≤ 1.25× live-trader's, and zero containment violations → go live with real money. If overlay underperforms A2-bare → remove overlay, evaluate trend-vol fallback. If containment violated → halt, redesign.

## Out of scope

live-trader changes (real money, untouched); crypto (inherits verdict later); aurel3 integration (later idea-feed); any live iteration on the algorithm during the run.
