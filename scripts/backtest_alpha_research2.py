#!/usr/bin/env python3
"""Round 2: Fine-tune the best config from round 1.

Full universe + 0% cash + 0% threshold was the clear winner.
Now test variations to find the optimal parameters:
- Small thresholds (1-5%) to reduce whipsaw
- Small cash rates (0-2%) for defensive protection
- With/without pilot entry
- With/without asymmetric thresholds
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.stdlib.BoundLogger, logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")))
logging.disable(logging.CRITICAL)

import pandas as pd
from rich.console import Console

from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy


def run_config(name, prices, start, end, dm_kwargs=None, engine_kwargs=None):
    """Run a backtest with specific configuration."""
    dm_kwargs = dm_kwargs or {}
    engine_kwargs = engine_kwargs or {}

    engine = BacktestEngine(
        initial_capital=10000,
        use_ai=False,
        **engine_kwargs,
    )
    if dm_kwargs:
        engine.dual_momentum = DualMomentumStrategy(**dm_kwargs)

    result = engine.run(
        prices=prices,
        start_date=start,
        end_date=end,
        benchmark_symbol="SPY",
    )

    years = (end - start).days / 365.25
    bench_return = ((result.benchmark_final / 10000) - 1) if result.benchmark_final else 0
    bench_cagr = ((1 + bench_return) ** (1 / years) - 1) if years > 0 else 0

    return {
        "name": name,
        "cagr": result.cagr,
        "bench_cagr": bench_cagr,
        "alpha": result.cagr - bench_cagr,
        "max_dd": result.max_drawdown,
        "sharpe": result.sharpe_ratio,
        "trades": result.num_trades,
        "final": result.final_value,
    }


def main():
    console = Console()
    console.print("\n[bold cyan]ALPHA RESEARCH ROUND 2: FINE-TUNING OPTIMAL PARAMETERS[/bold cyan]")
    console.print("=" * 120)

    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    fetch_start = date(2003, 6, 1)
    fetch_end = date.today() + timedelta(days=5)

    console.print(f"\nFetching {len(symbols)} symbols from {fetch_start}...")
    prices = provider.get_multi_prices(symbols, fetch_start, fetch_end)
    console.print(f"Total: {len(prices)} price records\n")

    if prices.empty:
        console.print("[red]ERROR: No price data[/red]")
        return

    periods = [
        ("20y", date(2005, 1, 1), date(2026, 2, 1)),
        ("15y", date(2010, 1, 1), date(2026, 2, 1)),
        ("10y", date(2015, 1, 1), date(2026, 2, 1)),
        ("5y",  date(2020, 1, 1), date(2026, 2, 1)),
    ]

    # All configs use full universe (ASSET_REGISTRY)
    base = {"assets": ASSET_REGISTRY, "pilot_entry_enabled": False}
    no_guard = {"correlation_guard": False, "sideways_hold": False}

    configs = [
        # Baseline: current production
        ("CURRENT (production)", {
            "assets": ASSET_REGISTRY, "switch_threshold": 0.10,
            "cash_rate": 0.04, "pilot_entry_enabled": True,
        }, {"correlation_guard": True, "sideways_hold": True}),

        # Round 1 winner
        ("R1 winner: 0%/0%", {**base, "switch_threshold": 0.0, "cash_rate": 0.0}, no_guard),

        # === Test switch thresholds with 0% cash ===
        ("0% cash, 1% thr", {**base, "switch_threshold": 0.01, "cash_rate": 0.0}, no_guard),
        ("0% cash, 2% thr", {**base, "switch_threshold": 0.02, "cash_rate": 0.0}, no_guard),
        ("0% cash, 3% thr", {**base, "switch_threshold": 0.03, "cash_rate": 0.0}, no_guard),
        ("0% cash, 5% thr", {**base, "switch_threshold": 0.05, "cash_rate": 0.0}, no_guard),

        # === Test cash rates with 0% threshold ===
        ("1% cash, 0% thr", {**base, "switch_threshold": 0.0, "cash_rate": 0.01}, no_guard),
        ("2% cash, 0% thr", {**base, "switch_threshold": 0.0, "cash_rate": 0.02}, no_guard),

        # === Best combo candidates ===
        ("1% cash, 2% thr", {**base, "switch_threshold": 0.02, "cash_rate": 0.01}, no_guard),
        ("0% cash, 2% thr + pilot", {
            "assets": ASSET_REGISTRY, "switch_threshold": 0.02,
            "cash_rate": 0.0, "pilot_entry_enabled": True,
        }, no_guard),

        # === Test with correlation guard (might help 2022) ===
        ("0% cash, 0% thr + corrguard", {**base, "switch_threshold": 0.0, "cash_rate": 0.0}, {
            "correlation_guard": True, "sideways_hold": False,
        }),
    ]

    for period_label, start, end in periods:
        console.print(f"\n[bold yellow]{'='*120}[/bold yellow]")
        console.print(f"[bold yellow]PERIOD: {period_label} ({start} → {end})[/bold yellow]")
        console.print(f"[bold yellow]{'='*120}[/bold yellow]")

        results = []
        for name, dm_kwargs, engine_kwargs in configs:
            console.print(f"  [dim]Running {name}...[/dim]")
            try:
                r = run_config(name, prices, start, end, dm_kwargs, engine_kwargs)
                results.append(r)
            except Exception as e:
                console.print(f"  [red]{name} FAILED: {e}[/red]")
                results.append({"name": name, "error": str(e)})

        console.print(f"\n  {'Config':<35} {'CAGR':>7} {'SPY':>7} {'Alpha':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>6} {'$10k→':>9}")
        console.print(f"  {'-'*95}")

        for r in results:
            if "error" in r:
                console.print(f"  {r['name']:<35} ERROR: {r['error'][:50]}")
                continue

            alpha_str = f"{r['alpha']:+6.1%}"
            if r["alpha"] > 0.005:
                alpha_str = f"[green]{alpha_str}[/green]"
            elif r["alpha"] < -0.005:
                alpha_str = f"[red]{alpha_str}[/red]"

            console.print(
                f"  {r['name']:<35} "
                f"{r['cagr']:+6.1%} "
                f"{r['bench_cagr']:+6.1%} "
                f"{alpha_str} "
                f"{r['max_dd']:6.1%} "
                f"{r['sharpe']:7.2f} "
                f"{r['trades']:6d} "
                f"${r['final']:>8,.0f}"
            )

        valid = [r for r in results if "error" not in r]
        if valid:
            best_alpha = max(valid, key=lambda r: r["alpha"])
            best_sharpe = max(valid, key=lambda r: r["sharpe"])
            console.print(f"\n  [bold green]Best alpha:  {best_alpha['name']} ({best_alpha['alpha']:+.1%}, Sharpe {best_alpha['sharpe']:.2f})[/bold green]")
            console.print(f"  [bold blue]Best Sharpe: {best_sharpe['name']} ({best_sharpe['sharpe']:.2f}, alpha {best_sharpe['alpha']:+.1%})[/bold blue]")


if __name__ == "__main__":
    main()
