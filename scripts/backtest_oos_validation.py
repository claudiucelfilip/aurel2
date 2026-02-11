#!/usr/bin/env python3
"""Out-of-sample validation: optimize on 2005-2015, test on 2015-2026.

If the parameters chosen on the training period also work on the test period,
we're NOT overfitting.

Also tests robustness by shifting start dates (Jan, Apr, Jul, Oct).
"""

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


def print_results(console, label, results):
    console.print(f"\n  {'Config':<28} {'CAGR':>7} {'SPY':>7} {'Alpha':>7} | {'StratTR':>8} {'SPYTR':>8} {'TR Alpha':>9} | {'MaxDD':>7} {'Sharpe':>7} {'Trd':>4}")
    console.print(f"  {'-'*110}")
    for r in results:
        if "error" in r:
            console.print(f"  {r['name']:<28} ERROR: {r['error'][:50]}")
            continue
        alpha_color = "green" if r["alpha_total"] > 0 else "red"
        console.print(
            f"  {r['name']:<28} "
            f"{r['cagr']:+6.1%} {r['bench_cagr']:+6.1%} {r['alpha_cagr']:+6.1%} | "
            f"{r['strat_total']:+7.0f}% {r['bench_total']:+7.0f}% [{alpha_color}]{r['alpha_total']:+8.0f}%[/{alpha_color}] | "
            f"{r['max_dd']:6.1%} {r['sharpe']:7.2f} {r['trades']:4d}"
        )


def main():
    console = Console()
    console.print("\n[bold cyan]OUT-OF-SAMPLE VALIDATION + ROBUSTNESS TESTS[/bold cyan]")
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

    base_current = {"assets": ASSET_REGISTRY, "switch_threshold": 0.10, "cash_rate": 0.04, "pilot_entry_enabled": True}
    base_proposed = {"assets": ASSET_REGISTRY, "switch_threshold": 0.02, "cash_rate": 0.0, "pilot_entry_enabled": False}
    eng_current = {"correlation_guard": True, "sideways_hold": True}
    eng_proposed = {"correlation_guard": False, "sideways_hold": False}

    configs = [
        ("CURRENT", base_current, eng_current),
        ("PROPOSED (0% cash, 2% thr)", base_proposed, eng_proposed),
    ]

    # ================================================================
    # TEST 1: Out-of-sample split
    # ================================================================
    console.print("[bold yellow]TEST 1: OUT-OF-SAMPLE SPLIT[/bold yellow]")
    console.print("If proposed params found on 2005-2015 still work on 2015-2026, it's not overfitting.\n")

    splits = [
        ("TRAIN: 2005-2015", date(2005, 1, 1), date(2015, 1, 1)),
        ("TEST:  2015-2026", date(2015, 1, 1), date(2026, 2, 1)),
    ]

    for label, start, end in splits:
        console.print(f"[bold]{label} ({start} → {end})[/bold]")
        results = []
        for name, dm_kw, eng_kw in configs:
            console.print(f"  [dim]Running {name}...[/dim]")
            try:
                results.append(run_config(name, prices, start, end, dm_kw, eng_kw))
            except Exception as e:
                results.append({"name": name, "error": str(e)})
        print_results(console, label, results)

    # ================================================================
    # TEST 2: Shifted start dates (robustness)
    # ================================================================
    console.print(f"\n\n[bold yellow]TEST 2: SHIFTED START DATES (20y periods)[/bold yellow]")
    console.print("Different start months to check if results are sensitive to timing.\n")

    shifts = [
        ("Start Jan 2005", date(2005, 1, 1)),
        ("Start Apr 2005", date(2005, 4, 1)),
        ("Start Jul 2005", date(2005, 7, 1)),
        ("Start Oct 2005", date(2005, 10, 1)),
        ("Start Jan 2006", date(2006, 1, 1)),
        ("Start Jul 2006", date(2006, 7, 1)),
    ]

    for label, start in shifts:
        end = date(2026, 2, 1)
        console.print(f"[bold]{label} → {end}[/bold]")
        results = []
        for name, dm_kw, eng_kw in configs:
            console.print(f"  [dim]Running {name}...[/dim]")
            try:
                results.append(run_config(name, prices, start, end, dm_kw, eng_kw))
            except Exception as e:
                results.append({"name": name, "error": str(e)})
        print_results(console, label, results)

    # ================================================================
    # TEST 3: Decade-by-decade (non-overlapping periods)
    # ================================================================
    console.print(f"\n\n[bold yellow]TEST 3: NON-OVERLAPPING DECADES[/bold yellow]")
    console.print("Does the proposed config win in BOTH decades independently?\n")

    decades = [
        ("2005-2015 (GFC era)", date(2005, 1, 1), date(2015, 1, 1)),
        ("2015-2026 (Tech era)", date(2015, 1, 1), date(2026, 2, 1)),
        ("2005-2010 (Housing+GFC)", date(2005, 1, 1), date(2010, 1, 1)),
        ("2010-2015 (QE recovery)", date(2010, 1, 1), date(2015, 1, 1)),
        ("2015-2020 (Late bull)", date(2015, 1, 1), date(2020, 1, 1)),
        ("2020-2026 (COVID+AI)", date(2020, 1, 1), date(2026, 2, 1)),
    ]

    for label, start, end in decades:
        console.print(f"[bold]{label}[/bold]")
        results = []
        for name, dm_kw, eng_kw in configs:
            console.print(f"  [dim]Running {name}...[/dim]")
            try:
                results.append(run_config(name, prices, start, end, dm_kw, eng_kw))
            except Exception as e:
                results.append({"name": name, "error": str(e)})
        print_results(console, label, results)

    # ================================================================
    # SUMMARY
    # ================================================================
    console.print(f"\n\n[bold cyan]{'='*120}[/bold cyan]")
    console.print("[bold cyan]VERDICT[/bold cyan]")
    console.print("If PROPOSED beats CURRENT in the TEST period (2015-2026) AND wins in")
    console.print("most shifted starts AND wins in both decades → NOT overfitting.")
    console.print(f"[bold cyan]{'='*120}[/bold cyan]")


if __name__ == "__main__":
    main()
