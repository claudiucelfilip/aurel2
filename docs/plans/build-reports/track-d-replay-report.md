# Track D — Overlay-ON Acceptance Replay: Build Report

**Date:** 2026-07-10
**Branch:** `overlay-replay` (based on `autonomous-trading`)
**Spec:** `docs/plans/2026-07-10-ai-overlay-design.md` (§Decision protocol, §Build tracks
Integration: "full-system backtest replay over Feb-Jul 2026 as acceptance test")

## Summary

Ran the full production overlay pipeline (real `claude --model claude-fable-5` CLI calls,
`src/aurel2/overlay/runner.py`'s majority-of-5 sampling, the frozen v2 context pack, and
`BacktestEngine`'s real `overlay_refresh` -> `run_overlay_for_decision` -> `apply_overlay`
seam — not a hand-rolled duplicate) over 2026-02-11 -> 2026-07-09. **Zero crashes, zero CLI
parse failures (110/110), every written tilt schema-valid, every cap respected, containment
clean.** A2-bare (overlay OFF) reproduces the documented 3-trade acceptance baseline's
sequence and symbols exactly. A2+overlay (overlay ON) made 5 trades — 2 more than bare — from
one `accelerate_entry` activation and one `lookback_override_months` activation; the overlay's
net effect on this specific window was a **loss of return relative to bare** (10.84% vs
14.40%), traced to one incorrect accelerate-entry symbol call, not a containment or engine bug.

## 1. Cadence decision (resolved mid-run, orchestrator-directed)

The task brief anchored on ~110 Fable 5 calls (22 weekly refreshes x 5 samples), consistent
with a **daily** engine cadence (22 ISO weeks in the window) — but the existing acceptance
baseline (`scripts/acceptance_replay.py`, `docs/plans/build-reports/track-a-report.md`) was
produced at **monthly** cadence (~5 rebalance dates total, no way to reach 22 weekly refreshes
naturally). This was flagged as a genuine fork before spending CLI budget:

- Confirmed A2-bare at `frequency="daily"` still produces the **same 3 trades, same symbols,
  same sequence** (buy GLD -> sell GLD + buy XLK) as the documented monthly baseline, with
  trade **dates shifted earlier** (2026-02-11 vs 2026-02-27; 2026-04-16 vs 2026-04-30) — daily
  cadence catches the exact triggering day, monthly waits for month-end. This is a known,
  expected cadence-granularity effect, not a fidelity bug.
- Orchestrator additionally clarified: the real paper trade journal bought XLK on
  **2026-05-07**, later than either replay date, because live ran `RobustQuarterlyStrategy`
  until 2026-05-07 and only then switched to plain `DualMomentumStrategy` (see the dated
  comment in `src/aurel2/live/checker.py.__init__`). This replay runs plain DM over the whole
  window, so **pre-05-07 live history is not comparable** — the 4/16-or-4/30 vs 5/07 gap is a
  strategy-migration artifact, not a fidelity bug either.
- Decision: **both arms run at `frequency="daily"`** (matches live's actual daily checker —
  the fidelity-true cadence), with `overlay_refresh` gated to fire once per ISO week. Under
  daily cadence the first business day of a new ISO week is always Monday (or the first
  trading day after a Monday holiday), so this IS the "weekly Monday pre-open" cadence from
  the design doc without a fragile hardcoded weekday check.

## 2. `overlay_refresh` hook (`src/aurel2/engine/backtest.py`)

Added `BacktestEngine.__init__(overlay_refresh: Callable[[date, pd.DataFrame], None] | None =
None)`. Called immediately before the overlay seam (`run_overlay_for_decision`) on every
rebalance date, only when `overlay_enabled=True`; production default `None` — zero behavior
change for any existing caller. Mirrors live's external weekly cron that regenerates
`data/{mode}/overlay_tilt.json` before the checker reads it — backtest has no cron, so a
caller that wants the tilt actually refreshed on a cadence passes this in.

