# Track C — Overlay Engine: Build Report

**Date:** 2026-07-10
**Branch:** track-c-overlay
**Spec:** `docs/plans/2026-07-10-ai-overlay-design.md`, `docs/plans/2026-07-09-edge-decomposition-goal.md` §Phase 2 non-negotiables

## Module layout

```
src/aurel2/overlay/
  __init__.py        containment statement (write-surface summary)
  schema.py           Tilt dataclass, validate_tilt/load_tilt/write_tilt, hard TTL + schema check
  state.py            OverlayState (data/{mode}/overlay_state.json), cap predicates + recorders
  powers.py           the 3 powers + apply_overlay(tilt, state, today, prices, dm_assets, ...) seam
  prompt.py           render_prompt(pack) — pack-agnostic prompt builder + tilt JSON schema text
  cli_backends.py     call_claude_cli / call_codex_cli (lifted from ai_tilt_harness.py)
  runner.py           majority-of-5 sampling, aggregation, git-auto-commit, CLI entrypoint
  claw_shadow.py       read-only codex shadow-scorer -> data/{mode}/claw_shadow.jsonl
  integration.py       run_overlay_for_decision() — the shared live/backtest seam

tests/
  test_overlay_schema.py       malformed/expired/invalid tilt -> None (21 cases)
  test_overlay_state.py        21d / 30d+2-per-quarter / 14d cap arithmetic (18 cases)
  test_overlay_powers.py       accelerate-entry restriction + all 3 caps end-to-end (20 cases)
  test_overlay_runner.py       majority-of-5 threshold, ties, mocked CLI (20 cases)
  test_overlay_claw_shadow.py  JSONL logging, mocked codex CLI (4 cases)
  test_overlay_prompt.py       pack-agnostic rendering (3 cases)
  test_overlay_integration.py  end-to-end seam incl. hard no-op paths (5 cases)
```

## Integration seam

`apply_overlay()` in `powers.py` is the single function both live and backtest call (via
`integration.run_overlay_for_decision`), matching the existing fidelity pattern where
`engine/backtest.py` already mirrors `live/checker.py` step-by-step with matching comments.

- **`src/aurel2/live/checker.py`**: added `Checker.__init__(overlay_enabled: bool = False)`,
  stores `self.mode`. New step **6c**, between the deterministic decision (6b) and the AI
  advisor (7), calls `self._apply_overlay(decision, prices, current_holding)`. On any override,
  a new `AgentDecision` is built with the overlay's action/symbol substituted and `reasoning`
  prefixed `"Overlay: ..."`; everything else (decision_type, confidence, timeout, urgency,
  regime, position sizing) is left untouched — the overlay never touches sizing/approval logic.
  Failures inside `_apply_overlay` are caught and logged; they fall through to the unmodified
  deterministic decision (an overlay bug can never block a normal trading cycle).
- **`src/aurel2/engine/backtest.py`**: added `BacktestEngine.__init__(overlay_enabled=False,
  overlay_mode="paper")`. New step **3b** in `run()`, directly after the orchestrator
  `analyze()` call, calls the identical `run_overlay_for_decision`.
- **`src/aurel2/live/journal.py`**: added `JournalEntry.overlay_activity: list` and
  `TradeJournal.record_decision(overlay_activity=...)`. Every applied AND ignored power
  activation is journaled as `{"power", "status", "reason", "detail"}` — reusing the existing
  journal file rather than inventing a new write surface, per containment.

