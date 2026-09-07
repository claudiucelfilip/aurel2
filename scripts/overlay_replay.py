#!/usr/bin/env python3
"""Track D: full-system acceptance replay, overlay ON, over 2026-02-11 -> 2026-07-09.

docs/plans/2026-07-10-ai-overlay-design.md, "Build tracks / Integration":
  "full-system backtest replay over Feb-Jul 2026 as acceptance test."

Runs the REAL production seam (BacktestEngine.run() -> overlay/integration.py
-> overlay/powers.py), not a hand-rolled duplicate. Both arms run at DAILY
rebalance cadence (frequency="daily") -- matching live's actual daily checker,
not the coarser "monthly" cadence scripts/acceptance_replay.py used for the
original Track A acceptance snapshot -- so decisions are directly comparable
on the same date grid and the replay reflects live's true fidelity:

  - A2-bare:     overlay_enabled=False. Verified (this run) to reproduce the
                 SAME 3-trade sequence and symbols as the documented monthly-
                 cadence acceptance baseline (buy GLD, sell GLD + buy XLK) --
                 see docs/plans/build-reports/track-a-report.md -- with trade
                 DATES shifted earlier (daily catches the exact triggering
                 day; monthly waits for month-end). This date shift is a
                 known, expected cadence-granularity effect, not a fidelity
                 bug; see the acceptance report for the verification.
  - A2+overlay:  overlay_enabled=True, overlay_mode="replay". An
                 `overlay_refresh` callback fires once per ISO week (the
                 first business day of a new ISO week under daily cadence is
                 always Monday or the first trading day after a Monday
                 holiday -- i.e. this IS the weekly Monday-anchored cadence
                 from the design doc), rebuilding the frozen v2 context pack
                 and calling run_overlay_decision (5 independent
                 claude-fable-5 CLI samples), writing
                 data/replay/overlay_tilt.json BEFORE the engine reads it via
                 the normal run_overlay_for_decision seam on every daily
                 decision -- mirrors live's external weekly cron + daily
                 checker in backtest form (see the overlay_refresh docstring
                 in src/aurel2/engine/backtest.py).

Data isolation: mode="replay" confines every write to data/replay/
(overlay_tilt.json, overlay_state.json) -- paper/live state is never touched.
data/replay/ is wiped at the start of every run (pure scratch state, not
committed).

Crash safety: every raw CLI sample (parsed tilt + raw text) is cached by
content hash (sha256 of the exact prompt) in
data/edge_decomposition/overlay_replay_cache.jsonl, appended before
aggregation. Rerunning this script re-loads the cache and only calls the CLI
for cells that aren't already there.

Output: data/edge_decomposition/overlay_replay.json -- equity curves for
A2+overlay / A2-bare / SPY buy-hold / QQQ buy-hold, every trade of both arms,
every overlay journal row (applied AND ignored) per week, and the weekly
regime_view/sample_agreement series.
"""

import hashlib
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
logging.disable(logging.WARNING)

import pandas as pd

import aurel2.overlay.integration as overlay_integration
import aurel2.overlay.powers as powers_mod
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, SignalAction
from aurel2.config.canonical import CANONICAL_CONFIG, live_asset_registry
from aurel2.engine.backtest import BacktestEngine
from aurel2.overlay.context_pack import build_frozen_context_pack
from aurel2.overlay.runner import collect_samples, aggregate_samples, N_SAMPLES
from aurel2.overlay.schema import write_tilt, tilt_path_for_mode, DEFENSIVE_CONTEST_SYMBOLS
from aurel2.overlay.state import state_path_for_mode

ACCEPTANCE_SNAPSHOT = REPO_ROOT / "data" / "acceptance" / "price_snapshot_2026-02-11_2026-07-09.csv"
EDGE_DECOMP_SNAPSHOT = REPO_ROOT / "data" / "edge_decomposition" / "snapshots" / "price_snapshot_raw.csv"
CACHE_PATH = REPO_ROOT / "data" / "edge_decomposition" / "overlay_replay_cache.jsonl"
OUT_PATH = REPO_ROOT / "data" / "edge_decomposition" / "overlay_replay.json"
REPLAY_MODE = "replay"
REPLAY_DATA_DIR = REPO_ROOT / "data" / "replay"
CACHE_ONLY = False
VARIANT: dict = {}
TILTS_FROM: dict = {}   # as_of -> weekly_overlay_log entry from a reference replay
TILTS_FROM_PATH = None


