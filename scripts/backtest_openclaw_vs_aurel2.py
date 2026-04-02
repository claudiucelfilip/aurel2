#!/usr/bin/env python3
"""Backtest: OpenClaw's discretionary strategy vs Aurel2's dual momentum.

Replicates OpenClaw's daily_runner.py logic using historical Yahoo data,
then compares against Aurel2's BacktestEngine on the same periods.

Usage: cd /root/aurel2 && python3 scripts/backtest_openclaw_vs_aurel2.py
"""

import sys
import os
import warnings
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))

from datetime import date, timedelta
from io import StringIO

import numpy as np
import pandas as pd

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine

# ============================================================================
# OpenClaw strategy constants (from daily_runner.py)
# ============================================================================

WATCHLIST = ["SPY", "QQQ", "GLD", "TLT", "IWM", "EFA", "EEM", "XLE"]
RISK_ASSETS = {"SPY", "QQQ", "IWM", "EFA", "EEM", "XLE"}
DEFENSIVE = {"GLD", "TLT"}
STRONG_EDGE_THRESHOLD = 4.0
ROTATION_MIN_DAYS = 2


# ============================================================================
# OpenClaw strategy logic (replicated from daily_runner.py)
# ============================================================================

def pct_return(closes, n):
    if len(closes) < n + 1:
        return None
    return ((closes[-1] / closes[-(n + 1)]) - 1.0) * 100.0


