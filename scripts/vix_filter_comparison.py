#!/usr/bin/env python3
"""VIX Filter Comparison Backtest.

Compares baseline strategy vs VIX-filtered (force defensive when VIX > threshold)
using the integrated BacktestEngine implementation.

Usage: cd /root/aurel2 && python3 scripts/vix_filter_comparison.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import json
import logging
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import structlog

from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine

# Suppress verbose backtest logging
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)
warnings.filterwarnings("ignore", category=FutureWarning)

# ============================================================================
# PERIODS
# ============================================================================

PERIODS = [
    ("Pre-GFC Bull (2005-2007)",   date(2005, 6, 1),  date(2007, 10, 1)),
    ("GFC Crash (2007-2009)",      date(2007, 10, 1), date(2009, 3, 1)),
    ("QE Recovery (2009-2013)",    date(2009, 3, 1),  date(2013, 1, 1)),
    ("Steady Bull (2013-2018)",    date(2013, 1, 1),  date(2018, 1, 1)),
    ("Vol Shock 2018",             date(2018, 1, 1),  date(2019, 1, 1)),
    ("Late Bull (2019-2020)",      date(2019, 1, 1),  date(2020, 2, 1)),
    ("COVID Crash+Recovery",       date(2020, 2, 1),  date(2021, 1, 1)),
    ("Post-COVID Bull (2021)",     date(2021, 1, 1),  date(2022, 1, 1)),
    ("Rate Hike Bear (2022)",      date(2022, 1, 1),  date(2023, 1, 1)),
    ("AI Bull (2023-2026)",        date(2023, 1, 1),  date(2026, 3, 1)),
    ("In-Sample (2005-2020)",      date(2005, 6, 1),  date(2020, 1, 1)),
    ("Out-of-Sample (2020-2026)",  date(2020, 1, 1),  date(2026, 3, 1)),
    ("Full 20y (2005-2026)",       date(2005, 6, 1),  date(2026, 3, 1)),
]

# ============================================================================
# CONFIGS TO COMPARE
# ============================================================================

CONFIGS = {
    "Baseline": dict(),
    "Canary → IEF": dict(canary_enabled=True, canary_safe_asset="IEF"),
    "Canary → SHY": dict(canary_enabled=True, canary_safe_asset="SHY"),
    "Canary → AGG": dict(canary_enabled=True, canary_safe_asset="AGG"),
    "Canary (SPY only)": dict(canary_enabled=True, canary_symbols=["SPY"], canary_safe_asset="IEF"),
    "SMA-200 filter": dict(trend_filter_enabled=True),
    "Canary + SMA-200": dict(canary_enabled=True, canary_safe_asset="IEF", trend_filter_enabled=True),
}


def run_comparison(prices: pd.DataFrame) -> list[dict]:
    """Run all configs across all periods."""
    results = []
    total = len(CONFIGS) * len(PERIODS)
    done = 0

    for period_name, start, end in PERIODS:
        for config_name, params in CONFIGS.items():
            done += 1
            print(f"\r  [{done}/{total}] {config_name} on {period_name}...", end="", flush=True)

            engine = BacktestEngine(initial_capital=10000.0, **params)
            try:
                result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
                result.calculate_metrics()

                years = (end - start).days / 365.25
                bench_cagr = (result.benchmark_final / 10000.0) ** (1 / years) - 1 if result.benchmark_final else 0

                results.append({
                    "period": period_name,
                    "config": config_name,
                    "cagr": result.cagr,
                    "sharpe": result.sharpe_ratio,
                    "sortino": result.sortino_ratio,
                    "max_dd": result.max_drawdown,
                    "calmar": result.calmar_ratio,
                    "trades": result.num_trades,
                    "final_value": result.final_value,
                    "spy_cagr": bench_cagr,
                })
            except Exception as e:
                print(f" ERROR: {e}")
                results.append({
                    "period": period_name,
                    "config": config_name,
                    "cagr": 0, "sharpe": 0, "sortino": 0, "max_dd": 0,
                    "calmar": 0, "trades": 0, "final_value": 0, "spy_cagr": 0,
                    "error": str(e),
                })

    print()  # newline after progress
    return results


def print_results(results: list[dict]):
    """Print comparison tables."""
    config_names = list(CONFIGS.keys())

    # Summary periods first
    summary_periods = [p for p in PERIODS if any(k in p[0] for k in ["In-Sample", "Out-of-Sample", "Full"])]
    detail_periods = [p for p in PERIODS if p not in summary_periods]

    print("\n" + "=" * 100)
    print("SUMMARY PERIODS")
    print("=" * 100)
    _print_period_table(results, [p[0] for p in summary_periods], config_names)

    print("\n" + "=" * 100)
    print("REGIME DETAIL")
    print("=" * 100)
    _print_period_table(results, [p[0] for p in detail_periods], config_names)

    # Delta table: Canary → IEF vs Baseline
    print("\n" + "=" * 100)
    print("CANARY → IEF vs BASELINE — DELTA BY PERIOD")
    print("=" * 100)
    print(f"  {'Period':<30s} {'CAGR Δ':>8s} {'Sharpe Δ':>9s} {'MaxDD Δ':>8s} {'Trades Δ':>9s}")
    print(f"  {'-'*66}")

    for period_name, _, _ in PERIODS:
        baseline = next((r for r in results if r["period"] == period_name and r["config"] == "Baseline"), None)
        canary = next((r for r in results if r["period"] == period_name and r["config"] == "Canary → IEF"), None)
        if baseline and canary:
            cagr_d = canary["cagr"] - baseline["cagr"]
            sharpe_d = canary["sharpe"] - baseline["sharpe"]
            dd_d = canary["max_dd"] - baseline["max_dd"]
            trades_d = canary["trades"] - baseline["trades"]
            marker = " <<<" if cagr_d > 0.005 and sharpe_d > 0 else ""
            print(f"  {period_name:<30s} {cagr_d:>+7.2%} {sharpe_d:>+8.4f} {dd_d:>+7.2%} {trades_d:>+8d}{marker}")


def _print_period_table(results: list[dict], period_names: list[str], config_names: list[str]):
    """Print a comparison table for given periods."""
    for period_name in period_names:
        print(f"\n  --- {period_name} ---")
        print(f"  {'Config':<22s} {'CAGR':>7s} {'Sharpe':>7s} {'Sortino':>8s} {'MaxDD':>7s} {'Calmar':>7s} {'Trades':>7s}")
        print(f"  {'-'*66}")

        period_results = [r for r in results if r["period"] == period_name]
        # Show SPY first
        spy_cagr = next((r["spy_cagr"] for r in period_results if r["spy_cagr"]), 0)
        if spy_cagr:
            print(f"  {'SPY Buy & Hold':<22s} {spy_cagr:>+6.2%}")

        for config in config_names:
            r = next((r for r in period_results if r["config"] == config), None)
            if r and "error" not in r:
                print(f"  {config:<22s} {r['cagr']:>+6.2%} {r['sharpe']:>7.2f} {r['sortino']:>8.2f} {r['max_dd']:>6.2%} {r['calmar']:>7.2f} {r['trades']:>7d}")
            elif r:
                print(f"  {config:<22s}  ERROR: {r['error'][:40]}")


def main():
    print("=" * 100)
    print("VIX FILTER COMPARISON BACKTEST")
    print("=" * 100)

    # Fetch data
    print("\nFetching price data...")
    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    for s in ["SPY", "^VIX"]:
        if s not in symbols:
            symbols.append(s)

    fetch_start = date(2005, 6, 1) - timedelta(days=500)
    fetch_end = date(2026, 4, 1)
    prices = provider.get_multi_prices(symbols, fetch_start, fetch_end)
    print(f"  Loaded {len(prices)} rows, {len(prices[prices['symbol'] == '^VIX'])} VIX days")

    # Run comparison
    print("\nRunning backtests...")
    results = run_comparison(prices)

    # Print results
    print_results(results)

    # Save
    output_path = os.path.join(os.path.dirname(__file__), "..", "data", "vix_filter_comparison.json")
    with open(output_path, "w") as f:
        json.dump([{k: round(v, 6) if isinstance(v, float) else v for k, v in r.items()} for r in results], f, indent=2)
    print(f"\nResults saved to {output_path}")

    print("\n" + "=" * 100)
    print("DONE")
    print("=" * 100)


if __name__ == "__main__":
    main()
