# Frozen Context-Pack Spec — AI Overlay (Track B ablation result)

**Date:** 2026-07-10
**Status:** Frozen. This is the single ablation step called for in
`docs/plans/2026-07-10-ai-overlay-design.md` ("Context pack — single ablation
step BEFORE build freeze"). No further iteration on pack contents after this.
**Winner: v2.**

## Result

| Metric | v1 | v2 | **v3** |
|---|---|---|---|
| First risk_off call (Feb-Mar chop) | 2026-03-23 | **2026-03-16** (earliest) | 2026-03-23 |
| Apr risk_on preserved (before Claw's 04-23 entry) | ✅ 04-20 | ✅ 04-20 | ✅ 04-20 |
| % weeks "mixed" | 50.0% (11/22) | 50.0% (11/22) | 50.0% (11/22) |
| Mean sample agreement, all 22 weeks | 0.955 | 0.936 | 0.964 |
| Mean sample agreement, 8 chop weeks | 0.875 | 0.900 | 0.950 |
| False regime signal in chop | none | none | **risk_on 5/5 on 02-23** (false positive) |
| Claw agreement (agree/disagree/neutral, n=9) | 4/2/3 | **4/1/4** | 4/1/4 |

**Winner: v2.** It calls risk_off a full week earlier than v1/v3 with a
well-grounded 3/5→then-unanimous majority (see below), matches v3's improved
Claw-agreement rate, and does not produce v3's false risk_on signal. v3's
higher raw stability number is not actually better judgment — it is high
because v3 unanimously (5/5) called `risk_on` on 2026-02-23, three weeks
into what was in fact an 8-week chop, on the strength of its new breadth
feature (94% of the universe above its 200dma). That call reads as
premature/wrong in hindsight — a real failure mode the ablation surfaced, not
a scoring artifact (all 5 samples independently produced the same
breadth-driven reasoning). v3's other new features (vol rate-of-change,
GLD/TLT relative strength, bond-ETF spreads, self-awareness block) did not
sharpen any judgment metric enough to outweigh that false positive.

### Why v2 called risk_off earlier and better than v1

v1 (2026-03-23, momentum-table-only) and v2 (2026-03-16, +RSI/z-score/
momentum-spread-alarm) both eventually called `risk_off`, but v2's call on
03-16 (3/5, becoming 5/5 the following week) is explicitly grounded in the
new per-asset technicals — sample reasoning cites `SPY z-score -3.2`,
`AGG RSI 21`, `DBC RSI 89 / z +2.8`, and the momentum-spread alarm's
`+16pp short-vs-12m spread` on DBC — identifying an equities-and-bonds-
falling-together / commodities-spiking "inflation shock" pattern that a bare
12m momentum table cannot distinguish from ordinary rotation. This is the
kind of "features that sharpen judgment" the ablation was designed to find,
and it is the deciding evidence for freezing v2's feature set (RSI(14) +
z-scores + momentum-spread alarm) while cutting v3's additions.

### Features cut

All of v3's additions over v2 are **cut** from the frozen pack:
breadth (% above 50/200dma), volatility rate-of-change, GLD/TLT-vs-equity
relative strength, bond-ETF spreads, and the self-awareness block
(drawdown/days-held/projected-next-action). None sharpened a judgment metric
enough to justify the added prompt size and the breadth feature actively
produced a false regime call. (Note: the self-awareness block's `days_held`
and `held_drawdown_from_12m_high_pct` fields already existed in v1 — only the
*additional* v3 self-awareness field, `core_projected_next_action`, is cut.)

## Frozen pack: exact field list (= v2)

Built by `src/aurel2/overlay/context_pack.py::build_context_pack_v2`
(equivalently `BUILDERS["v2"]`):

```
{
  "as_of": "<ISO date>",
  "last_20d_pct_change": {"<SYMBOL>": [float, ...20 values...], ...},
  "momentum_snapshot_pct": {"<SYMBOL>": {"1m": float|null, "3m": float|null, "12m": float|null}, ...},
  "dm_top5_ranking_12m": [["<SYMBOL>", momentum_12m_pct], ...up to 5],
  "spy_20d_annualized_vol_pct": float|null,
  "current_position": "<SYMBOL>|CASH",
  "days_held": int,
  "held_drawdown_from_12m_high_pct": float|null,
  "deterministic_a2_signal_today": {"action": str, "symbol": str|null, "reason": str},
  "per_asset_technicals": {"<SYMBOL>": {"rsi_14": float|null, "zscore_60d": float|null}, ...all ~17 universe symbols...},
  "momentum_spread_alarm": {"<SYMBOL>": {"short_vs_12m_spread_pp": float, "alarm": bool}|null, ...}
}
```

