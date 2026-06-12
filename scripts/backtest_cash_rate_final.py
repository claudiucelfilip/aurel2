#!/usr/bin/env python3
"""Final cash-rate isolation: threshold fixed at 0.02, vary cash_rate only."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.stdlib.BoundLogger, logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")))
logging.disable(logging.CRITICAL)

from rich.console import Console

from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy


def run_config(name, prices, start, end, dm_kwargs=None, engine_kwargs=None):
    dm_kwargs = dm_kwargs or {}
    engine_kwargs = engine_kwargs or {}
    engine = BacktestEngine(initial_capital=10000, use_ai=False, **engine_kwargs)
    if dm_kwargs:
        engine.dual_momentum = DualMomentumStrategy(**dm_kwargs)
    result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")

    years = (end - start).days / 365.25
    bench_return = ((result.benchmark_final / 10000) - 1) if result.benchmark_final else 0
    bench_cagr = ((1 + bench_return) ** (1 / years) - 1) if years > 0 else 0
    strat_total = (result.final_value / 10000 - 1) * 100
    bench_total = bench_return * 100
    return {
        "name": name,
        "cagr": result.cagr,
        "bench_cagr": bench_cagr,
        "alpha_cagr": result.cagr - bench_cagr,
        "strat_total": strat_total,
        "bench_total": bench_total,
        "alpha_total": strat_total - bench_total,
        "max_dd": result.max_drawdown,
        "sharpe": result.sharpe_ratio,
        "trades": result.num_trades,
        "final": result.final_value,
    }


def main():
    console = Console()
    console.print("\n[bold cyan]CASH RATE ISOLATION: thr=0.02 fixed, vary cash_rate[/bold cyan]")
    console.print("=" * 120)

    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    console.print(f"\nFetching {len(symbols)} symbols...")
    prices = provider.get_multi_prices(symbols, date(2003, 6, 1), date.today() + timedelta(days=5))
    console.print(f"Total: {len(prices)} price records\n")

    if prices.empty:
        console.print("[red]ERROR: No price data[/red]")
        return

    no_guard = {"correlation_guard": False, "sideways_hold": False}
    base = {"assets": ASSET_REGISTRY, "switch_threshold": 0.02, "pilot_entry_enabled": False}

    configs = [
        ("cash=0.00", {**base, "cash_rate": 0.00}, no_guard),
        ("cash=0.01", {**base, "cash_rate": 0.01}, no_guard),
        ("cash=0.02", {**base, "cash_rate": 0.02}, no_guard),
    ]

    periods = [
        ("20y", date(2005, 1, 1), date(2026, 2, 1)),
        ("15y", date(2010, 1, 1), date(2026, 2, 1)),
        ("10y", date(2015, 1, 1), date(2026, 2, 1)),
        ("5y",  date(2020, 1, 1), date(2026, 2, 1)),
        ("1y",  date(2025, 2, 1), date(2026, 2, 1)),
    ]

    # Also test sub-periods for regime analysis
    regimes = [
        ("2005-2010 GFC", date(2005, 1, 1), date(2010, 1, 1)),
        ("2010-2015 QE", date(2010, 1, 1), date(2015, 1, 1)),
        ("2015-2020 Bull", date(2015, 1, 1), date(2020, 1, 1)),
        ("2020-2026 COVID", date(2020, 1, 1), date(2026, 2, 1)),
    ]

    all_periods = periods + regimes

    console.print(f"[bold yellow]FULL PERIOD RESULTS[/bold yellow]\n")
    console.print(f"  {'Config':<12} {'Period':<18} {'CAGR':>7} {'SPY':>7} {'Alpha':>7} | {'StratTR':>8} {'SPYTR':>8} {'TR Alpha':>9} | {'MaxDD':>7} {'Sharpe':>7} {'Trd':>4}")
    console.print(f"  {'-'*110}")

    for period_label, start, end in all_periods:
        for cname, dm_kw, eng_kw in configs:
            try:
                r = run_config(cname, prices, start, end, dm_kw, eng_kw)
                alpha_color = "green" if r["alpha_total"] > 0 else "red"
                console.print(
                    f"  {cname:<12} {period_label:<18} "
                    f"{r['cagr']:+6.1%} {r['bench_cagr']:+6.1%} {r['alpha_cagr']:+6.1%} | "
                    f"{r['strat_total']:+7.0f}% {r['bench_total']:+7.0f}% [{alpha_color}]{r['alpha_total']:+8.0f}%[/{alpha_color}] | "
                    f"{r['max_dd']:6.1%} {r['sharpe']:7.2f} {r['trades']:4d}"
                )
            except Exception as e:
                console.print(f"  {cname:<12} {period_label:<18} ERROR: {e}")
        console.print()


if __name__ == "__main__":
    main()