Both toggles **default to `False`** — the overlay is inert until something (Track A's config
artifact, per the design doc's Build tracks) turns it on. This keeps the merge safe: no
behavior change for anyone not yet wiring in the overlay.

## The three powers, as implemented

1. **`accelerate_entry`** (`powers.py::apply_accelerate_entry` + `project_next_pick`) —
   `project_next_pick` reuses `calculate_momentum_scores` (the same function
   `DualMomentumStrategy` itself calls) at a 3-month lookback as the "current short-term
   trend" proxy, restricted to the live `dm_assets` dict. A requested symbol is applied only
   if it exactly matches the projection's winner — this makes it structurally impossible for
   the overlay to introduce a symbol the core doesn't already track. Cap: 21-day cooldown
   (`state.py::can_accelerate_entry`).
2. **`lookback_override_months`** (`powers.py::apply_lookback_override`) — activation cap is
   "no override currently active AND < 2 activations this quarter"
   (`state.py::can_activate_lookback_override`); an active override auto-reverts after 30
   calendar days (used as a practical proxy for "30 consecutive trading days" — see Interface
   assumptions below) or immediately on a `risk_on` tilt view, whichever comes first. The
   caller (checker/backtest) is expected to re-run the DM ranking at
   `OverlayOutcome.active_lookback_months` when set — **not yet wired into checker.py's
   `_run_strategies`**, see Interface assumptions.
3. **`force_defensive_contest`** (`powers.py::run_defensive_contest` +
   `apply_force_defensive_contest`) — restricted ranking over the fixed
   `{GLD, AGG, SHY, IEF, TIP, CASH}` set (via `ASSET_REGISTRY`, matched by `yahoo_symbol`) plus
   the currently held asset, equal 12m-momentum thresholds (highest score wins outright, no
   asymmetric switch threshold). Rotates only if a defensive asset (or CASH) beats the held
   asset. Cap: 14-day cooldown, consumed whether or not the contest actually rotates.

Every power call returns an `OverlayJournalEntry` (`applied`/`ignored` + `reason` + `detail`)
even when it's a genuine no-op from the tilt's own choice (e.g. `accelerate_entry.symbol: null`)
— **except** that a genuinely absent request (null/false, nothing to decide) is *not* journaled
at all, since there's nothing to report; only a real request that then gets capped or rejected
produces an `ignored` row. A malformed/expired/missing tilt (`Tilt is None`) short-circuits
`apply_overlay` before touching any cap state or producing any journal rows at all — a bad tilt
file was never a valid instruction, so there's nothing to log about it beyond the
`overlay_tilt_invalid`/`overlay_tilt_expired` warning already emitted by `schema.py`.

## Majority-of-5 runner

`runner.collect_samples` makes up to 5 independent `claude -p --model claude-fable-5` calls
(via `cli_backends.call_claude_cli`, the same subprocess pattern as
`scripts/edge_decomposition/ai_tilt_harness.py`); malformed/timed-out samples are dropped from
the vote entirely rather than defaulted, so the agreement denominator is the count of *valid*
samples, not always 5. `aggregate_samples`:
- `regime_view`: plurality vote; ties broken toward the more conservative view
  (`risk_off > mixed > risk_on`) to bias toward caution when the panel is split.
- Each power activates only if **≥3** of the valid samples requested it (ties on which *symbol*/
  *lookback value* split the vote below 3 are correctly treated as no activation — see
  `test_accelerate_entry_split_votes_dont_sum`).
- `confidence` is averaged across all valid samples; `reasoning` is the samples' reasonings
  joined with `" | "`, capped at 2000 chars.
- Writes `data/{mode}/overlay_tilt.json` via `schema.write_tilt`, then `runner.git_commit_tilt`
  (`git add` + `git commit`, best-effort — a commit failure is logged but never blocks the
  write itself from taking effect for that cycle).

`runner.py`'s `__main__` block and `run_overlay_decision()` are invokable entrypoints only —
nothing schedules them. `runner.should_event_trigger(pct_move_3d)` implements the >5%-in-3-days
predicate as a pure function; wiring it to a real 3-day price window is left to whatever cron
layer eventually calls it (Track A/cadence owner).

## Claw shadow-scorer

`claw_shadow.run_claw_shadow(pack, mode, model="gpt-5.5")` sends the identical `render_prompt`
output to `codex exec --model gpt-5.5 --json` (stdin from `/dev/null`, matching the working
caller in `ai_tilt_harness.py`) and appends one record to `data/{mode}/claw_shadow.jsonl`. Its
return value is **never** read by `powers.py`/`integration.py`/any decision path — grep
confirms `claw_shadow` is referenced nowhere outside its own module and its own tests.

## Test results

```
390 passed  (pre-existing suite, unchanged — confirms no regression)
+ 83 new overlay unit tests (schema/state/powers/runner/claw_shadow/prompt)
+ 5 new end-to-end integration tests (tilt-file-on-disk -> outcome)
= 395 passed, 0 failed
```

Run: `python3 -m pytest tests/ -q` (from repo root). No test makes a live CLI call — every
`claude`/`codex` invocation in the test suite is mocked via `unittest.mock.patch` on
`_sample_one` / `call_codex_cli`.

Cap arithmetic verified precisely at boundaries: 21-day (blocked at 14d, allowed at exactly
21d), 14-day (blocked at 9d, allowed at exactly 14d), 30-day lookback expiry (active at 30d,
reverted at 31d), and the 2-per-quarter cap resetting on quarter boundary. Majority logic
verified for clean majority, conservative tie-break, unanimous, all-samples-failed fallback,
below-threshold-for-every-power, and vote-splitting-across-multiple-candidate-symbols (the case
that could otherwise wrongly sum to a false majority).

## Interface assumptions made for Tracks A/B

- **Track B's `context_pack.py`** does not exist in this worktree (by design — it's owned by
  a parallel worktree). `prompt.render_prompt(pack: dict) -> str` treats `pack` as an opaque
  JSON-serializable dict; nothing in `overlay/` accesses a named field on it. Dropping in the
  frozen v1/v2/v3 pack should require zero changes here. `render_prompt` also carries the tilt
  JSON schema and power descriptions in the prompt text itself (not the pack), so the pack only
  ever needs to supply market/position data, not schema/instructions.
