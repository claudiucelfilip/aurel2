#!/usr/bin/env python3
"""Research script: diagnose and improve 20y/15y alpha.

Tests multiple configurations against the current strategy to identify
which parameters are destroying alpha and what classic GEM would produce.
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.stdlib.BoundLogger, logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")))
logging.disable(logging.CRITICAL)

import pandas as pd
from rich.console import Console
from rich.table import Table

from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import AssetClass, AssetCategory, Asset
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy


# Classic GEM universe: only SPY + EFA as offensive, AGG as defensive
GEM_ASSETS = {
    AssetClass.US_STOCKS: ASSET_REGISTRY[AssetClass.US_STOCKS],
    AssetClass.INTL_DEVELOPED: ASSET_REGISTRY[AssetClass.INTL_DEVELOPED],
    AssetClass.BONDS_AGGREGATE: ASSET_REGISTRY[AssetClass.BONDS_AGGREGATE],
    AssetClass.CASH: ASSET_REGISTRY[AssetClass.CASH],
}

# Expanded but limited: SPY + EFA + GLD (for alternatives)
GEM_PLUS_GOLD = {
    **GEM_ASSETS,
    AssetClass.GOLD: ASSET_REGISTRY[AssetClass.GOLD],
}


def run_config(name, prices, start, end, dm_kwargs=None, engine_kwargs=None):
    """Run a backtest with specific DM and engine configuration."""
    dm_kwargs = dm_kwargs or {}
    engine_kwargs = engine_kwargs or {}

    engine = BacktestEngine(
        initial_capital=10000,
        use_ai=False,
        **engine_kwargs,
    )

    # Override DM strategy if custom kwargs provided
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
    console.print("\n[bold cyan]ALPHA RESEARCH: DIAGNOSING 20Y/15Y UNDERPERFORMANCE[/bold cyan]")
    console.print("=" * 120)

    # Fetch data
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

    # Test periods
    periods = [
        ("20y", date(2005, 1, 1), date(2026, 2, 1)),
        ("15y", date(2010, 1, 1), date(2026, 2, 1)),
        ("10y", date(2015, 1, 1), date(2026, 2, 1)),
        ("5y",  date(2020, 1, 1), date(2026, 2, 1)),
    ]

    # Configurations to test
    configs = [
        # 0. Current production (baseline)
        ("CURRENT (production)", {
            "assets": ASSET_REGISTRY,
            "switch_threshold": 0.10,
            "cash_rate": 0.04,
            "pilot_entry_enabled": True,
        }, {
            "correlation_guard": True,
            "sideways_hold": True,
        }),

        # 1. Classic GEM: SPY/EFA/AGG, no threshold, 0% cash rate
        ("Classic GEM", {
            "assets": GEM_ASSETS,
            "switch_threshold": 0.0,
            "cash_rate": 0.0,
            "pilot_entry_enabled": False,
        }, {
            "correlation_guard": False,
            "sideways_hold": False,
        }),

        # 2. GEM + small threshold to reduce whipsaw
        ("GEM + 3% threshold", {
            "assets": GEM_ASSETS,
            "switch_threshold": 0.03,
            "cash_rate": 0.0,
            "pilot_entry_enabled": False,
        }, {
            "correlation_guard": False,
            "sideways_hold": False,
        }),

        # 3. GEM + 5% threshold
        ("GEM + 5% threshold", {
            "assets": GEM_ASSETS,
            "switch_threshold": 0.05,
            "cash_rate": 0.0,
            "pilot_entry_enabled": False,
        }, {
            "correlation_guard": False,
            "sideways_hold": False,
        }),

        # 4. GEM + gold as extra defensive
        ("GEM + Gold", {
            "assets": GEM_PLUS_GOLD,
            "switch_threshold": 0.0,
            "cash_rate": 0.0,
            "pilot_entry_enabled": False,
        }, {
            "correlation_guard": False,
            "sideways_hold": False,
        }),

        # 5. Full universe but 0% cash rate and no threshold
        ("Full universe, 0% cash, 0% thr", {
            "assets": ASSET_REGISTRY,
            "switch_threshold": 0.0,
            "cash_rate": 0.0,
            "pilot_entry_enabled": False,
        }, {
            "correlation_guard": False,
            "sideways_hold": False,
        }),

        # 6. Current DM but no orchestrator overrides
        ("Current DM, no overrides", {
            "assets": ASSET_REGISTRY,
            "switch_threshold": 0.10,
            "cash_rate": 0.04,
            "pilot_entry_enabled": True,
        }, {
            "correlation_guard": False,
            "sideways_hold": False,
        }),

        # 7. GEM + asymmetric thresholds (5% to leave equity, 3% to return)
        ("GEM + asym 5%/3%", {
            "assets": GEM_ASSETS,
            "switch_threshold": 0.05,
            "cash_rate": 0.0,
            "pilot_entry_enabled": False,
        }, {
            "correlation_guard": False,
            "sideways_hold": False,
        }),

        # 8. GEM + correlation guard (redirect bonds to GLD in correlated regimes)
        ("GEM + corr guard", {
            "assets": GEM_PLUS_GOLD,
            "switch_threshold": 0.0,
            "cash_rate": 0.0,
            "pilot_entry_enabled": False,
        }, {
            "correlation_guard": True,
            "sideways_hold": False,
        }),

        # 9. GEM + 2% cash rate (moderate absolute momentum gate)
        ("GEM + 2% cash rate", {
            "assets": GEM_ASSETS,
            "switch_threshold": 0.0,
            "cash_rate": 0.02,
            "pilot_entry_enabled": False,
        }, {
            "correlation_guard": False,
            "sideways_hold": False,
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

        # Display results
        console.print(f"\n  {'Config':<35} {'CAGR':>7} {'SPY':>7} {'Alpha':>7} {'MaxDD':>7} {'Sharpe':>7} {'Trades':>6} {'$10k→':>9}")
        console.print(f"  {'-'*95}")

        for r in results:
            if "error" in r:
                console.print(f"  {r['name']:<35} ERROR: {r['error'][:50]}")
                continue

            # Highlight positive alpha
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

        # Find best config
        valid = [r for r in results if "error" not in r]
        if valid:
            best = max(valid, key=lambda r: r["alpha"])
            console.print(f"\n  [bold green]Best alpha: {best['name']} ({best['alpha']:+.1%})[/bold green]")


if __name__ == "__main__":
    main()
