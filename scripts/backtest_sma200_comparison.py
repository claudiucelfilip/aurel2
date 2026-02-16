#!/usr/bin/env python3
"""Compare baseline vs SMA-200 trend filter across multiple periods.

Usage: cd /root/aurel2 && python3 scripts/backtest_sma200_comparison.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from datetime import date, timedelta
from aurel2.engine.backtest import BacktestEngine
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.core.assets import get_all_yahoo_symbols


def run_comparison():
    print("=" * 100)
    print("SMA-200 TREND FILTER BACKTEST COMPARISON")
    print("=" * 100)

    # Fetch data
    end_date = date.today()
    start_date = end_date - timedelta(days=20 * 365 + 600)

    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    print(f"\nFetching {len(symbols)} symbols from {start_date} to {end_date}...")
    prices = provider.get_multi_prices(symbols, start_date, end_date + timedelta(days=5))
    if prices.empty:
        print("ERROR: No price data")
        return

    # Verify SPY data quality
    spy_data = prices[prices["symbol"] == "SPY"]
    print(f"Loaded {len(prices)} total rows. SPY: {len(spy_data)} rows ({spy_data['date'].min()} to {spy_data['date'].max()})")

    periods = [
        ("20y", end_date - timedelta(days=20 * 365), end_date),
        ("15y", end_date - timedelta(days=15 * 365), end_date),
        ("10y", end_date - timedelta(days=10 * 365), end_date),
        ("5y", end_date - timedelta(days=5 * 365), end_date),
        ("2009-2015", date(2009, 1, 1), date(2015, 12, 31)),
        ("2016-2022", date(2016, 1, 1), date(2022, 12, 31)),
        ("2019-2023", date(2019, 1, 1), date(2023, 12, 31)),
    ]

    results = []
    for label, p_start, p_end in periods:
        print(f"\n{'='*100}")
        print(f"Period: {label} ({p_start} to {p_end})")
        print(f"{'='*100}")

        # Baseline
        print("  [1/2] Baseline (no filter)...")
        baseline = BacktestEngine(initial_capital=10000.0, trend_filter_enabled=False)
        b_result = baseline.run(prices=prices, start_date=p_start, end_date=p_end, benchmark_symbol="SPY")
        b_result.calculate_metrics()

        # With SMA-200
        print("  [2/2] With SMA-200 trend filter...")
        filtered = BacktestEngine(
            initial_capital=10000.0,
            trend_filter_enabled=True,
            trend_filter_symbol="SPY",
            trend_filter_period=200,
            trend_filter_safe_asset="AGG",
        )
        f_result = filtered.run(prices=prices, start_date=p_start, end_date=p_end, benchmark_symbol="SPY")
        f_result.calculate_metrics()

        # Compare
        better_return = f_result.total_return >= b_result.total_return
        better_cagr = f_result.cagr >= b_result.cagr
        better_sharpe = f_result.sharpe_ratio >= b_result.sharpe_ratio
        better_dd = f_result.max_drawdown <= b_result.max_drawdown

        status = "✓" if better_return else "✗"
        status += " ✓" if better_cagr else " ✗"
        status += " ✓" if better_sharpe else " ✗"
        status += " ✓" if better_dd else " ✗"

        results.append((label, b_result, f_result, status))

        print(f"\n  {'Metric':<20} {'Baseline':>12} {'SMA-200':>12} {'Better?':>10}")
        print(f"  {'-'*54}")
        print(f"  {'Total Return':<20} {b_result.total_return:>11.2%} {f_result.total_return:>11.2%} {'✓' if better_return else '✗':>10}")
        print(f"  {'CAGR':<20} {b_result.cagr:>11.2%} {f_result.cagr:>11.2%} {'✓' if better_cagr else '✗':>10}")
        print(f"  {'Sharpe':<20} {b_result.sharpe_ratio:>11.2f} {f_result.sharpe_ratio:>11.2f} {'✓' if better_sharpe else '✗':>10}")
        print(f"  {'Max Drawdown':<20} {b_result.max_drawdown:>11.2%} {f_result.max_drawdown:>11.2%} {'✓' if better_dd else '✗':>10}")
        print(f"  {'Trades':<20} {b_result.num_trades:>11} {f_result.num_trades:>11}")

    # Summary
    print(f"\n\n{'='*100}")
    print("SUMMARY")
    print(f"{'='*100}")
    print(f"  {'Period':<15} | {'Baseline CAGR':>13} {'SR':>6} {'DD':>8} | {'Filter CAGR':>11} {'SR':>6} {'DD':>8} | Result")
    print(f"  {'-'*90}")
    all_pass = True
    for label, b, f, status in results:
        print(f"  {label:<15} | {b.cagr:>12.2%} {b.sharpe_ratio:>6.2f} {b.max_drawdown:>7.2%} | {f.cagr:>10.2%} {f.sharpe_ratio:>6.2f} {f.max_drawdown:>7.2%} | {status}")
        if "✗" in status:
            all_pass = False

    print(f"\n{'='*100}")
    if all_pass:
        print("✓ SMA-200 TREND FILTER: PASSED — All periods equal or better. Safe to merge.")
    else:
        print("✗ SMA-200 TREND FILTER: NEEDS REVIEW — Some periods show worse performance.")
    print(f"{'='*100}")


if __name__ == "__main__":
    run_comparison()
