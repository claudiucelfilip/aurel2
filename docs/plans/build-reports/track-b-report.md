# Track B Report — Senses + Context-Pack Ablation

**Branch:** `track-b-senses` (based on `autonomous-trading`)
**Date:** 2026-07-10
**Scope:** `docs/plans/2026-07-10-ai-overlay-design.md`, "Context pack (single
ablation step BEFORE build freeze)" and Track B in "Build tracks (parallelizable)".

## Result: v2 wins

Full scorecard, methodology, frozen field list, and data-pinning procedure:
`docs/plans/2026-07-10-context-pack-spec.md`.

| Metric | v1 | **v2 (winner)** | v3 |
|---|---|---|---|
| First risk_off call (Feb-Mar chop) | 2026-03-23 | **2026-03-16** | 2026-03-23 |
| Apr risk_on preserved (before Claw 04-23) | ✅ 04-20 | ✅ 04-20 | ✅ 04-20 |
| % weeks "mixed" | 50.0% | 50.0% | 50.0% |
| Mean sample agreement (chop weeks) | 0.875 | 0.900 | 0.950 |
| False regime signal in chop | none | none | risk_on 5/5 on 02-23 (false positive) |
| Claw agreement (agree/disagree/neutral) | 4/2/3 | **4/1/4** | 4/1/4 |

v2 = v1 + per-asset RSI(14) + 60d z-scores (full ~17-symbol universe) +
momentum-spread alarm (1-3m vs 12m divergence). It called risk_off a full
week earlier than v1, with reasoning explicitly grounded in the new
technicals (`SPY z-score -3.2`, `AGG RSI 21`, `DBC RSI 89 / z +2.8`,
`+16pp momentum-spread alarm` — an inflation-shock pattern a bare 12m
momentum table can't distinguish from ordinary rotation), and matched v3's
improved Claw-agreement rate without v3's failure mode.

## Features cut

v3's additions over v2 — breadth (% above 50/200dma), volatility
rate-of-change, GLD/TLT-vs-equity relative strength, bond-ETF spreads, and
the self-awareness block's `core_projected_next_action` field — are cut.
None sharpened a judgment metric enough to justify the added prompt size, and
breadth actively hurt: all 5 samples independently called `risk_on` on
2026-02-23, three weeks into what was in fact an 8-week chop, driven by the
new "94% of universe above 200dma" figure. That's a real false positive the
ablation was designed to catch, not sampling noise — verified by reading all
5 samples' raw reasoning.

## What ran

- **Data pinning:** `src/aurel2/overlay/data_snapshot.py` fetches RAW
  (`auto_adjust=False`) yfinance closes once and snapshots them to a committed
  CSV (`data/edge_decomposition/snapshots/price_snapshot_raw.csv`, 17 symbols,
  2024-11-18→2026-07-08). This fixes the determinism bug from the model
  bake-off, where yfinance's default auto-adjusted closes silently drift on
  every refetch as dividends accrue. Parquet was the original plan but
  pyarrow/fastparquet aren't installed in this environment, so CSV was used
  instead — same pinning guarantee, and it diffs cleanly in review.
- **Context-pack module:** `src/aurel2/overlay/context_pack.py` —
  `build_context_pack_v1/v2/v3`, all point-in-time-clean (every feature reads
  only `clip_to_as_of(prices, as_of)`, i.e. strictly before the pack's own
  date). This is a real `src/` module, not a research script — Track C
  (overlay engine) and production both import it directly. Includes a
  `_to_native()` pass to strip numpy scalar types before JSON serialization
  (numpy `bool_`/`int64`/`float64` otherwise leak through pandas comparisons
  and break `json.dumps`).
- **Ablation runner:** `scripts/edge_decomposition/context_pack_ablation.py`
  — 22 weeks × {v1,v2,v3} × 5 samples = 330 `claude-fable-5` CLI calls,
  5 concurrent workers, content-hash-keyed append-only jsonl cache
  (`data/edge_decomposition/ablation_raw.jsonl`).
- **Scoring:** `scripts/edge_decomposition/score_ablation.py` computes all
  five judgment metrics from the spec (first Feb risk_off, Apr risk_on
  preservation, % mixed, resample stability via majority-of-5 agreement,
  Claw calibration) into `data/edge_decomposition/ablation_scorecard.json`.
- **Tests:** `tests/test_context_pack.py` (9 tests) — JSON-serializability,
  point-in-time-clean guarantee, determinism (identical inputs → byte-identical
  pack), and v1/v2/v3 field-set differences. Full suite (`tests/`, 316+9=325
  tests) passes.

## Duration / incident notes

- Sanity CLI check: 1 call, ~9s, confirmed `claude -p ... --model
  claude-fable-5 --output-format json` works before committing to the full run.
- Main ablation run: 330 cells, 30 min wall clock, but **80/330 (24%) came
  back as parse-failure fallbacks**, clustered on 6 specific weeks
  (2026-05-11, 05-18, 05-25, 06-22, 06-29, 07-06) spread evenly across
  v1/v2/v3 — not a data or pack-size bug (a standalone reproduction of one
  failing cell succeeded immediately in ~18-27s). Consistent with resource
  contention from 5 concurrent `claude` CLI subprocesses landing in the same
  scheduling burst. A retry pass at concurrency=2
  (`scripts/edge_decomposition/context_pack_ablation_retry.py`) recovered
  **80/80** in ~13 minutes, replacing the fallback records in-place in the
  raw cache (backed up before the rewrite, removed after success). Final
  cache: 330/330 real responses, 0 fallbacks.
- Total wall clock including the sanity check, main run, retry pass, and
  scoring: ~50 minutes — well under the 2-5h budget in the task spec.
- **Note on "reuse cached results where keys match":** the original v1
  harness's fable5 cache (`ai_tilt_cache_fable5.jsonl`, from the earlier
  model bake-off) was built against `CachedPriceProvider`'s auto-adjusted
  prices, not the new pinned raw-close snapshot, so its prompt content (and
  cache-key hash) can never match a pack built from pinned data — nothing was
  reusable from it in practice. All 330 cells were freshly generated. The
  crash-safety/reuse property does apply going forward: rerunning
  `context_pack_ablation.py` skips any cell already present in
  `ablation_raw.jsonl`.

## Data files (committed)

- `data/edge_decomposition/snapshots/price_snapshot_raw.csv` — pinned raw closes.
- `data/edge_decomposition/ablation_raw.jsonl` — 330 raw CLI responses + parsed tilts.
- `data/edge_decomposition/ablation_results.json` — per-(week,version) sample grid.
- `data/edge_decomposition/ablation_scorecard.json` — scored judgment metrics.
- `docs/plans/2026-07-10-context-pack-spec.md` — frozen spec (field list, scorecard, pinning procedure).

## Commits made

See `git log` on `track-b-senses` for the commits from this session (data
snapshot module, context-pack module + tests, ablation runner + retry script,
scorer, frozen spec doc, this report).

## Handoff to Track C

Track C's majority-of-5 runner should import
`aurel2.overlay.context_pack.BUILDERS["v2"]` (or
`build_context_pack_v2` directly) and `aurel2.overlay.data_snapshot.load_snapshot()`
for prices — never construct its own context pack or hit yfinance directly.
The pinned snapshot will need extending forward in time as new trading days
occur (re-run `build_and_write_snapshot` with an updated `end_date` and commit
the new CSV) — this is a deliberate, explicit action, not automatic.