- `per_asset_technicals` / `momentum_spread_alarm` cover the full DM universe
  (`ASSET_REGISTRY`, ~17 yahoo symbols incl. extras) — not just the top-5
  ranked assets.
- `momentum_spread_alarm.alarm` fires when `(1m+3m)/2` short-term momentum
  diverges from the 12m-implied pace by more than 8pp — see
  `_momentum_spread_alarm` in `context_pack.py`.
- No lookahead: every field is computed from `clip_to_as_of(prices, as_of)`,
  i.e. rows strictly before `as_of`'s prior close.

## Data-pinning procedure (determinism, non-negotiable per design doc)

Raw (unadjusted) closes drift on refetch under `yfinance`'s default
`auto_adjust=True` (dividend/split adjustment is recalculated against the
*latest* corporate-action data every time, so identical `(date, symbol)` rows
can return different `close` values weeks apart) — this broke cache
reproducibility in the model bake-off.

Fix, implemented in `src/aurel2/overlay/data_snapshot.py`:

1. Fetch once with `yf.Ticker(sym).history(..., auto_adjust=False)` — raw
   close, fixed at trade time, never revised.
2. Write the fetched frame to a single committed CSV:
   `data/edge_decomposition/snapshots/price_snapshot_raw.csv`
   (columns: `date, symbol, close, volume`; 17 symbols, 2024-11-18 through
   2026-07-08, 408 rows/symbol).
3. `context_pack.py` (and any production/backtest consumer) must call
   `data_snapshot.load_snapshot()` — which only reads that committed CSV —
   and must **never** call `CachedPriceProvider`/`yfinance` directly for
   context-pack construction. Rebuilding the snapshot (re-running
   `build_and_write_snapshot`) is a deliberate, explicit, committed action,
   not an implicit side effect of running the pack builder or the ablation.
4. To extend the window (new data as time passes), re-run
   `build_and_write_snapshot` with an updated `end_date` and commit the new
   CSV — do not edit it by hand, and do not silently regenerate it from a
   script that also builds packs.

## Ablation methodology (for reproducibility)

- Window: 2026-02-11 → 2026-07-08, 22 Monday-anchored weekly decision days
  (same cadence as `scripts/edge_decomposition/ai_tilt_harness.py`).
- Model: `claude-fable-5` via `claude -p ... --model claude-fable-5
  --output-format json` (never the Anthropic API/SDK).
- 22 weeks × {v1, v2, v3} × 5 independent samples = 330 CLI calls.
- Position/days-held/deterministic-signal context shared across all three
  versions per week (from a single no-AI baseline replay) — the only thing
  that varies between v1/v2/v3 is the pack's feature set, isolating the
  ablation to context content, not position drift.
- Every raw CLI response cached by content hash in
  `data/edge_decomposition/ablation_raw.jsonl` before any aggregation
  (crash-safe: 330/330 cells now cached, 0 fallbacks).
- **Note on "reuse cached results where keys match":** the original v1
  harness cache (`ai_tilt_cache_fable5.jsonl`) was built against
  `CachedPriceProvider`'s auto-adjusted prices, not the new pinned raw-close
  snapshot — its prompt content (and therefore its cache-key hash) can never
  match a pack built from pinned data, so no cells were reusable from it in
  practice. All 330 cells in this ablation were freshly generated under the
  new deterministic pinning scheme; the crash-safety/reuse property applies
  going forward (reruns of `context_pack_ablation.py` skip any cell already
  in `ablation_raw.jsonl`).
- First run hit 80/330 (24%) transient CLI failures clustered on 6 specific
  weeks, spread evenly across v1/v2/v3 (not a data or pack-size bug — a
  standalone reproduction of one failing cell succeeded immediately) —
  consistent with resource contention from 5 concurrent `claude` CLI
  subprocesses. A follow-up retry pass at concurrency=2
  (`context_pack_ablation_retry.py`) recovered all 80/80.

## Data files (committed)

- `data/edge_decomposition/snapshots/price_snapshot_raw.csv` — pinned raw closes.
- `data/edge_decomposition/ablation_raw.jsonl` — all 330 raw CLI responses + parsed tilts.
- `data/edge_decomposition/ablation_results.json` — per-(week,version) sample grid.
- `data/edge_decomposition/ablation_scorecard.json` — the scored judgment metrics behind this doc.

## What production (Track C) should import

`src/aurel2/overlay/context_pack.py::build_context_pack_v2` (or
`BUILDERS["v2"]`) is the frozen pack builder. Track C's majority-of-5 runner
should call this function, feed its output into the same `PROMPT_TEMPLATE`
used here (`ai_tilt_harness.PROMPT_TEMPLATE`, or its production equivalent),
and read prices via `data_snapshot.load_snapshot()` — extending the pinned
snapshot forward in time as new trading days occur, per the procedure above.
