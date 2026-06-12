#!/usr/bin/env python3
"""Compare 12-month vs 9-month momentum lookback across market regimes."""

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


PERIODS = [
    ("Full 20y",        date(2005, 1, 1),  date(2026, 2, 1)),
    ("Full 15y",        date(2010, 1, 1),  date(2026, 2, 1)),
    ("Pre-GFC Bull",    date(2005, 1, 1),  date(2007, 10, 1)),
    ("GFC Crash",       date(2007, 10, 1), date(2009, 3, 1)),
    ("GFC Recovery",    date(2009, 3, 1),  date(2013, 1, 1)),
    ("Steady Bull",     date(2013, 1, 1),  date(2016, 1, 1)),
    ("Late Bull+COVID", date(2016, 1, 1),  date(2020, 3, 23)),
    ("COVID V-shape",   date(2020, 3, 23), date(2022, 1, 1)),
    ("Rate Hike Bear",  date(2022, 1, 1),  date(2022, 12, 31)),
    ("AI Bull",         date(2023, 1, 1),  date(2026, 2, 1)),
    ("GFC Full Cycle",  date(2005, 1, 1),  date(2013, 1, 1)),
    ("COVID Full Cycle",date(2019, 1, 1),  date(2023, 12, 31)),
]

ALT_LOOKBACK = 9
LOOKBACKS = [12, ALT_LOOKBACK]


def main():
    console = Console()
    console.print(f"\n[bold cyan]12-MONTH vs {ALT_LOOKBACK}-MONTH MOMENTUM LOOKBACK COMPARISON[/bold cyan]")
    console.print("=" * 110)

    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    console.print(f"Fetching {len(symbols)} symbols from 2003-06-01...")
    prices = provider.get_multi_prices(symbols, date(2003, 6, 1), date.today() + timedelta(days=5))
    console.print(f"Total: {len(prices)} price records\n")

    if prices.empty:
        console.print("[red]ERROR: No price data[/red]")
        return

    # Run all backtests
    all_results = {}
    for lookback in LOOKBACKS:
        console.print(f"\n[bold]--- Running {lookback}-month lookback ---[/bold]")
        for label, start, end in PERIODS:
            console.print(f"  {label}...", end=" ")
            try:
                engine = BacktestEngine(initial_capital=10000, use_ai=False)
                engine.dual_momentum = DualMomentumStrategy(
                    assets=ASSET_REGISTRY,
                    lookback_months=lookback,
                )
                result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")

                years = (end - start).days / 365.25
                bench_return = ((result.benchmark_final / 10000) - 1) if result.benchmark_final else 0
                bench_cagr = ((1 + bench_return) ** (1 / years) - 1) if years > 0 else 0

                all_results[(label, lookback)] = {
                    "cagr": result.cagr,
                    "max_dd": result.max_drawdown,
                    "sharpe": result.sharpe_ratio,
                    "trades": result.num_trades,
                    "bench_cagr": bench_cagr,
                    "alpha": result.cagr - bench_cagr,
                    "final": result.final_value,
                }
                console.print(f"CAGR {result.cagr:+.1%}")
            except Exception as e:
                console.print(f"[red]FAILED: {e}[/red]")
                all_results[(label, lookback)] = None

    # Print comparison table
    alt = ALT_LOOKBACK
    console.print("\n\n")
    header = f"{'Period':<18} {'SPY':>7} | {'12m CAGR':>8} {'Alpha':>7} {'MaxDD':>7} {'Shrp':>5} {'Trd':>4} | {f'{alt}m CAGR':>8} {'Alpha':>7} {'MaxDD':>7} {'Shrp':>5} {'Trd':>4} | {'Winner':>7}"
    console.print("[bold]" + header + "[/bold]")
    console.print("-" * len(header))

    wins_12 = 0
    wins_alt = 0

    for label, start, end in PERIODS:
        r12 = all_results.get((label, 12))
        ralt = all_results.get((label, alt))

        if not r12 or not ralt:
            console.print(f"{label:<18} ERROR")
            continue

        if label == "GFC Full Cycle":
            console.print("-" * len(header))

        if r12["cagr"] > ralt["cagr"]:
            winner = "12m"
            wins_12 += 1
        elif ralt["cagr"] > r12["cagr"]:
            winner = f"{alt}m"
            wins_alt += 1
        else:
            winner = "tie"

        w_color = "cyan" if winner == "12m" else ("magenta" if winner == f"{alt}m" else "white")
        a12_color = "green" if r12["alpha"] > 0 else "red"
        aalt_color = "green" if ralt["alpha"] > 0 else "red"

        line = (
            f"{label:<18} {r12['bench_cagr']:+6.1%} | "
            f"{r12['cagr']:+7.1%} [{a12_color}]{r12['alpha']:+6.1%}[/{a12_color}] {r12['max_dd']:6.1%} {r12['sharpe']:5.2f} {r12['trades']:4d} | "
            f"{ralt['cagr']:+7.1%} [{aalt_color}]{ralt['alpha']:+6.1%}[/{aalt_color}] {ralt['max_dd']:6.1%} {ralt['sharpe']:5.2f} {ralt['trades']:4d} | "
            f"[{w_color}]{winner:>7}[/{w_color}]"
        )
        console.print(line)

    console.print("-" * len(header))
    console.print(f"\n[bold]Score: 12-month wins {wins_12}, {alt}-month wins {wins_alt}[/bold]")

    console.print(f"\n[bold]Key period deltas ({alt}m - 12m):[/bold]")
    for key_period in ["GFC Recovery", "COVID V-shape", "Rate Hike Bear", "Full 20y"]:
        r12 = all_results.get((key_period, 12))
        ralt = all_results.get((key_period, alt))
        if r12 and ralt:
            cagr_diff = ralt["cagr"] - r12["cagr"]
            dd_diff = ralt["max_dd"] - r12["max_dd"]
            color = "green" if cagr_diff > 0 else "red"
            console.print(f"  {key_period:<18} CAGR: [{color}]{cagr_diff:+.1%}[/{color}]  MaxDD: {dd_diff:+.1%}")


if __name__ == "__main__":
    main()
