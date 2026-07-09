#!/usr/bin/env python3
"""Historical-regime extension of lt_core_replay.py: replays LT-core (live-trader's
deterministic daily_runner.py core, AI/discretionary tilt forced to zero) over the
same named regime windows used by scripts/backtest_lookback.py (12-month lookback
config), so LT-core's edge can be compared against Aurel2's dual-momentum results
in data/lookback_and_vote_2026_07_07.json.

Imports the actual feature/scoring/selection functions from live-trader verbatim
(via exec of the source), exactly like lt_core_replay.py — see that file's header
for why exec-of-source instead of `import daily_runner` is used.

Each window starts fresh with $10,000 cash (not chained/carried across windows).
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

LIVE_TRADER_DIR = "/Users/claudiu/.openclaw/workspace/live-trader"
sys.path.insert(0, LIVE_TRADER_DIR)

# Import the real deterministic engine verbatim — no reimplementation. See
# lt_core_replay.py's header comment for why we exec a slice of the source
# instead of `import daily_runner` (avoids broker/news-ingest imports at
# module top that require credentials we don't have/want here).
_dr_source = Path(LIVE_TRADER_DIR, "daily_runner.py").read_text()
_dr_globals: dict = {
    "__name__": "daily_runner_core", "__file__": str(Path(LIVE_TRADER_DIR, "daily_runner.py")),
    "Path": Path, "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
}
_core_start = _dr_source.index("WATCHLIST = ")
_core_end = _dr_source.index("def main():")
exec(compile(_dr_source[_core_start:_core_end], "daily_runner_core", "exec"), _dr_globals)

WATCHLIST = _dr_globals["WATCHLIST"]
build_features = _dr_globals["build_features"]
infer_regime = _dr_globals["infer_regime"]
choose_target = _dr_globals["choose_target"]

HARD_STOP_VALUE = _dr_globals["HARD_STOP_VALUE"]
SOFT_RISK_OFF_VALUE = _dr_globals["SOFT_RISK_OFF_VALUE"]
ROTATION_MIN_DAYS = _dr_globals["ROTATION_MIN_DAYS"]
STRONG_EDGE_THRESHOLD = _dr_globals["STRONG_EDGE_THRESHOLD"]

NO_TILT = {"symbol_bias": {}, "regime_bias": {}, "notes": ""}
INITIAL_CAPITAL = 10_000.0
WARMUP_TRADING_DAYS = 220  # generous vs. build_features' 80-bar minimum requirement

# Regime windows: dates copied verbatim from scripts/backtest_lookback.py's PERIODS
# list (the script that produced data/lookback_and_vote_2026_07_07.json's exp1_lookback_sweep
# rows for these four names). "Last 12m" has no discoverable generating script in
# scripts/ (grep for the literal string found nothing) — inferred as the 12 months
# ending on the lookback_and_vote data's generation date, per instructions.
WINDOWS: list[tuple[str, date, date]] = [
    ("AI Bull 23-26", date(2023, 1, 1), date(2026, 2, 1)),
    ("Rate Hike Bear", date(2022, 1, 1), date(2022, 12, 31)),
    ("COVID V-shape", date(2020, 3, 23), date(2022, 1, 1)),
    ("Full 15y", date(2010, 1, 1), date(2026, 2, 1)),
    ("Last 12m", date(2025, 7, 7), date(2026, 7, 7)),
]

OUT_PATH = Path("/Users/claudiu/Sites/aurel2/data/edge_decomposition/lt_core_history.json")


@dataclass
class Bar:
    close: float


def fetch_all_bars(fetch_start: date, fetch_end: date) -> dict[str, list[tuple[date, float]]]:
    """Daily auto-adjusted closes per symbol, as (date, close) pairs, for the full
    span needed across every window (fetched once and sliced per-window below)."""
    raw = yf.download(
        WATCHLIST, start=fetch_start.isoformat(), end=(fetch_end + timedelta(days=3)).isoformat(),
        auto_adjust=True, progress=False, group_by="ticker",
    )
    out: dict[str, list[tuple[date, float]]] = {}
    for sym in WATCHLIST:
        closes = raw[sym]["Close"].dropna()
        out[sym] = [(d.date(), float(v)) for d, v in closes.items()]
    return out


def bars_as_of(all_bars: dict[str, list[tuple[date, float]]], as_of: date) -> dict[str, list[Bar]]:
    out = {}
    for sym, series in all_bars.items():
        closes = [c for d, c in series if d <= as_of]
        if closes:
            out[sym] = [Bar(close=c) for c in closes]
    return out


def trading_days_in(all_bars: dict[str, list[tuple[date, float]]], start: date, end: date) -> list[date]:
    days = sorted({d for series in all_bars.values() for d, _ in series})
    return [d for d in days if start <= d <= end]


def first_available_date(all_bars: dict[str, list[tuple[date, float]]], sym: str) -> date | None:
    series = all_bars.get(sym)
    return series[0][0] if series else None


def annualized_sharpe(equity: list[float]) -> float:
    """Daily returns, rf=0, annualized with sqrt(252) — matches BacktestEngine's
    convention in src/aurel2/engine/backtest.py (periods_per_year derived from
    snapshot density; for our daily snapshots that's ~252)."""
    if len(equity) < 2:
        return 0.0
    values = pd.Series(equity)
    returns = values.pct_change().dropna()
    if len(returns) == 0 or returns.std() == 0:
        return 0.0
    periods_per_year = 252.0
    return float((returns.mean() * periods_per_year) / (returns.std() * math.sqrt(periods_per_year)))


def simulate_window(all_bars: dict[str, list[tuple[date, float]]], start: date, end: date) -> dict:
    days = trading_days_in(all_bars, start, end)

    cash = INITIAL_CAPITAL
    shares = 0.0
    holding: str | None = None
    last_rotation_day: date | None = None
    switches = 0

    daily_equity: list[dict] = []

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
            daily_equity.append({"date": d.isoformat(), "equity": equity, "holding": holding})
            continue

        regime = infer_regime(features)

        # --- Hard stop: liquidate to cash ---
        if equity < HARD_STOP_VALUE / 100.0 * INITIAL_CAPITAL and holding:
            cash = equity
            holding = None
            shares = 0.0
            daily_equity.append({"date": d.isoformat(), "equity": cash, "holding": None})
            continue

        target_symbol, target_row, ranked = choose_target(features, regime, NO_TILT)

        if not holding:
            soft_risk_off_dollars = SOFT_RISK_OFF_VALUE / 100.0 * INITIAL_CAPITAL
            if equity <= soft_risk_off_dollars and regime["name"] == "risk_off" and target_row["score"] < 1.0:
                daily_equity.append({"date": d.isoformat(), "equity": cash, "holding": None})
                continue

            notional = cash * 0.99
            if notional > 1:
                px_buy = price_on(target_symbol, d)
                shares = notional / px_buy
                cash -= notional
                holding = target_symbol
                last_rotation_day = d
                switches += 1
            equity = cash + shares * price_on(holding, d) if holding else cash
            daily_equity.append({"date": d.isoformat(), "equity": equity, "holding": holding})
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
            shares = 0.0
            holding = None
            switches += 1

            notional = cash * 0.99
            if notional > 1:
                px_buy = price_on(target_symbol, d)
                shares = notional / px_buy
                cash -= notional
                holding = target_symbol
                last_rotation_day = d
                switches += 1

        px_eod = price_on(holding, d) if holding else None
        equity = cash + (shares * px_eod if (holding and px_eod is not None) else 0.0)
        daily_equity.append({"date": d.isoformat(), "equity": equity, "holding": holding})

    if not daily_equity:
        return {"error": "no trading days in window"}

    equity_series = [r["equity"] for r in daily_equity]
    final_value = equity_series[-1]
    total_return = (final_value / INITIAL_CAPITAL) - 1.0

    years = (days[-1] - days[0]).days / 365.25
    cagr = (final_value / INITIAL_CAPITAL) ** (1 / years) - 1.0 if years > 0 else 0.0

    peak = equity_series[0]
    max_dd = 0.0
    for v in equity_series:
        peak = max(peak, v)
        dd = (peak - v) / peak
        max_dd = max(max_dd, dd)

    sharpe = annualized_sharpe(equity_series)

    spy_start = price_on("SPY", days[0])
    spy_end = price_on("SPY", days[-1])
    spy_cagr = (spy_end / spy_start) ** (1 / years) - 1.0 if years > 0 and spy_start else 0.0

    return {
        "actual_start": days[0].isoformat(),
        "actual_end": days[-1].isoformat(),
        "cagr": round(cagr, 6),
        "total_return": round(total_return, 6),
        "max_drawdown": round(max_dd, 6),
        "sharpe": round(sharpe, 6),
        "switches": switches,
        "final_value": round(final_value, 2),
        "alpha_vs_spy": round(cagr - spy_cagr, 6),
        "spy_cagr": round(spy_cagr, 6),
    }


def main():
    notes = [
        "LT-core (daily_runner.py deterministic core, symbol_bias/regime_bias forced to {}) "
        "replayed over the same named regime windows used by scripts/backtest_lookback.py's "
        "12-month-lookback config, for comparison against Aurel2 dual-momentum results in "
        "data/lookback_and_vote_2026_07_07.json (exp1_lookback_sweep).",
        "Window dates for 'AI Bull 23-26' (as 'AI Bull'), 'Rate Hike Bear', 'COVID V-shape', and "
        "'Full 15y' copied verbatim from scripts/backtest_lookback.py PERIODS (2023-01-01/2026-02-01, "
        "2022-01-01/2022-12-31, 2020-03-23/2022-01-01, 2010-01-01/2026-02-01).",
        "'Last 12m' has no discoverable generating script (grepped scripts/ for the literal string "
        "'Last 12m' with no hits) — inferred as 2025-07-07 to 2026-07-07, the 12 months ending on "
        "lookback_and_vote_2026_07_07.json's generation date. This is an assumption, not a verified match.",
        "Each window starts independently with $10,000 cash (not chained across windows); first BUY "
        "deploys 99% of cash, matching daily_runner.py's buying_power * 0.99 notional logic.",
        "CAGR: (final_value/initial_capital)^(1/years) - 1, years = (actual_last_day - actual_first_day).days / 365.25. "
        "Sharpe: daily returns, rf=0, annualized with sqrt(252). Both match the convention in "
        "src/aurel2/engine/backtest.py's BacktestEngine.calculate_metrics() (used by backtest_lookback.py) "
        "for daily-cadence snapshots.",
        "'switches' counts every BUY/SELL fill (a rotation = 1 SELL + 1 BUY = 2), matching "
        "lt_core_replay.py's n_trades convention — not directly comparable to exp1_lookback_sweep's "
        "'switches' field without checking that sweep's own counting convention, which wasn't in scope here.",
        "Trades execute at the decision day's close (same convention as lt_core_replay.py): "
        "features/regime/score for day D use bars through D's close, and any BUY/SELL fills at D's close.",
        "Data: yfinance daily auto-adjusted closes. All 8 WATCHLIST tickers (SPY, QQQ, GLD, TLT, IWM, "
        "EFA, EEM, XLE) have data back to at least 2009-01-02, so 'Full 15y' (starting 2010-01-01) runs "
        "with the complete watchlist — no ticker was dropped for any window.",
        f"Each window's data fetch starts {WARMUP_TRADING_DAYS} trading days before the window start to "
        "warm build_features()'s 80-bar minimum lookback (150-day figure in lt_core_replay.py's docstring "
        "is a conservative description of the same warmup; 220 trading days is comfortably generous either way).",
        "Functions (build_features, infer_regime, conviction_score, choose_target, WATCHLIST, RISK_ASSETS, "
        f"DEFENSIVE, threshold constants) imported verbatim from {LIVE_TRADER_DIR}/daily_runner.py via exec "
        "of the source, identically to lt_core_replay.py.",
    ]

    results = {}
    for label, start, end in WINDOWS:
        # Fetch with a warmup buffer computed in calendar days (generous: trading
        # days are ~5/7 of calendar days, so 220 trading days needs ~310 calendar days).
        fetch_start = start - timedelta(days=int(WARMUP_TRADING_DAYS * 7 / 5) + 10)
        print(f"[{label}] fetching {fetch_start} .. {end} ...", file=sys.stderr)
        all_bars = fetch_all_bars(fetch_start, end)

        missing = [sym for sym in WATCHLIST if (first_available_date(all_bars, sym) or date.max) > start]
        if missing:
            notes.append(f"[{label}] tickers with data starting after window start: {missing} (see actual_start/actual_end in result).")

        result = simulate_window(all_bars, start, end)
        result["start"] = start.isoformat()
        result["end"] = end.isoformat()
        results[label] = result
        print(
            f"[{label}] cagr={result.get('cagr', float('nan')):.4f} "
            f"total_return={result.get('total_return', float('nan')):.4f} "
            f"switches={result.get('switches')}",
            file=sys.stderr,
        )

    output = {"arm": "lt_core_history", "windows": results, "notes": notes}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Wrote {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