def parse_variant_args():
    """--variant NAME runs the overlay arm under research knobs (see
    OverlaySettings) and writes to overlay_replay_<NAME>.json with its own
    scratch mode dir, so variants can run in parallel without clobbering."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default=None, help="name; omit for the canonical default run")
    ap.add_argument("--accel", choices=["on", "off"], default=None)
    ap.add_argument("--accel-candidates", type=int, default=None)
    ap.add_argument("--mixed-action", choices=["none", "lookback_3m", "lookback_6m", "defensive_contest"], default=None)
    ap.add_argument("--cache-only", action="store_true", help="fail on a cache miss instead of calling the CLI")
    ap.add_argument("--tilts-from", default=None, help="reference overlay_replay.json whose weekly_overlay_log supplies every week's aggregated tilt (no CLI, no cache)")
    return ap.parse_args()


def apply_variant(args):
    """Patch the overlay config the powers seam reads; defaults untouched."""
    global REPLAY_MODE, REPLAY_DATA_DIR, OUT_PATH, CACHE_ONLY, VARIANT, TILTS_FROM, TILTS_FROM_PATH
    from dataclasses import replace as dc_replace
    CACHE_ONLY = bool(args.cache_only)
    if args.tilts_from:
        TILTS_FROM_PATH = args.tilts_from
        ref = json.loads(Path(args.tilts_from).read_text())
        TILTS_FROM = {w["as_of"]: w for w in ref["weekly_overlay_log"]}
    if not args.variant:
        return
    knobs = {}
    if args.accel is not None:
        knobs["accelerate_entry_enabled"] = args.accel == "on"
    if args.accel_candidates is not None:
        knobs["accelerate_entry_candidates"] = args.accel_candidates
    if args.mixed_action is not None:
        knobs["mixed_regime_action"] = args.mixed_action
    cfg = dc_replace(powers_mod.CANONICAL_CONFIG, overlay=dc_replace(powers_mod.CANONICAL_CONFIG.overlay, **knobs))
    powers_mod.CANONICAL_CONFIG = cfg
    VARIANT = {"name": args.variant, **knobs}
    REPLAY_MODE = f"replay-{args.variant}"
    REPLAY_DATA_DIR = REPO_ROOT / "data" / REPLAY_MODE
    OUT_PATH = REPO_ROOT / "data" / "edge_decomposition" / f"overlay_replay_{args.variant}.json"

START = date(2026, 2, 11)
END = date(2026, 7, 9)
MODEL = "claude-fable-5"

# Benchmarks not in the DM universe (buy-and-hold comparisons + accelerate_entry
# projection target) -- pulled from the edge_decomposition snapshot (same raw-close
# fetch method, auto_adjust=False) and merged into the DM-universe price frame used
# for this replay only. Neither pinned CSV is modified.
EXTRA_BENCHMARK_SYMBOLS = ["QQQ"]


def load_merged_snapshot() -> pd.DataFrame:
    """DM-universe + SPY prices (data/acceptance) + QQQ (data/edge_decomposition),
    both raw-close (auto_adjust=False) fetches -- consistent basis, merged only
    for this replay's benchmark/projection needs.
    """
    base = pd.read_csv(ACCEPTANCE_SNAPSHOT, parse_dates=["date"])
    base["date"] = base["date"].dt.date

    extra = pd.read_csv(EDGE_DECOMP_SNAPSHOT, parse_dates=["date"])
    extra["date"] = extra["date"].dt.date
    extra = extra[extra["symbol"].isin(EXTRA_BENCHMARK_SYMBOLS)][["date", "symbol", "close"]]

    merged = pd.concat([base[["date", "symbol", "close"]], extra], ignore_index=True)
    merged = merged.drop_duplicates(subset=["date", "symbol"]).sort_values(["symbol", "date"]).reset_index(drop=True)
    return merged


def cache_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:24]


def load_cache() -> dict:
    cache = {}
    if CACHE_PATH.exists():
        for line in CACHE_PATH.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cache[rec["cache_key"]] = rec
    return cache


def append_cache(rec: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def cached_collect_samples(pack: dict, as_of: date, cache: dict) -> list[dict]:
    """Same contract as overlay.runner.collect_samples but caches each of the
    n_samples independent CLI calls by content hash of (prompt + sample index),
    so a crash mid-week never loses already-made calls.
    """
    from aurel2.overlay.prompt import render_prompt
    from aurel2.overlay.runner import _sample_one, _valid_raw_sample

    prompt = render_prompt(pack)
    base_key = cache_key(prompt)
    samples = []
    for i in range(N_SAMPLES):
        key = f"{as_of.isoformat()}:{base_key}:s{i}"
        rec = cache.get(key)
        if rec is None and CACHE_ONLY:
            raise RuntimeError(f"cache miss for {as_of} sample {i} (--cache-only): refusing to call the CLI")
        if rec is None:
            parsed = _sample_one(pack, model=MODEL)
            rec = {"cache_key": key, "date": as_of.isoformat(), "sample_idx": i, "parsed": parsed}
            append_cache(rec)
            cache[key] = rec
        parsed = rec.get("parsed")
        if parsed is not None and _valid_raw_sample(parsed):
            samples.append(parsed)
    return samples


class HoldingTracker:
    """Mirrors BacktestEngine.run()'s DM -> orchestrator -> overlay -> execute
    resolution ONLY to know current_holding_symbol/days_held at the moment
    overlay_refresh fires (before the real engine has updated its own state
    for this rebalance date) -- needed to build the context pack and to run
    apply_overlay's own projection/contest logic identically to what the real
    seam will do a few lines later in BacktestEngine.run(). This does not
    execute trades or touch cash/shares; the real engine owns portfolio state.
    """

    def __init__(self, engine: BacktestEngine):
        self.engine = engine
        self.current_holding: AssetClass | None = None
        self.current_holding_symbol: str | None = None
        self.since_date: date | None = None

    def days_held(self, as_of: date) -> int:
        if self.since_date is None:
            return 0
        return (as_of - self.since_date).days

    def advance(self, prices: pd.DataFrame, rebal_date: date) -> dict:
        """Resolve today's decision (DM -> orchestrator -> overlay) exactly as
        BacktestEngine.run() will, update the mirror, and return the
        deterministic (pre-overlay) DM signal dict for the context pack.
        """
        engine = self.engine
        signal = engine.dual_momentum.generate_signal(
            prices=prices, calc_date=rebal_date, current_holding=self.current_holding
        )
        det_signal = {
            "action": signal.action.value if hasattr(signal.action, "value") else str(signal.action),
            "symbol": signal.asset.symbol if getattr(signal, "asset", None) else None,
            "reason": getattr(signal, "reasoning", None) or getattr(signal, "reason", ""),
        }

        normalized = engine._normalize_signal(signal)
        market_context = engine._build_market_context(prices, rebal_date)
        decision = engine.orchestrator.analyze(
            signals={"dual_momentum": normalized},
            market_context=market_context,
            current_holding=self.current_holding_symbol,
        )

        action = decision.action
        target_symbol = decision.asset_symbol
        if action == SignalAction.BUY and target_symbol:
            if target_symbol != self.current_holding_symbol:
                self.current_holding = engine._symbol_to_asset_class(target_symbol)
                self.current_holding_symbol = target_symbol
                self.since_date = rebal_date
        elif action == SignalAction.SELL:
            if self.current_holding_symbol not in (None, "CASH"):
                self.current_holding = AssetClass.CASH
                self.current_holding_symbol = "CASH"
                self.since_date = rebal_date

        return det_signal

    def apply_overlay_outcome(self, action: SignalAction | None, asset_symbol: str | None, rebal_date: date):
        """If the overlay itself overrides today's decision, fold that into the
        mirror too, so next week's days_held/current_position reflects it."""
        if action == SignalAction.BUY and asset_symbol and asset_symbol != self.current_holding_symbol:
            self.current_holding = self.engine._symbol_to_asset_class(asset_symbol)
            self.current_holding_symbol = asset_symbol
            self.since_date = rebal_date