def realized_vol(closes, n=20):
    if len(closes) < n + 1:
        return None
    rets = []
    for i in range(-n, 0):
        prev = closes[i - 1]
        cur = closes[i]
        if prev > 0:
            rets.append((cur / prev) - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * 100.0


def drawdown_lookback(closes, n=20):
    if len(closes) < n:
        return None
    window = closes[-n:]
    peak = max(window)
    if peak <= 0:
        return None
    return ((window[-1] / peak) - 1.0) * 100.0


def build_features(prices_df, symbols, calc_date):
    """Build OpenClaw features from Yahoo price data."""
    features = {}
    for sym in symbols:
        sym_data = prices_df[(prices_df["symbol"] == sym) & (prices_df["date"] <= calc_date)]
        if len(sym_data) < 80:
            continue
        closes = sym_data["close"].values[-80:].tolist()

        r5 = pct_return(closes, 5)
        r20 = pct_return(closes, 20)
        r60 = pct_return(closes, 60)
        vol20 = realized_vol(closes, 20)
        dd20 = drawdown_lookback(closes, 20)

        if None in {r5, r20, r60, vol20, dd20}:
            continue

        features[sym] = {
            "price": closes[-1],
            "ret_5d": round(r5, 4),
            "ret_20d": round(r20, 4),
            "ret_60d": round(r60, 4),
            "vol_20d": round(vol20, 4),
            "dd_20d": round(dd20, 4),
        }
    return features


def infer_regime(features):
    risk_positive = sum(1 for s in RISK_ASSETS if s in features and features[s]["ret_20d"] > 0)
    breadth = risk_positive / max(1, len([s for s in RISK_ASSETS if s in features]))

    spy20 = features.get("SPY", {}).get("ret_20d", 0.0)
    qqq20 = features.get("QQQ", {}).get("ret_20d", 0.0)
    gld20 = features.get("GLD", {}).get("ret_20d", 0.0)

    risk_on = breadth >= 0.55 and (spy20 > 0 or qqq20 > 0)
    risk_off = breadth <= 0.30 and gld20 > 0

    if risk_off:
        return {"name": "risk_off"}
    elif risk_on:
        return {"name": "risk_on"}
    else:
        return {"name": "mixed"}


def conviction_score(sym, f, regime):
    """Score without tilts (no AI/manual bias in backtest)."""
    trend = 0.45 * f["ret_20d"] + 0.20 * f["ret_60d"] + 0.15 * f["ret_5d"]
    risk_penalty = 0.25 * max(0.0, f["vol_20d"]) + 0.25 * abs(min(0.0, f["dd_20d"]))

    regime_adj = 0.0
    if regime["name"] == "risk_off":
        regime_adj += 1.5 if sym in DEFENSIVE else -2.0
    elif regime["name"] == "risk_on":
        regime_adj += 1.0 if sym in RISK_ASSETS else -0.5

    return round(trend - risk_penalty + regime_adj, 4)


def choose_target(features, regime):
    scored = []
    for sym, f in features.items():
        score = conviction_score(sym, f, regime)
        scored.append({"symbol": sym, "score": score, "price": f["price"]})

    scored.sort(key=lambda x: x["score"], reverse=True)

    if regime["name"] == "risk_off":
        defensive = [r for r in scored if r["symbol"] in DEFENSIVE]
        if defensive:
            return defensive[0]

    return scored[0] if scored else None


# ============================================================================
# OpenClaw backtest engine
# ============================================================================

def backtest_openclaw(prices_df, start_date, end_date, initial_capital=10000.0, rebalance="monthly"):
    """Backtest OpenClaw's strategy.

    Args:
        rebalance: "daily" for true replication, "monthly" for fair comparison with aurel2
    """
    # Ensure date column is comparable
    prices_df = prices_df.copy()
    prices_df["date"] = pd.to_datetime(prices_df["date"]).dt.date

    # Get business days in range
    spy_dates = prices_df[
        (prices_df["symbol"] == "SPY") &
        (prices_df["date"] >= start_date) &
        (prices_df["date"] <= end_date)
    ]["date"].sort_values().unique()

    if rebalance == "monthly":
        # Use month-end dates only
        date_series = pd.Series(pd.to_datetime(spy_dates))
        check_dates = date_series.groupby(date_series.dt.to_period("M")).last().values
        check_dates = [pd.Timestamp(d).date() for d in check_dates]
    else:
        check_dates = [pd.Timestamp(d).date() for d in spy_dates]

    capital = initial_capital
    current_symbol = None
    current_shares = 0.0
    last_rotation_date = None
    num_trades = 0
    snapshots = []
    transaction_cost = 0.001  # 0.1% per trade, same as aurel2

    for check_date in check_dates:
        features = build_features(prices_df, WATCHLIST, check_date)
        if not features:
            continue

        # Portfolio value
        if current_symbol and current_symbol in features:
            portfolio_value = current_shares * features[current_symbol]["price"]
        elif current_symbol:
            portfolio_value = capital  # can't price, assume unchanged
        else:
            portfolio_value = capital

        snapshots.append({"date": check_date, "value": portfolio_value})

        regime = infer_regime(features)
        target = choose_target(features, regime)
        if not target:
            continue

        target_sym = target["symbol"]

        # No position: buy
        if not current_symbol:
            price = target["price"]
            cost = portfolio_value * transaction_cost
            current_shares = (portfolio_value - cost) / price
            current_symbol = target_sym
            capital = 0
            num_trades += 1
            last_rotation_date = check_date
            continue

        # Check rotation
        current_row = next(
            ({"symbol": current_symbol, "score": conviction_score(current_symbol, features[current_symbol], regime), "price": features[current_symbol]["price"]}
             for _ in [1] if current_symbol in features),
            {"symbol": current_symbol, "score": -999.0, "price": 0},
        )

        score_gap = target["score"] - current_row["score"]

        cooldown_block = False
        if last_rotation_date:
            days_since = (check_date - last_rotation_date).days
            cooldown_block = days_since < ROTATION_MIN_DAYS

        should_rotate = (
            target_sym != current_symbol
            and score_gap >= STRONG_EDGE_THRESHOLD
            and not cooldown_block
        )

        if should_rotate:
            # Sell
            sell_price = features[current_symbol]["price"] if current_symbol in features else current_row["price"]
            proceeds = current_shares * sell_price
            proceeds -= proceeds * transaction_cost

            # Buy
            buy_price = target["price"]
            current_shares = proceeds / buy_price
            current_symbol = target_sym
            capital = 0
            num_trades += 1
            last_rotation_date = check_date

    # Final value
    if current_symbol:
        # Get final price
        final_data = prices_df[
            (prices_df["symbol"] == current_symbol) &
            (prices_df["date"] <= end_date)
        ]
        if not final_data.empty:
            final_price = final_data.iloc[-1]["close"]
            final_value = current_shares * final_price
        else:
            final_value = snapshots[-1]["value"] if snapshots else initial_capital
    else:
        final_value = capital or initial_capital

    years = (end_date - start_date).days / 365.25
    cagr = (final_value / initial_capital) ** (1 / years) - 1 if years > 0 else 0

    # Max drawdown
    max_dd = 0.0
    peak = 0.0
    for s in snapshots:
        v = s["value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe
    sharpe = 0.0
    sortino = 0.0
    if len(snapshots) > 1:
        values = [s["value"] for s in snapshots]
        returns = pd.Series(values).pct_change().dropna()
        if len(returns) > 0 and returns.std() > 0:
            periods = 12 if rebalance == "monthly" else 252
            sharpe = (returns.mean() * periods) / (returns.std() * np.sqrt(periods))
            downside = returns[returns < 0]
            if len(downside) > 0 and downside.std() > 0:
                sortino = (returns.mean() * periods) / (downside.std() * np.sqrt(periods))

    turnover = num_trades / years if years > 0 else 0

    return {
        "cagr": cagr,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "turnover": turnover,
        "trades": num_trades,
        "final_value": final_value,
    }


# ============================================================================
# Main comparison
# ============================================================================

def main():
    print("=" * 95)
    print("OPENCLAW vs AUREL2 BACKTEST COMPARISON")
    print("=" * 95)

    # Fetch data
    print("\nFetching prices...")
    provider = YahooFinanceProvider()
    # Need QQQ and IWM for OpenClaw (not in aurel2 registry)
    aurel2_symbols = get_all_yahoo_symbols()
    all_symbols = list(set(aurel2_symbols + WATCHLIST))

    old = sys.stdout
    sys.stdout = StringIO()
    prices = provider.get_multi_prices(all_symbols, date(2004, 1, 1), date(2026, 4, 1))
    sys.stdout = old
    print(f"Loaded {len(prices)} rows across {len(all_symbols)} symbols")

    PERIODS = [
        ("Post-GFC 15y",    date(2010, 1, 1),  date(2026, 3, 1)),
        ("Steady Bull",     date(2013, 1, 1),  date(2018, 1, 1)),
        ("Vol Shock 2018",  date(2018, 1, 1),  date(2019, 1, 1)),
        ("COVID Crash",     date(2020, 2, 1),  date(2020, 6, 1)),
        ("COVID Recovery",  date(2020, 6, 1),  date(2022, 1, 1)),
        ("Rate Hike Bear",  date(2022, 1, 1),  date(2023, 1, 1)),
        ("AI Bull",         date(2023, 1, 1),  date(2026, 3, 1)),
        ("OOS 2020-2026",   date(2020, 1, 1),  date(2026, 3, 1)),
        ("Full 20y",        date(2005, 6, 1),  date(2026, 3, 1)),
    ]

    # Header
    print(f"\n{'':20s} |  OpenClaw (monthly)     |  Aurel2 (12mo DM)       |")
    print(f"{'Period':20s} | {'CAGR':>6s} {'Shrp':>5s} {'MaxDD':>6s} {'Trn':>4s} | {'CAGR':>6s} {'Shrp':>5s} {'MaxDD':>6s} {'Trn':>4s} | {'SPY':>6s}")
    print("-" * 90)

    for pname, start, end in PERIODS:
        # OpenClaw backtest (monthly rebalance for fair comparison)
        oc = backtest_openclaw(prices, start, end, rebalance="monthly")

        # Aurel2 backtest
        engine = BacktestEngine(initial_capital=10000.0)
        sys.stdout = StringIO()
        a2 = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
        sys.stdout = old
        a2.calculate_metrics()

        spy_cagr = ""
        if a2.benchmark_final:
            years = (end - start).days / 365.25
            sc = (a2.benchmark_final / 10000.0) ** (1 / years) - 1 if years > 0 else 0
            spy_cagr = f"{sc:>+5.1%}"

        print(
            f"{pname:20s}"
            f" | {oc['cagr']:>+5.1%} {oc['sharpe']:>5.2f} {oc['max_dd']:>5.1%} {oc['turnover']:>4.1f}"
            f" | {a2.cagr:>+5.1%} {a2.sharpe_ratio:>5.2f} {a2.max_drawdown:>5.1%} {a2.turnover:>4.1f}"
            f" | {spy_cagr}"
        )

    # Also run OpenClaw with daily rebalance for reference
    print(f"\n{'':20s} |  OpenClaw (DAILY)       |")
    print(f"{'Period':20s} | {'CAGR':>6s} {'Shrp':>5s} {'MaxDD':>6s} {'Trn':>4s} |")
    print("-" * 50)
    for pname, start, end in PERIODS:
        oc = backtest_openclaw(prices, start, end, rebalance="daily")
        print(f"{pname:20s} | {oc['cagr']:>+5.1%} {oc['sharpe']:>5.2f} {oc['max_dd']:>5.1%} {oc['turnover']:>4.1f} |")


if __name__ == "__main__":
    main()
