#!/usr/bin/env python3
"""Arm 1 of the edge-decomposition experiment: replay live-trader's deterministic
core (daily_runner.py) with all discretionary/AI tilts forced to zero.

Imports the actual feature/scoring/selection functions from live-trader
verbatim (via sys.path) so this can't drift from the real logic. Only the
data source (yfinance instead of live Alpaca bars) and the day-by-day
simulation loop (instead of a single cron invocation) are new.

Trades are executed at the daily close of the decision day: each day's bars
(through that day's close) are used to compute features/regime/score for that
same day, and any resulting BUY/SELL/ROTATE is filled at that day's close.
This is a simplification vs. the live system (which runs intraday against
whatever the latest available bar is) but is the standard backtest convention
and is noted in the output.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

LIVE_TRADER_DIR = "/Users/claudiu/.openclaw/workspace/live-trader"
sys.path.insert(0, LIVE_TRADER_DIR)

# Import the real deterministic engine verbatim — no reimplementation.
# (daily_runner.py imports alpaca/trader/news_ingest at module level for its
# main(), which we don't call and don't want; those imports would require
# broker credentials we don't have/want here. So we exec just the functions
# we need out of the source instead of `import daily_runner`, to avoid
# pulling in the broker-dependent imports at the top of that file.)
_dr_source = Path(LIVE_TRADER_DIR, "daily_runner.py").read_text()
# BASE_DIR/Path is used for TRADE_LOG etc. in the sliced section; harmless
# (those paths are never read since we don't call get_last_rotation_time()).
_dr_globals: dict = {
    "__name__": "daily_runner_core", "__file__": str(Path(LIVE_TRADER_DIR, "daily_runner.py")),
    "Path": Path, "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
}
# Strip the broker/news-ingest imports (lines 16-22) — everything below them
# (WATCHLIST..choose_target) has zero dependency on those imports.
_core_start = _dr_source.index("WATCHLIST = ")
_core_end = _dr_source.index("def main():")
exec(compile(_dr_source[_core_start:_core_end], "daily_runner_core", "exec"), _dr_globals)

WATCHLIST = _dr_globals["WATCHLIST"]
RISK_ASSETS = _dr_globals["RISK_ASSETS"]
DEFENSIVE = _dr_globals["DEFENSIVE"]
build_features = _dr_globals["build_features"]
infer_regime = _dr_globals["infer_regime"]
conviction_score = _dr_globals["conviction_score"]
choose_target = _dr_globals["choose_target"]

HARD_STOP_VALUE = _dr_globals["HARD_STOP_VALUE"]
SOFT_RISK_OFF_VALUE = _dr_globals["SOFT_RISK_OFF_VALUE"]
ROTATION_MIN_DAYS = _dr_globals["ROTATION_MIN_DAYS"]
STRONG_EDGE_THRESHOLD = _dr_globals["STRONG_EDGE_THRESHOLD"]

WINDOW_START = date(2026, 2, 11)
WINDOW_END = date(2026, 7, 8)
FETCH_START = "2025-06-01"  # warms the 150-day/80-bar lookback well before window start
NO_TILT = {"symbol_bias": {}, "regime_bias": {}, "notes": ""}

# daily_runner.py's distinctive reasoning format first appears in the real trade
# log on this date; before it, live trades were driven by a different (AI-scored)
# system entirely, so this is the only era where lt_core is a fair comparison arm.
DAILY_RUNNER_ERA_START = date(2026, 4, 1)

OUT_PATH = Path("/Users/claudiu/Sites/aurel2/data/edge_decomposition/lt_core.json")


@dataclass
class Bar:
    close: float


def fetch_bars() -> dict[str, list[tuple[date, float]]]:
    """Daily auto-adjusted closes per symbol, as (date, close) pairs."""
    end = (WINDOW_END + timedelta(days=3)).isoformat()
    raw = yf.download(
        WATCHLIST, start=FETCH_START, end=end, auto_adjust=True, progress=False, group_by="ticker",
    )
    out: dict[str, list[tuple[date, float]]] = {}
    for sym in WATCHLIST:
        closes = raw[sym]["Close"].dropna()
        out[sym] = [(d.date(), float(v)) for d, v in closes.items()]
    return out


def bars_as_of(all_bars: dict[str, list[tuple[date, float]]], as_of: date) -> dict[str, list[Bar]]:
    """Mimic the shape build_features() expects: {symbol: [Bar(close), ...]} truncated to as_of."""
    out = {}
    for sym, series in all_bars.items():
        closes = [c for d, c in series if d <= as_of]
        if closes:
            out[sym] = [Bar(close=c) for c in closes]
    return out


def trading_days(all_bars: dict[str, list[tuple[date, float]]]) -> list[date]:
    days = sorted({d for series in all_bars.values() for d, _ in series})
    return [d for d in days if WINDOW_START <= d <= WINDOW_END]


def build_subwindows(daily_equity: list[dict], decisions: list[dict]) -> dict:
    """Slice the full-window sim into named eras, re-normalizing equity to 100.0
    at the era's first day. Reuses the already-simulated daily_equity/decisions
    (position/cash state as of the era start carries over from the full sim) —
    no re-simulation, so no re-seeding-from-cash convention is needed.
    """
    out = {}
    for label, start, end in [
        ("2026-04-01_to_2026-07-08", DAILY_RUNNER_ERA_START, WINDOW_END),
    ]:
        rows = [r for r in daily_equity if start.isoformat() <= r["date"] <= end.isoformat()]
        if not rows:
            continue
        decs = [d for d in decisions if start.isoformat() <= d["date"] <= end.isoformat()]

        base_equity = rows[0]["equity"]
        norm_equity = []
        running_peak = 100.0
        max_dd = 0.0
        for r in rows:
            eq = round((r["equity"] / base_equity) * 100.0, 4)
            running_peak = max(running_peak, eq)
            dd = ((eq - running_peak) / running_peak) * 100.0
            max_dd = min(max_dd, dd)
            norm_equity.append({"date": r["date"], "equity": eq, "holding": r["holding"]})

        trades = [d for d in decs if d["action"] in ("BUY", "SELL")]
        holdings_timeline = []
        last_holding = object()  # sentinel, never equals a real value
        for r in rows:
            if r["holding"] != last_holding:
                holdings_timeline.append({"date": r["date"], "holding": r["holding"]})
                last_holding = r["holding"]

        final_return_pct = ((norm_equity[-1]["equity"] - 100.0) / 100.0) * 100.0

        out[label] = {
            "window": {"start": rows[0]["date"], "end": rows[-1]["date"]},
            "start_equity": 100.0,
            "renormalized_from_actual_equity": round(base_equity, 4),
            "daily_equity": norm_equity,
            "holdings_timeline": holdings_timeline,
            "final_return_pct": round(final_return_pct, 4),
            "max_drawdown_pct": round(max_dd, 4),
            "n_trades": len(trades),
            "notes": [
                "Sliced from the full 2026-02-11 sim, not re-simulated: position/cash state carries "
                "over from whatever the full sim was holding as of the day before this era starts, "
                "so the 150-day feature lookback stays warm across the era boundary.",
                f"Equity re-normalized to 100.0 at {rows[0]['date']} by dividing every day's actual "
                f"simulated equity by that day's value ({round(base_equity, 4)}) and scaling by 100.",
            ],
        }
    return out


def main():
    print("Fetching daily bars from yfinance...", file=sys.stderr)
    all_bars = fetch_bars()
    days = trading_days(all_bars)
    print(f"{len(days)} trading days in window {WINDOW_START} - {WINDOW_END}", file=sys.stderr)

    cash = 100.0
    shares = 0.0
    holding: str | None = None
    last_rotation_day: date | None = None
    peak_equity = 100.0

    daily_equity = []
    decisions = []
    n_trades = 0

    def price_on(sym: str, d: date) -> float | None:
        for dd, c in all_bars[sym]:
            if dd == d:
                return c
        return None

    for d in days:
        bars_today = bars_as_of(all_bars, d)
        features = build_features(bars_today)

        px = price_on(holding, d) if holding else None
        equity = cash + (shares * px if (holding and px is not None) else 0.0)

        if not features:
            decisions.append({
                "date": d.isoformat(), "action": "HOLD", "symbol": holding,
                "score_gap": None, "reason": "insufficient feature history",
            })
            daily_equity.append({"date": d.isoformat(), "equity": round(equity, 4), "holding": holding})
            continue

        regime = infer_regime(features)

        # --- Hard stop: liquidate to cash ---
        if equity < HARD_STOP_VALUE and holding:
            cash = equity
            decisions.append({
                "date": d.isoformat(), "action": "SELL", "symbol": holding,
                "score_gap": None, "reason": f"Hard stop < ${HARD_STOP_VALUE:.0f}: liquidating to cash.",
            })
            n_trades += 1
            holding = None
            shares = 0.0
            daily_equity.append({"date": d.isoformat(), "equity": round(cash, 4), "holding": None})
            peak_equity = max(peak_equity, cash)
            continue

        target_symbol, target_row, ranked = choose_target(features, regime, NO_TILT)

        if not holding:
            if equity <= SOFT_RISK_OFF_VALUE and regime["name"] == "risk_off" and target_row["score"] < 1.0:
                decisions.append({
                    "date": d.isoformat(), "action": "HOLD", "symbol": None,
                    "score_gap": None,
                    "reason": "Soft risk-off and weak conviction; staying cash.",
                })
                daily_equity.append({"date": d.isoformat(), "equity": round(cash, 4), "holding": None})
                peak_equity = max(peak_equity, cash)
                continue

            notional = cash * 0.99
            if notional > 1:
                px_buy = price_on(target_symbol, d)
                shares = notional / px_buy
                cash -= notional
                holding = target_symbol
                last_rotation_day = d
                n_trades += 1
                decisions.append({
                    "date": d.isoformat(), "action": "BUY", "symbol": target_symbol,
                    "score_gap": None,
                    "reason": (
                        f"Initiating position via discretionary engine. "
                        f"Target={target_symbol}, conviction={target_row['score']:.2f}, regime={regime['name']}."
                    ),
                })
            equity = cash + shares * price_on(holding, d) if holding else cash
            daily_equity.append({"date": d.isoformat(), "equity": round(equity, 4), "holding": holding})
            peak_equity = max(peak_equity, equity)
            continue

        current_row = next((r for r in ranked if r["symbol"] == holding), None)
        if not current_row:
            current_row = {"symbol": holding, "score": -999.0}

        score_gap = target_row["score"] - current_row["score"]

        cooldown_block = False
        if last_rotation_day:
            cooldown_block = (d - last_rotation_day).days < ROTATION_MIN_DAYS

        should_rotate = (
            target_symbol != holding
            and score_gap >= STRONG_EDGE_THRESHOLD
            and not cooldown_block
        )

        if should_rotate:
            px_sell = price_on(holding, d)
            proceeds = shares * px_sell
            cash += proceeds
            sold_symbol = holding
            shares = 0.0
            holding = None
            n_trades += 1
            decisions.append({
                "date": d.isoformat(), "action": "SELL", "symbol": sold_symbol,
                "score_gap": round(score_gap, 4),
                "reason": (
                    f"Discretionary rotation: {sold_symbol}({current_row['score']:.2f}) -> "
                    f"{target_symbol}({target_row['score']:.2f}), edge {score_gap:.2f}, regime={regime['name']}."
                ),
            })

            notional = cash * 0.99
            if notional > 1:
                px_buy = price_on(target_symbol, d)
                shares = notional / px_buy
                cash -= notional
                holding = target_symbol
                last_rotation_day = d
                n_trades += 1
                decisions.append({
                    "date": d.isoformat(), "action": "BUY", "symbol": target_symbol,
                    "score_gap": round(score_gap, 4),
                    "reason": (
                        f"Conviction leader {target_symbol}. "
                        f"trend={target_row['components']['trend_component']:.2f}, "
                        f"risk_penalty={target_row['components']['risk_penalty']:.2f}, "
                        f"regime_adj={target_row['components']['regime_adjustment']:.2f}."
                    ),
                })
        else:
            decisions.append({
                "date": d.isoformat(), "action": "HOLD", "symbol": holding,
                "score_gap": round(score_gap, 4),
                "reason": (
                    f"HOLD {holding}. target={target_symbol} gap={score_gap:.2f}, "
                    f"regime={regime['name']}, cooldown_block={cooldown_block}."
                ),
            })

        px_eod = price_on(holding, d) if holding else None
        equity = cash + (shares * px_eod if (holding and px_eod is not None) else 0.0)
        daily_equity.append({"date": d.isoformat(), "equity": round(equity, 4), "holding": holding})
        peak_equity = max(peak_equity, equity)

    final_equity = daily_equity[-1]["equity"] if daily_equity else 100.0
    final_return_pct = ((final_equity - 100.0) / 100.0) * 100.0

    max_dd = 0.0
    running_peak = 100.0
    for row in daily_equity:
        running_peak = max(running_peak, row["equity"])
        dd = ((row["equity"] - running_peak) / running_peak) * 100.0
        max_dd = min(max_dd, dd)

    subwindows = build_subwindows(daily_equity, decisions)

    output = {
        "arm": "lt_core",
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "start_equity": 100.0,
        "daily_equity": daily_equity,
        "decisions": decisions,
        "final_return_pct": round(final_return_pct, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "n_trades": n_trades,
        "subwindows": subwindows,
        "notes": [
            "Deterministic core only: symbol_bias and regime_bias forced to {} for every day "
            "(equivalent to live-trader running with zero discretionary/AI tilt).",
            "Functions (build_features, infer_regime, conviction_score, choose_target, WATCHLIST, "
            "RISK_ASSETS, DEFENSIVE, and threshold constants) are imported verbatim from "
            f"{LIVE_TRADER_DIR}/daily_runner.py via exec of the source (broker/news-ingest imports "
            "at the top of that file were skipped since main() is not invoked).",
            "Trades execute at the daily close of the decision day: features/regime/score for day D "
            "use bars through D's close, and any resulting BUY/SELL/ROTATE fills at D's close. Live-trader "
            "actually runs intraday (afternoon cron) against the latest available bar, which may not "
            "yet be day D's finalized close, so this is a simplifying backtest convention.",
            "Price data: yfinance daily auto-adjusted closes, fetched from "
            f"{FETCH_START} to warm the 150-day/80-bar lookback before the window start.",
            "Position sizing: 99% of cash deployed on every BUY, matching live-trader's "
            "`buying_power * 0.99` notional logic; single-position (no partial holds).",
            "get_last_rotation_time() in the live system is broker-order-based; this replay "
            "approximates it with the in-sim last BUY/SELL day, which is equivalent absent "
            "any out-of-band manual trades (none exist in this deterministic-only simulation).",
            "FIDELITY CAVEAT (important for downstream attribution): the actual trades.jsonl shows "
            "daily_runner.py's distinctive reasoning format (e.g. 'Discretionary rotation: X(score) -> "
            "Y(score), edge N, regime=...') first appearing on 2026-04-01. Entries before that date "
            "(2026-02-11 through 2026-03-25, e.g. the initial GLD buy on 2026-02-11) use a different "
            "reasoning shape (free-text 'momentum_snapshot'/'scores' fields, LLM-authored prose) that "
            "does not match this engine's score/threshold logic at all — those were very likely produced "
            "by a different (AI-driven) decision system, not by daily_runner.py with a small tilt. "
            "This replay's day-one pick of XLE (vs. the real system's GLD) reflects that: it is genuine "
            "engine divergence from a different live decision-maker, not a data/logic bug in this replay. "
            "Treat pre-2026-04-01 real trades as out of scope for a 'daily_runner.py + tilt' attribution "
            "comparison; the deterministic-core-vs-AI diff is only apples-to-apples from 2026-04-01 onward.",
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Wrote {OUT_PATH}", file=sys.stderr)
    print(f"final_return_pct={final_return_pct:.2f} n_trades={n_trades} max_dd={max_dd:.2f}", file=sys.stderr)


if __name__ == "__main__":
    main()