Exception-safe: a refresh failure is caught and logged (`backtest_overlay_refresh_error`),
never aborts the backtest — verified by `tests/test_backtest_overlay_refresh.py`
(5 cases: disabled = never called, called once per rebalance date when enabled, receives the
full prices frame, refresh exception doesn't break the run, default `None` is a no-op).
Full suite: 325 passed (320 pre-existing + 5 new), 0 failed.

## 3. `scripts/overlay_replay.py`

Two arms, same pinned prices, same daily rebalance-date grid:

- **A2-bare**: `overlay_enabled=False`. Same non-cadence config as
  `scripts/acceptance_replay.py` (`use_ai=False`, default `correlation_guard`/`sideways_hold`
  from `CANONICAL_CONFIG`).
- **A2+overlay**: `overlay_enabled=True`, `overlay_mode="replay"`. `overlay_refresh` fires once
  per ISO week: builds the frozen v2 context pack (`build_frozen_context_pack`, sourced from
  `CANONICAL_CONFIG.overlay.context_pack_version`), calls `collect_samples` (5 independent
  `claude-fable-5` CLI calls) + `aggregate_samples` (majority-of-5, identical to
  `runner.run_overlay_decision` minus the git-auto-commit — `auto_commit` is irrelevant here
  since this is scratch replay state), writes `data/replay/overlay_tilt.json` via the real
  `write_tilt`. The engine then reads it through the unmodified production seam
  (`run_overlay_for_decision` -> `apply_overlay`) on every daily decision, exactly as live
  would with a cron-refreshed file.

**Price snapshot**: merged `data/acceptance/price_snapshot_2026-02-11_2026-07-09.csv` (DM
16-asset universe + SPY, pinned raw closes) with QQQ pulled from
`data/edge_decomposition/snapshots/price_snapshot_raw.csv` (same raw-close fetch method,
`auto_adjust=False` — consistent basis) for the QQQ buy-hold benchmark and the
`accelerate_entry` projection space. Neither pinned CSV was modified; the merge happens only
in-memory for this replay.

**Holding-state mirror** (`HoldingTracker`): since `overlay_refresh` fires before the real
engine updates its own `current_holding_symbol` for that date, the callback needs to know
"today's" position to build an accurate context pack. `HoldingTracker.advance()` resolves the
DM -> orchestrator decision the same way the engine will a few lines later (read-only — it
never touches cash/shares; the real engine owns portfolio state) and
`apply_overlay_outcome()` folds any overlay-applied override back into the mirror via a thin
wrapper around `overlay.integration.run_overlay_for_decision` (`make_journal_capturing_wrapper`)
that observes every real call's `OverlayOutcome` without re-invoking the seam (re-invoking
would double-consume cap state).

**Crash safety**: every one of the 110 individual CLI samples is cached by content hash
(`sha256(prompt)[:24]` + week date + sample index) in
`data/edge_decomposition/overlay_replay_cache.jsonl`, appended before aggregation. A rerun
reloads the cache and only calls the CLI for missing cells.

**Data isolation**: `overlay_mode="replay"` confines every write to `data/replay/`
(`overlay_tilt.json`, `overlay_state.json`); `data/replay/` is wiped at the start of every run.

## 4. Run results

**Real run**: 110/110 `claude-fable-5` CLI calls, 0 parse failures, 0 fallback-to-default
weeks, all 22 weekly refreshes at `samples=5`. Total elapsed ~50 minutes (sequential CLI calls,
up to 150s timeout each).

### Final returns

| Arm | Final value | Return | Trades |
|---|---|---|---|
| A2+overlay | $11,084.05 | **+10.84%** | 5 |
| A2-bare | $11,439.78 | **+14.40%** | 3 |
| SPY buy-hold | — | +8.63% | — |
| QQQ buy-hold | — | +16.04% | — |