def make_overlay_refresh(cache: dict, weekly_log: list[dict]):
    """Returns a callable(rebal_date, prices) for BacktestEngine(overlay_refresh=...).

    Regenerates data/replay/overlay_tilt.json only on the first rebalance date
    that falls in a new ISO week since the last refresh (Monday-anchored weekly
    cadence per docs/plans/2026-07-10-ai-overlay-design.md, "Decision protocol").
    Every regeneration is logged to weekly_log (regime_view, sample_agreement,
    caps) for the acceptance report.
    """
    state = {"last_week": None, "engine": None, "tracker": None}

    def refresh(rebal_date: date, prices: pd.DataFrame):
        # Daily cadence (matching live's daily checker) means rebal_date walks
        # every business day; the first business day of a new ISO week is
        # always Monday (or the first trading day after a Monday holiday),
        # so gating on ISO-week transitions IS the "weekly Monday pre-open"
        # cadence from the design doc, without hardcoding a weekday check
        # that would misfire around holidays.
        wk = rebal_date.isocalendar()[:2]
        if wk == state["last_week"]:
            return  # already refreshed this ISO week
        state["last_week"] = wk

        tracker: HoldingTracker = state["tracker"]
        det_signal = tracker.advance(prices, rebal_date)

        if TILTS_FROM:
            # Replay the reference run's aggregated AI decisions verbatim so an
            # A/B isolates the power mechanics, not model/prompt drift.
            w = TILTS_FROM.get(rebal_date.isoformat())
            if w is None:
                state.setdefault("errors", []).append(f"no reference tilt for {rebal_date}")
                raise RuntimeError(f"--tilts-from has no tilt for {rebal_date}")
            from aurel2.overlay.runner import DEFAULT_TTL_DAYS
            tilt_dict = {
                "as_of": w["as_of"],
                "expires": (rebal_date + timedelta(days=DEFAULT_TTL_DAYS)).isoformat(),
                "regime_view": w["regime_view"],
                "confidence": w["confidence"],
                "powers": w["powers_requested"],
                "reasoning": f"replayed from {TILTS_FROM_PATH}",
                "samples": w["samples"],
                "sample_agreement": w["sample_agreement"],
            }
        else:
            pack = build_frozen_context_pack(
                prices=prices,
                calc_date=rebal_date,
                dm_assets=tracker.engine.dual_momentum.assets,
                current_holding_symbol=tracker.current_holding_symbol,
                days_held=tracker.days_held(rebal_date),
                deterministic_signal=det_signal,
            )
            try:
                samples = cached_collect_samples(pack, rebal_date, cache)
            except Exception as e:
                state.setdefault("errors", []).append(f"{rebal_date}: {e}")
                raise
            tilt_dict = aggregate_samples(samples, as_of=rebal_date)
        tilt_path = tilt_path_for_mode(REPLAY_MODE)
        write_tilt(tilt_path, tilt_dict)

        weekly_log.append({
            "as_of": rebal_date.isoformat(),
            "iso_week": f"{wk[0]}-W{wk[1]:02d}",
            "regime_view": tilt_dict["regime_view"],
            "confidence": tilt_dict["confidence"],
            "samples": tilt_dict["samples"],
            "sample_agreement": tilt_dict["sample_agreement"],
            "powers_requested": tilt_dict["powers"],
            "current_position_at_refresh": tracker.current_holding_symbol,
            "days_held_at_refresh": tracker.days_held(rebal_date),
        })

    return refresh, state


