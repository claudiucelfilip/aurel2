#!/usr/bin/env python3
"""Backtest script to validate enhanced momentum improvements.

Runs an incremental comparison:
  0. Classic GEM baseline (12mo, SPY/EFA/AGG, all-or-nothing)
  1. + Expanded asset universe
  2. + Multi-lookback ensemble (1-3-6-12)
  3. + Canary universe crash protection
  4. + 200-day SMA filter
  5. + Volatility-weighted scoring
  6. + Partial rotation (graduated allocation)

Each step enables one more feature so you can see the marginal impact.
"""

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, SignalAction
from aurel2.data.providers.cache import CachedPriceProvider
from aurel2.strategies.enhanced_momentum import (
    EnhancedMomentumStrategy,
    ASSET_SYMBOL_MAP,
    CANARY_SYMBOLS,
)


def fetch_prices(console: Console, start: date, end: date) -> pd.DataFrame:
    """Fetch all required price data using CachedPriceProvider (Yahoo + Parquet cache)."""
    provider = CachedPriceProvider()

    # Need extra days for 12-month lookback + 200-day SMA
    extended_start = start - timedelta(days=600)

    # Collect ALL symbols needed across all configs
    all_asset_classes = set()
    for config in CONFIGS:
        all_asset_classes.update(config["params"].get("offensive_assets", CLASSIC_OFF))
        all_asset_classes.update(config["params"].get("defensive_assets", CLASSIC_DEF))

    config_symbols = set()
    for ac in all_asset_classes:
        if ac in ASSET_REGISTRY:
            sym = ASSET_REGISTRY[ac].yahoo_symbol or ASSET_REGISTRY[ac].symbol
            config_symbols.add(sym)

    symbols = sorted(config_symbols | set(CANARY_SYMBOLS) | {"SPY", "BND", "AGG"})
    console.print(f"  Fetching {len(symbols)} symbols: {', '.join(symbols)}")

    all_dfs = []
    for symbol in symbols:
        try:
            df = provider.get_prices(symbol, extended_start, end + timedelta(days=5))
            if not df.empty:
                all_dfs.append(df)
                console.print(f"  [dim]{symbol}: {len(df)} rows[/dim]")
            else:
                console.print(f"  [yellow]Warning: {symbol} returned no data[/yellow]")
        except Exception as e:
            console.print(f"  [yellow]Warning: {symbol} failed: {e}[/yellow]")

    if not all_dfs:
        return pd.DataFrame(columns=["date", "close", "symbol"])

    return pd.concat(all_dfs, ignore_index=True)


def run_backtest(
    strategy: EnhancedMomentumStrategy,
    prices: pd.DataFrame,
    start_date: date,
    end_date: date,
    initial_capital: float = 10000.0,
    benchmark_symbol: str = "SPY",
) -> dict:
    """Run a single backtest and return metrics."""
    rebalance_dates = strategy.get_rebalance_dates(start_date, end_date)

    cash = initial_capital
    holding: AssetClass | None = None
    holding_symbol: str | None = None
    shares = 0.0
    trades = 0
    values = []

    for rebal_date in rebalance_dates:
        signal = strategy.generate_signal(prices, rebal_date, holding)

        target_ac = signal.asset_class
        target_symbol = ASSET_SYMBOL_MAP.get(target_ac) if target_ac else None

        if signal.action == SignalAction.BUY and target_ac and target_symbol:
            # Sell current
            if holding and holding_symbol:
                sell_price = _get_price(prices, holding_symbol, rebal_date)
                if sell_price and shares > 0:
                    cash += shares * sell_price * 0.999  # 0.1% cost
                    shares = 0.0
                    trades += 1

            # Buy new
            buy_price = _get_price(prices, target_symbol, rebal_date)
            if buy_price and buy_price > 0:
                invest = cash * 0.999
                shares = invest / buy_price
                cash = cash - invest
                holding = target_ac
                holding_symbol = target_symbol
                trades += 1

        elif signal.action == SignalAction.SELL:
            if holding and holding_symbol:
                sell_price = _get_price(prices, holding_symbol, rebal_date)
                if sell_price and shares > 0:
                    cash += shares * sell_price * 0.999
                    shares = 0.0
                    trades += 1
            holding = None
            holding_symbol = None

        # Record portfolio value
        if holding_symbol and shares > 0:
            p = _get_price(prices, holding_symbol, rebal_date)
            total = cash + (shares * p if p else 0)
        else:
            total = cash
        values.append(total)

    final = values[-1] if values else initial_capital

    # Metrics
    years = (end_date - start_date).days / 365.25
    total_return = (final / initial_capital) - 1
    cagr = (final / initial_capital) ** (1 / years) - 1 if years > 0 else 0

    # Max drawdown
    peak = values[0] if values else initial_capital
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Sharpe
    if len(values) > 1:
        rets = pd.Series(values).pct_change().dropna()
        sharpe = (rets.mean() * 12) / (rets.std() * np.sqrt(12)) if rets.std() > 0 else 0
    else:
        sharpe = 0

    # Benchmark
    bench_return = _benchmark_return(prices, benchmark_symbol, start_date, end_date)
    bench_cagr = (1 + bench_return) ** (1 / years) - 1 if years > 0 else 0

    return {
        "final": final,
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "trades": trades,
        "alpha": cagr - bench_cagr,
        "bench_return": bench_return,
        "bench_cagr": bench_cagr,
    }