### A2-bare trades (matches the known baseline's sequence/symbols; dates shifted per §1)

| Date | Action | Symbol |
|---|---|---|
| 2026-02-11 | buy | GLD |
| 2026-04-16 | sell | GLD |
| 2026-04-16 | buy | XLK |

### A2+overlay trades

| Date | Action | Symbol |
|---|---|---|
| 2026-02-11 | buy | GLD |
| 2026-04-06 | sell | GLD |
| 2026-04-06 | buy | XLE (`accelerate_entry`) |
| 2026-04-13 | sell | XLE |
| 2026-04-13 | buy | XLK |

## 5. Overlay interventions — what happened and why

Two of the three powers activated during the window; `force_defensive_contest` was never
requested by any weekly majority (consistent with a trending, not choppy-defensive, Feb-Jul
window).

### `accelerate_entry` — 1 applied, 14 ignored

- **2026-04-06, applied**: the majority tilt requested `XLE`; `project_next_pick` (3-month
  momentum projection, restricted to the DM universe) independently agreed `XLE` was the
  core's own projected next pick, so the power applied — GLD sold, XLE bought 10 days before
  bare's eventual XLK entry.
- **All 14 ignored rows** are the containment rule working as designed: every rejection names
  a real DM-universe symbol (GLD, XLE, EEM — never an invented one) and the reason is always
  either "not the core's own projected next pick" or "already holding X, nothing to
  accelerate." The AI requested GLD acceleration on 2026-03-02..06 (rejected — projection said
  XLE, not GLD) and EEM acceleration on 2026-04-13..17 (rejected — projection said DBC, not
  EEM, at that point in the 3m-lookback window). **Containment held in every case: the overlay
  never got a symbol accepted that the core's own projection didn't independently confirm.**
- Cap check: 1 applied event, cap is max 1 per 21 days — trivially satisfied (only 1 total).

### `lookback_override_months` — 1 distinct activation (20 daily "still active" applied rows)

- **Activated 2026-03-23** (risk_off view, 3-month override), stayed active through
  **2026-04-17** (the daily "still active" journal rows are the same activation persisting,
  not repeated activations), then **auto-reverted 2026-04-20** when the regime flipped to
  `risk_on` — exactly the auto-revert-on-risk_on rule in `apply_lookback_override`.
- Cap check: 1 activation this quarter, cap is max 2 per quarter — satisfied. Duration: 28
  calendar days (03-23 to 04-20), under the 30-day cap.

### `force_defensive_contest` — 0 requests, 0 applications, 0 cap consumption

No week's majority ever requested this power. Cap (max 1 per 14 days) trivially satisfied by
having zero applied events.

## 6. Why overlay underperformed bare on this window