def make_journal_capturing_wrapper(overlay_journal_log: list[dict], tracker: "HoldingTracker"):
    """Wraps overlay.integration.run_overlay_for_decision so every call the
    real engine makes (once per rebalance date, applied AND ignored powers
    alike) is captured for the acceptance report -- without duplicating or
    re-invoking the seam (that would double-consume cap state). The real
    function is still the one that runs; this only observes its return value,
    and folds an applied override back into the tracker's holding mirror so
    next week's context pack sees the overlay-adjusted position.
    """
    real_fn = overlay_integration.run_overlay_for_decision

    def wrapped(*args, **kwargs):
        outcome = real_fn(*args, **kwargs)
        today = kwargs.get("today")
        if today is None and len(args) > 1:
            today = args[1]
        if outcome.journal_rows:
            overlay_journal_log.append({
                "date": today.isoformat() if hasattr(today, "isoformat") else str(today),
                "rows": outcome.journal_rows,
            })
        if outcome.action is not None and outcome.asset_symbol is not None and today is not None:
            tracker.apply_overlay_outcome(outcome.action, outcome.asset_symbol, today)
        return outcome

    return wrapped


def build_overlay_engine() -> BacktestEngine:
    engine = BacktestEngine(
        initial_capital=10000,
        use_ai=False,
        overlay_enabled=True,
        overlay_mode=REPLAY_MODE,
    )
    return engine


