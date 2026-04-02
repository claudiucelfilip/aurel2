#!/usr/bin/env python3
"""Quick experiment: robust quarterly with narrow crisis override."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.robust_quarterly import build_robust_quarterly_strategy
from aurel2.strategies.robust_quarterly_crisis import RobustQuarterlyCrisisStrategy


structlog.configure(
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")),
)
logging.disable(logging.CRITICAL)


END_DATE = date(2026, 4, 1)
FETCH_START = date(2003, 6, 1)
PERIODS = [
    ("15y", date(2010, 1, 1), END_DATE),
    ("10y", date(2015, 1, 1), END_DATE),
    ("5y", date(2020, 1, 1), END_DATE),
    ("1y", date(2025, 4, 1), END_DATE),
    ("1m", date(2026, 3, 1), END_DATE),
    ("20y", date(2005, 1, 1), END_DATE),
]


@dataclass(frozen=True)
class CrisisConfig:
    name: str
    drawdown_trigger: float | None = None
    vol_percentile_trigger: float | None = None


CONFIGS = [
    CrisisConfig("Robust_Quarterly"),
    CrisisConfig("Robust_Quarterly_Crisis", drawdown_trigger=0.12, vol_percentile_trigger=0.75),
    CrisisConfig("Robust_Quarterly_CrisisStrict", drawdown_trigger=0.15, vol_percentile_trigger=0.80),
]


def build_strategy(config: CrisisConfig):
    if config.drawdown_trigger is None:
        return build_robust_quarterly_strategy(), "quarterly"
    return (
        RobustQuarterlyCrisisStrategy(
            drawdown_trigger=config.drawdown_trigger,
            vol_percentile_trigger=config.vol_percentile_trigger or 0.75,
        ),
        "monthly",
    )


def run_config(config: CrisisConfig, prices: pd.DataFrame, start: date, end: date) -> dict[str, Any]:
    engine = BacktestEngine(initial_capital=10000.0, use_ai=False, correlation_guard=False, sideways_hold=False)
    strategy, frequency = build_strategy(config)
    engine.dual_momentum = strategy
    result = engine.run(
        prices=prices,
        start_date=start,
        end_date=end,
        benchmark_symbol="SPY",
        frequency=frequency,
    )
    result.calculate_metrics()

    years = (end - start).days / 365.25
    benchmark_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    benchmark_cagr = ((1.0 + benchmark_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    alpha_total = (result.total_return - benchmark_return) * 100
    return {
        "name": config.name,
        "cagr": result.cagr,
        "bench_cagr": benchmark_cagr,
        "alpha_cagr": result.cagr - benchmark_cagr,
        "alpha_total": alpha_total / 100,
        "max_dd": result.max_drawdown,
        "trades": result.num_trades,
        "sharpe": result.sharpe_ratio,
    }


def main() -> None:
    console = Console()
    console.print("\n[bold cyan]ROBUST CRISIS OVERRIDE EXPERIMENT[/bold cyan]")

    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    console.print(f"Fetching {len(symbols)} symbols from {FETCH_START}...")
    prices = provider.get_multi_prices(symbols, FETCH_START, END_DATE + timedelta(days=5))
    console.print(f"Loaded {len(prices)} price rows")

    all_results: list[dict[str, Any]] = []
    for label, start, end in PERIODS:
        console.print(f"\n[bold yellow]{label} ({start} -> {end})[/bold yellow]")
        rows = []
        for config in CONFIGS:
            console.print(f"  [dim]Running {config.name}...[/dim]")
            row = run_config(config, prices, start, end)
            row["period"] = label
            rows.append(row)
            all_results.append(row)

        console.print(f"{'Config':<32} {'CAGR':>8} {'SPY':>8} {'Alpha':>8} {'MaxDD':>8} {'Trd':>4}")
        console.print("-" * 76)
        for row in rows:
            console.print(
                f"{row['name']:<32} {row['cagr']:+7.1%} {row['bench_cagr']:+7.1%} "
                f"{row['alpha_cagr']:+7.1%} {row['max_dd']:7.1%} {row['trades']:4d}"
            )

    output_path = Path("data/robust_crisis_override_experiment.json")
    output_path.write_text(json.dumps({"results": all_results}, indent=2))
    console.print(f"\nSaved results to {output_path}")


if __name__ == "__main__":
    main()
