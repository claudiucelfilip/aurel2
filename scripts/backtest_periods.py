#!/usr/bin/env python3
"""Backtest the production strategy across historically significant market periods.

Tests how the full live path (3 strategies + orchestrator) performs during:
- Pre-GFC bull, GFC crash, QE recovery, late bull, COVID, rate hikes, AI rally
- Full 20-year history (2005-2026)
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
from rich.table import Table

from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.engine.backtest import BacktestEngine


# Key market periods with context
PERIODS = [
    # label, start, end, description
    ("Full 20y",        date(2005, 1, 1),  date(2026, 2, 1),  "Full available history"),
    ("Full 15y",        date(2010, 1, 1),  date(2026, 2, 1),  "Post-GFC to present"),
    ("Pre-GFC Bull",    date(2005, 1, 1),  date(2007, 10, 1), "Housing bubble, easy money"),
    ("GFC Crash",       date(2007, 10, 1), date(2009, 3, 1),  "Financial crisis, -55% SPY"),
    ("GFC Recovery",    date(2009, 3, 1),  date(2013, 1, 1),  "QE1/QE2, strong recovery"),
    ("Steady Bull",     date(2013, 1, 1),  date(2016, 1, 1),  "Low vol, steady growth"),
    ("Late Bull+COVID", date(2016, 1, 1),  date(2020, 3, 23), "Trade wars, COVID crash"),
    ("COVID V-shape",   date(2020, 3, 23), date(2022, 1, 1),  "Stimulus rally, meme stocks"),
    ("Rate Hike Bear",  date(2022, 1, 1),  date(2022, 12, 31),"Stocks AND bonds fell"),
    ("AI Bull",         date(2023, 1, 1),  date(2026, 2, 1),  "Tech-led recovery"),
    # Cross-crisis periods
    ("GFC Full Cycle",  date(2005, 1, 1),  date(2013, 1, 1),  "Bull → crash → recovery"),
    ("COVID Full Cycle",date(2019, 1, 1),  date(2023, 12, 31),"Pre-COVID → crash → rally → rate hikes"),
]


def main():
    console = Console()
    console.print("\n[bold cyan]STRATEGY PERFORMANCE ACROSS MARKET REGIMES[/bold cyan]")
    console.print("Production strategy: 3 strategies + orchestrator (no AI)")
    console.print("=" * 100)

    # Fetch data covering all periods (2004 start for 2005 lookback buffer)
    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    # Need data from 2003 for 400-day lookback buffer before 2005
    fetch_start = date(2003, 6, 1)
    fetch_end = date.today() + timedelta(days=5)

    console.print(f"\nFetching {len(symbols)} symbols from {fetch_start}...")
    prices = provider.get_multi_prices(symbols, fetch_start, fetch_end)
    console.print(f"Total: {len(prices)} price records\n")

    if prices.empty:
        console.print("[red]ERROR: No price data[/red]")
        return

    # Run backtests
    results = []
    for label, start, end, desc in PERIODS:
        console.print(f"[dim]Running {label} ({start} → {end})...[/dim]")
        try:
            engine = BacktestEngine(initial_capital=10000, use_ai=False)
            result = engine.run(
                prices=prices,
                start_date=start,
                end_date=end,
                benchmark_symbol="SPY",
            )

            # Calculate benchmark metrics
            years = (end - start).days / 365.25
            bench_return = ((result.benchmark_final / 10000) - 1) if result.benchmark_final else 0
            bench_cagr = ((1 + bench_return) ** (1 / years) - 1) if years > 0 else 0

            results.append({
                "label": label,
                "desc": desc,
                "start": start,
                "end": end,
                "years": years,
                "cagr": result.cagr,
                "total_return": result.total_return,
                "max_dd": result.max_drawdown,
                "sharpe": result.sharpe_ratio,
                "trades": result.num_trades,
                "bench_cagr": bench_cagr,
                "bench_return": bench_return,
                "alpha": result.cagr - bench_cagr,
                "final": result.final_value,
            })
        except Exception as e:
            console.print(f"  [red]FAILED: {e}[/red]")
            results.append({
                "label": label, "desc": desc, "start": start, "end": end,
                "error": str(e),
            })

    # Display results - plain text for reliability
    console.print("\n")
    console.print("[bold]Production Strategy vs SPY Buy & Hold Across Market Regimes[/bold]")
    console.print("-" * 105)
    header = f"{'Period':<18} {'Dates':<22} {'Yrs':>4} {'StrCAGR':>8} {'SPYCAGR':>8} {'Alpha':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trd':>4} {'$10k→':>9}"
    console.print(header)
    console.print("-" * 105)

    for r in results:
        if "error" in r:
            console.print(f"{r['label']:<18} {r['start']:%Y-%m} → {r['end']:%Y-%m}   ERROR: {r['error'][:40]}")
            continue

        if r["label"] == "GFC Full Cycle":
            console.print("-" * 105)

        line = (
            f"{r['label']:<18} "
            f"{r['start']:%Y-%m} → {r['end']:%Y-%m}  "
            f"{r['years']:4.1f} "
            f"{r['cagr']:+7.1%} "
            f"{r['bench_cagr']:+7.1%} "
            f"{r['alpha']:+6.1%} "
            f"{r['max_dd']:6.1%} "
            f"{r['sharpe']:7.2f} "
            f"{r['trades']:4d} "
            f"${r['final']:>8,.0f}"
        )
        console.print(line)

    console.print("-" * 105)

    # Summary stats
    valid = [r for r in results if "error" not in r]
    periods_with_alpha = sum(1 for r in valid if r["alpha"] > 0)
    avg_alpha = sum(r["alpha"] for r in valid) / len(valid) if valid else 0
    worst_dd = max(r["max_dd"] for r in valid) if valid else 0
    worst_dd_period = next((r["label"] for r in valid if r["max_dd"] == worst_dd), "?")

    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Periods with positive alpha: {periods_with_alpha}/{len(valid)}")
    console.print(f"  Average alpha (annualized): {avg_alpha:+.1%}")
    console.print(f"  Worst drawdown: -{worst_dd:.1%} ({worst_dd_period})")

    # Full 20y compound result
    full = next((r for r in valid if r["label"] == "Full 20y"), None)
    if full:
        console.print(f"\n[bold]Full 20-Year Result:[/bold]")
        console.print(f"  $10,000 → ${full['final']:,.0f} (strategy) vs ${10000 * (1 + full['bench_return']):,.0f} (SPY)")
        console.print(f"  CAGR: {full['cagr']:+.1%} vs SPY {full['bench_cagr']:+.1%}")
        console.print(f"  Max drawdown: -{full['max_dd']:.1%}")


if __name__ == "__main__":
    main()