def build_bare_engine() -> BacktestEngine:
    # Same non-cadence config as scripts/acceptance_replay.py (use_ai=False,
    # default correlation_guard/sideways_hold from CANONICAL_CONFIG) -- see
    # this script's module docstring for why frequency="daily" (not "monthly"
    # like acceptance_replay.py) is used for both arms here.
    return BacktestEngine(initial_capital=10000, use_ai=False)


def buy_and_hold(prices: pd.DataFrame, symbol: str, start: date, end: date, initial_capital: float = 10000) -> dict:
    sym_prices = prices[prices["symbol"] == symbol].sort_values("date")
    start_rows = sym_prices[sym_prices["date"] >= start]
    end_rows = sym_prices[sym_prices["date"] <= end]
    if start_rows.empty or end_rows.empty:
        return {"symbol": symbol, "equity_curve": [], "final_return": None}
    entry_price = float(start_rows.iloc[0]["close"])
    shares = initial_capital / entry_price
    curve = []
    for _, row in sym_prices[(sym_prices["date"] >= start) & (sym_prices["date"] <= end)].iterrows():
        curve.append({"date": row["date"].isoformat(), "value": round(shares * float(row["close"]), 2)})
    final_value = shares * float(end_rows.iloc[-1]["close"])
    return {
        "symbol": symbol,
        "equity_curve": curve,
        "final_value": round(final_value, 2),
        "final_return": round(final_value / initial_capital - 1, 4),
    }


def trades_to_dicts(trades) -> list[dict]:
    out = []
    for t in trades:
        out.append({
            "date": t.date.isoformat(),
            "action": t.action.value if hasattr(t.action, "value") else str(t.action),
            "symbol": t.asset.symbol if getattr(t, "asset", None) else None,
            "shares": float(t.shares),
            "price": t.price,
            "commission": t.commission,
        })
    return out


def snapshots_to_curve(snapshots) -> list[dict]:
    return [{"date": s.date.isoformat(), "value": round(float(s.total_value), 2), "holding": s.holding_symbol} for s in snapshots]