def _get_price(prices: pd.DataFrame, symbol: str, as_of: date) -> float | None:
    df = prices[prices["symbol"] == symbol].copy()
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    rows = df[df["date"] <= pd.Timestamp(as_of)].sort_values("date")
    return float(rows.iloc[-1]["close"]) if not rows.empty else None


def _benchmark_return(
    prices: pd.DataFrame, symbol: str, start: date, end: date,
) -> float:
    start_p = _get_price(prices, symbol, start + timedelta(days=5))
    end_p = _get_price(prices, symbol, end)
    if start_p and end_p and start_p > 0:
        return (end_p / start_p) - 1
    return 0.0


# ── Focused asset sets ────────────────────────────────────────────────────

# Classic Antonacci GEM: SPY vs EFA, fallback AGG
CLASSIC_OFF = [AssetClass.US_STOCKS, AssetClass.INTL_DEVELOPED]
CLASSIC_DEF = [AssetClass.BONDS_AGGREGATE]

# Improved defensive: pick best-of-4 instead of just AGG
BETTER_DEF = [
    AssetClass.BONDS_SHORT_TERM,    # SHY - safe haven
    AssetClass.BONDS_INTERMEDIATE,  # IEF - duration diversified
    AssetClass.TIPS,                # TIP - inflation protection
    AssetClass.GOLD,                # GLD - crisis alpha
]

# Sector rotation: SPY + top sectors
SECTOR_OFF = [
    AssetClass.US_STOCKS,          # SPY
    AssetClass.TECH_SECTOR,        # XLK - tech outperformance
    AssetClass.HEALTHCARE_SECTOR,  # XLV - defensive growth
    AssetClass.FINANCIAL_SECTOR,   # XLF - cyclical exposure
]

# ── Strategy configurations ──────────────────────────────────────────────