- **Track A's canonical config artifact** is expected to own the `overlay_enabled` /
  `overlay_mode` toggle wiring end-to-end (env var, CLI flag, or config file → `Checker(...)`
  / `BacktestEngine(...)`). This build adds the constructor parameters (both default `False`)
  and the call sites, but does **not** wire a CLI flag or config key, since Track A owns "one
  canonical config artifact loaded by both checker.py and BacktestEngine" and I did not want to
  invent a second, competing config surface. `src/aurel2/live/daemon.py`'s two `Checker(...)`
  call sites were intentionally left unchanged (overlay stays off there until Track A wires it).
- **"30 consecutive trading days"** (lookback-override cap) is implemented in `state.py` as 30
  *calendar* days for simplicity, since `OverlayState` has no access to a trading calendar and
  loading one felt like scope creep for a cap-bookkeeping module. `powers.py`'s
  `apply_lookback_override` calls `state.get_active_lookback_override`, which does this
  calendar-day check; if Track A's fidelity guard or a later ablation shows this drifts
  meaningfully from a trading-day count (weekends/holidays inflate the calendar-day count by
  ~30%, i.e. an override could run a few real trading days longer than intended), the fix is
  localized to that one function — pass in a trading-day set the same way
  `engine/backtest.py`'s `_trading_days_since` already does.
- **`accelerate_entry`'s projection window** (`PROJECTION_TREND_MONTHS = 3` in `powers.py`) is
  my choice for "current short-term trend" — the design doc doesn't pin an exact number. 3
  months mirrors `DualMomentumStrategy`'s own existing `pilot_lookback_months` default, so it's
  consistent with how the core already defines "short-term" elsewhere in the codebase, but it
  is not evidence-tuned for this specific use and would be a reasonable ablation target.
  Track B's frozen context pack may end up carrying the AI's own view of the projected pick;
  if so, `project_next_pick` is the one function to swap or reconcile against.
- **Same-day accelerate_entry + force_defensive_contest collision**: the design doc doesn't
  specify what happens if a single tilt requests both powers and both would apply the same
  day. I made `accelerate_entry` take precedence (it's evaluated first and `force_defensive`
  only sets `decision_overrides` via `setdefault`, i.e. never overwrites an already-set
  override) since it's the tighter-capped, more surgical power. This is arbitrary and worth
  revisiting once real tilt data exists — in practice a reasonable operator prompt should
  rarely request both in the same call (one is bullish-leaning, one defensive-leaning), so
  I'd expect this branch to be rare.
- **`overlay_activity` journaling** reuses the existing `TradeJournal` / `JournalEntry`
  structure (new `overlay_activity: list` field) rather than a separate overlay-only journal
  file, since the design doc explicitly names "trade journal" as an allowed write surface and
  I didn't want a second file to reconcile.

## Commits made (this branch, `track-c-overlay`)

Not pushed — orchestrator merges into `autonomous-trading` and pushes, per instructions.

1. Added `src/aurel2/overlay/` package (schema, state, powers, prompt, cli_backends, runner,
   claw_shadow, integration) + `tests/test_overlay_*.py` (7 files, 108 test cases).
2. Wired the overlay seam into `src/aurel2/live/checker.py` (`overlay_enabled` flag, step 6c,
   `_apply_overlay` helper) and `src/aurel2/engine/backtest.py` (`overlay_enabled`/
   `overlay_mode`, step 3b), plus `overlay_activity` on `src/aurel2/live/journal.py`.
3. This report.

Exact commit hashes/messages are in `git log` on this branch; see `git log --oneline
track-c-overlay` from the repo root.