def main():
    apply_variant(parse_variant_args())
    if VARIANT:
        print(f"Variant: {VARIANT}")
    print(f"Wiping {REPLAY_DATA_DIR} (scratch state only)...")
    if REPLAY_DATA_DIR.exists():
        shutil.rmtree(REPLAY_DATA_DIR)
    REPLAY_DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading merged price snapshot (acceptance DM-universe+SPY, + QQQ from edge_decomposition)...")
    prices = load_merged_snapshot()
    print(f"  {len(prices)} rows, symbols={sorted(prices['symbol'].unique())}")

    cache = load_cache()
    print(f"  {len(cache)} cached CLI samples loaded from {CACHE_PATH}")

    # ---------------------------------------------------------------
    # Arm 1: A2-bare (overlay OFF). Daily cadence matches live's actual daily
    # checker (fidelity-true, per orchestrator guidance during this run) --
    # trade sequence/symbols verified to match the documented monthly-cadence
    # acceptance baseline (buy GLD -> sell GLD/buy XLK), with dates shifted
    # earlier than the monthly baseline purely from daily vs monthly rebalance
    # granularity (monthly waits for month-end; daily catches the exact
    # triggering day) -- see this script's docstring and the report.
    # ---------------------------------------------------------------
    print("\n=== Running A2-bare (overlay OFF, daily cadence) ===")
    bare_engine = build_bare_engine()
    bare_result = bare_engine.run(prices=prices, start_date=START, end_date=END, frequency="daily")

    # ---------------------------------------------------------------
    # Arm 2: A2+overlay (overlay ON, daily cadence, weekly-refreshed tilt via
    # the real seam). Both arms share daily cadence so trades are directly
    # comparable on the same decision-date grid.
    # ---------------------------------------------------------------
    print("\n=== Running A2+overlay (overlay ON, daily cadence, weekly tilt refresh) ===")
    overlay_engine = build_overlay_engine()
    weekly_log: list[dict] = []
    overlay_journal_log: list[dict] = []
    refresh_fn, refresh_state = make_overlay_refresh(cache, weekly_log)
    refresh_state["engine"] = overlay_engine
    refresh_state["tracker"] = HoldingTracker(overlay_engine)
    overlay_engine.overlay_refresh = refresh_fn

    real_run_overlay_for_decision = overlay_integration.run_overlay_for_decision
    overlay_integration.run_overlay_for_decision = make_journal_capturing_wrapper(
        overlay_journal_log, refresh_state["tracker"]
    )
    try:
        overlay_result = overlay_engine.run(prices=prices, start_date=START, end_date=END, frequency="daily")
    finally:
        overlay_integration.run_overlay_for_decision = real_run_overlay_for_decision

    # ---------------------------------------------------------------
    # Benchmarks
    # ---------------------------------------------------------------
    print("\n=== Building benchmarks ===")
    spy_bh = buy_and_hold(prices, "SPY", START, END)
    qqq_bh = buy_and_hold(prices, "QQQ", START, END)

    out = {
        "window": {"start": START.isoformat(), "end": END.isoformat()},
        "model": MODEL,
        "n_samples": N_SAMPLES,
        "context_pack_version": CANONICAL_CONFIG.overlay.context_pack_version,
        "variant": VARIANT or None,
        "arms": {
            "a2_overlay": {
                "final_value": round(overlay_result.final_value, 2),
                "final_return": round(overlay_result.total_return, 4),
                "equity_curve": snapshots_to_curve(overlay_result.snapshots),
                "trades": trades_to_dicts(overlay_result.trades),
                "num_trades": len(overlay_result.trades),
            },
            "a2_bare": {
                "final_value": round(bare_result.final_value, 2),
                "final_return": round(bare_result.total_return, 4),
                "equity_curve": snapshots_to_curve(bare_result.snapshots),
                "trades": trades_to_dicts(bare_result.trades),
                "num_trades": len(bare_result.trades),
            },
            "spy_buy_hold": spy_bh,
            "qqq_buy_hold": qqq_bh,
        },
        "weekly_overlay_log": weekly_log,
        "overlay_journal_log": overlay_journal_log,
    }

    refresh_errors = refresh_state.get("errors", [])
    if refresh_errors or not weekly_log:
        print(f"\nOVERLAY ARM INVALID: {len(refresh_errors)} refresh error(s), {len(weekly_log)} weeks refreshed -- "
              "the engine swallows refresh errors and silently runs a bare arm. Not writing output.", file=sys.stderr)
        for e in refresh_errors[:5]:
            print("  " + str(e), file=sys.stderr)
        sys.exit(2)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nWrote {OUT_PATH}")
    print(f"A2+overlay final return: {overlay_result.total_return:.2%}  ({len(overlay_result.trades)} trades)")
    print(f"A2-bare final return:    {bare_result.total_return:.2%}  ({len(bare_result.trades)} trades)")
    print(f"SPY buy-hold return:     {spy_bh['final_return']:.2%}" if spy_bh["final_return"] is not None else "SPY: n/a")
    print(f"QQQ buy-hold return:     {qqq_bh['final_return']:.2%}" if qqq_bh["final_return"] is not None else "QQQ: n/a")
    print(f"Overlay weeks refreshed: {len(weekly_log)}")


if __name__ == "__main__":
    main()