CONFIGS = [
    # --- Baseline: True classic GEM ---
    {
        "label": "0. Classic GEM (12mo, abs gate)",
        "params": {
            "use_multi_lookback": False,
            "use_canary": False,
            "use_sma_filter": False,
            "use_vol_weighting": False,
            "use_partial_rotation": False,
            "use_absolute_momentum": True,
            "abs_momentum_threshold": 0.0,
            "offensive_assets": CLASSIC_OFF,
            "defensive_assets": CLASSIC_DEF,
            "top_n_offensive": 1,
            "top_n_defensive": 1,
        },
    },

    # --- WINNER: High switch threshold, always offensive ---
    {
        "label": "1. ENHANCED: 8% thresh, no gate",
        "params": {
            "use_multi_lookback": False,
            "use_canary": False,
            "use_sma_filter": False,
            "use_vol_weighting": False,
            "use_partial_rotation": False,
            "use_absolute_momentum": False,
            "offensive_assets": CLASSIC_OFF,
            "defensive_assets": CLASSIC_DEF,
            "top_n_offensive": 1,
            "top_n_defensive": 1,
            "switch_threshold": 0.08,
        },
    },

    # --- With crash protection for 2008-style events ---
    {
        "label": "2. ENHANCED + deep crash gate (-15%)",
        "params": {
            "use_multi_lookback": False,
            "use_canary": False,
            "use_sma_filter": False,
            "use_vol_weighting": False,
            "use_partial_rotation": False,
            "use_absolute_momentum": True,
            "abs_momentum_threshold": -0.15,
            "offensive_assets": CLASSIC_OFF,
            "defensive_assets": CLASSIC_DEF,
            "top_n_offensive": 1,
            "top_n_defensive": 1,
            "switch_threshold": 0.08,
        },
    },

    # --- Sector rotation variant ---
    {
        "label": "3. SECTOR: SPY/XLK/XLV/XLF, 5%",
        "params": {
            "use_multi_lookback": False,
            "use_canary": False,
            "use_sma_filter": False,
            "use_vol_weighting": False,
            "use_partial_rotation": False,
            "use_absolute_momentum": False,
            "offensive_assets": SECTOR_OFF,
            "defensive_assets": CLASSIC_DEF,
            "top_n_offensive": 1,
            "top_n_defensive": 1,
            "switch_threshold": 0.05,
        },
    },
]


def main():
    console = Console()

    console.print("\n[bold cyan]ENHANCED MOMENTUM BACKTEST — INCREMENTAL VALIDATION[/bold cyan]")
    console.print("=" * 80)

    # Periods to test
    today = date.today()
    periods = [
        ("10y", date(today.year - 10, today.month, 1), today),
        ("5y", date(today.year - 5, today.month, 1), today),
    ]

    # Fetch data (once, covering the longest period)
    earliest = min(p[1] for p in periods)
    console.print(f"\nFetching price data from {earliest}...")
    prices = fetch_prices(console, earliest, today)
    console.print(f"Total: {len(prices)} price records\n")

    for period_label, start_date, end_date in periods:
        console.print(f"\n[bold]═══ {period_label} PERIOD: {start_date} → {end_date} ═══[/bold]\n")

        table = Table(title=f"{period_label} Performance Comparison")
        table.add_column("Strategy", style="cyan", min_width=35)
        table.add_column("CAGR", justify="right")
        table.add_column("Total Ret", justify="right")
        table.add_column("MaxDD", justify="right")
        table.add_column("Sharpe", justify="right")
        table.add_column("Alpha", justify="right")
        table.add_column("Trades", justify="right")

        for config in CONFIGS:
            strategy = EnhancedMomentumStrategy(**config["params"])
            result = run_backtest(strategy, prices, start_date, end_date)

            # Color coding
            alpha_color = "green" if result["alpha"] > 0 else "red"
            cagr_color = "green" if result["cagr"] > result["bench_cagr"] else "white"

            table.add_row(
                config["label"],
                f"[{cagr_color}]{result['cagr']:.1%}[/{cagr_color}]",
                f"{result['total_return']:.1%}",
                f"[red]-{result['max_drawdown']:.1%}[/red]",
                f"{result['sharpe']:.2f}",
                f"[{alpha_color}]{result['alpha']:+.1%}[/{alpha_color}]",
                str(result["trades"]),
            )

        # Add SPY benchmark row
        bench = _benchmark_return(prices, "SPY", start_date, end_date)
        years = (end_date - start_date).days / 365.25
        bench_cagr = (1 + bench) ** (1 / years) - 1

        table.add_row(
            "[dim]SPY Buy & Hold[/dim]",
            f"[dim]{bench_cagr:.1%}[/dim]",
            f"[dim]{bench:.1%}[/dim]",
            "[dim]—[/dim]",
            "[dim]—[/dim]",
            "[dim]—[/dim]",
            "[dim]—[/dim]",
            style="dim",
        )

        console.print(table)

    # Save results
    console.print("\n[bold green]Backtest complete.[/bold green]")


if __name__ == "__main__":
    main()