The `accelerate_entry` call on 2026-04-06 got the **direction** right (rotate out of GLD into
a cyclical/growth asset ahead of bare's later exit) but the **specific symbol** wrong: XLE, not
XLK. One week later the DM ranking (still running at the active 3-month lookback override)
flipped to XLK, and the overlay sold XLE at a ~4.3% loss ($59.68 -> $57.11) before buying XLK —
3 trading days *earlier* than bare's XLK entry (04-13 vs 04-16), but at a worse net cost from
the failed XLE detour. This is a real, attributable underperformance from one power's decision
quality, not a containment or engine defect — every step of the trade was capped, journaled,
and restricted to real universe symbols exactly as designed. It's the kind of outcome the
6-8 week parallel run (`docs/plans/2026-07-10-ai-overlay-design.md`, "Parallel run &
graduation") is meant to catch before any real-money graduation decision.

## 7. Acceptance criteria — verified

| Criterion | Result |
|---|---|
| Zero crashes / unhandled errors | **Pass** — clean exit, log has zero uncaught tracebacks; every `backtest_overlay_error`/`backtest_overlay_refresh_error` catch site logged 0 hits |
| Every written tilt schema-valid | **Pass** — all 22 weekly tilts re-validated against `schema.validate_tilt`, 0 invalid |
| `accelerate_entry` cap (max 1 / 21 days) | **Pass** — 1 applied event total |
| `lookback_override_months` caps (max 30 consecutive days / activation, max 2 / quarter) | **Pass** — 1 activation, 28-day duration, 1 of 2 quarterly slots used |
| `force_defensive_contest` cap (max 1 / 14 days) | **Pass** (vacuously — 0 applied events) |
| Apr risk_on transition visible in regime series | **Pass** — `mixed` through 04-13, `risk_on` from **2026-04-20** onward (matches the frozen context-pack spec's documented Apr-20 call, `docs/plans/2026-07-10-context-pack-spec.md`) |
| A2-bare matches acceptance baseline | **Pass, with documented cadence-driven date shift** — see §1; same 3 trades, same symbols, same sequence |
| Overlay differences reported, each checked against caps | **Pass** — see §5; every applied/ignored row traced to a specific cap or containment rule |

## 8. Containment audit

`git status --porcelain` after the run, filtered to `data/`, shows exactly:

```
data/edge_decomposition/overlay_replay.json       (result, committed)
data/edge_decomposition/overlay_replay_cache.jsonl (raw CLI cache, committed)
data/replay/                                        (scratch tilt/state, NOT committed)
```

No other file under `data/` was touched — `data/paper/` and `data/live/` do not exist in this
worktree and were never created. `data/replay/` is regenerated (wiped + rebuilt) on every run
and is excluded from this branch's commit (see §9).

## 9. Commits made (this branch, `overlay-replay`)

Not pushed — orchestrator merges into `autonomous-trading` and pushes, per instructions.

1. `Add optional overlay_refresh hook to BacktestEngine` — `src/aurel2/engine/backtest.py` +
   `tests/test_backtest_overlay_refresh.py` (5 new tests, full suite 325 passed).
2. This commit: `scripts/overlay_replay.py`, `data/edge_decomposition/overlay_replay.json`
   (results), `data/edge_decomposition/overlay_replay_cache.jsonl` (raw CLI cache, 110
   records), this report. `data/replay/` (scratch tilt/state) is **not** committed — it's
   wiped and rebuilt by every run.

## 10. Scope notes / assumptions

- **`overlay_refresh`'s week-gating** relies on ISO-week transitions under daily cadence rather
  than an explicit `date.weekday() == 0` check, specifically so it doesn't misfire around
  holidays (a Tuesday-after-Monday-holiday is still correctly treated as "first day of the new
  week"). This is a Track D replay-script choice, not a change to production code.
- **`HoldingTracker`** duplicates the DM -> orchestrator resolution logic that
  `BacktestEngine.run()` already has internally, because the `overlay_refresh` callback fires
  before the engine updates its own state for that date and the callback signature is fixed to
  `(date, prices)` per the task brief. This is read-only bookkeeping (never touches cash/shares)
  scoped entirely to this replay script — not a second production code path.
- **`make_journal_capturing_wrapper`** monkeypatches
  `aurel2.overlay.integration.run_overlay_for_decision` for the duration of the overlay arm's
  `run()` call (restored in a `finally` block) so every real journal row the engine's own seam
  produces can be captured without re-invoking `apply_overlay` a second time (which would
  double-consume cap state). This only observes; the real function is what actually runs.
- **QQQ price merge**: `data/acceptance/price_snapshot_2026-02-11_2026-07-09.csv` doesn't
  include QQQ (it was fetched for the DM universe + SPY only). QQQ prices come from the
  already-committed `data/edge_decomposition/snapshots/price_snapshot_raw.csv` (same
  `auto_adjust=False` raw-close method) and are merged in-memory only for this replay's
  benchmark curve and the `accelerate_entry` projection space — neither committed CSV was
  edited.
