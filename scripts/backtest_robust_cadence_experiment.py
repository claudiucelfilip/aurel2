#!/usr/bin/env python3
"""Experiment: rebalance cadence variants on top of Static_RobustLongHorizon."""

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

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import Asset, AssetCategory, AssetClass, Signal, SignalAction
from aurel2.data.momentum import calculate_momentum_scores
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy


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
]


@dataclass(frozen=True)
class CadenceConfig:
    name: str
    frequency: str
    conditional_spread: float = 0.0


CONFIGS = [
    CadenceConfig("Robust_Quarterly", "quarterly"),
    CadenceConfig("Robust_Monthly", "monthly"),
    CadenceConfig("Robust_ConditionalMonthly", "conditional", conditional_spread=0.08),
]


class ConditionalCadenceStrategy(DualMomentumStrategy):
    """Monthly checks with quarter-end default and strong-spread early switches."""

    def __init__(self, conditional_spread: float):
        super().__init__(
            assets=ASSET_REGISTRY,
            lookback_months=12,
            switch_threshold=0.02,
            cash_rate=0.0,
            pilot_entry_enabled=False,
        )
        self.conditional_spread = conditional_spread

    def get_rebalance_dates(self, start_date: date, end_date: date, frequency: str = "quarterly") -> list[date]:
        return super().get_rebalance_dates(start_date, end_date, "monthly")

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> Signal:
        base_signal = super().generate_signal(prices=prices, calc_date=calc_date, current_holding=current_holding)

        if calc_date.month in {3, 6, 9, 12}:
            return base_signal
        if self.current_holding is None or self.current_holding == AssetClass.CASH:
            return base_signal
        if base_signal.action != SignalAction.BUY:
            return base_signal

        scores = base_signal.momentum_scores
        current_score = scores.get(self.current_holding)
        target_asset = base_signal.asset
        target_class = target_asset.asset_class if target_asset else None
        target_score = scores.get(target_class) if target_class else None
        if current_score is None or target_score is None or target_class == self.current_holding:
            return base_signal

        momentum_diff = target_score.momentum_12m - current_score.momentum_12m
        current_is_equity = current_score.asset.category == AssetCategory.EQUITY
        target_is_equity = target_score.asset.category == AssetCategory.EQUITY
        if current_is_equity and not target_is_equity:
            effective_threshold = self.equity_to_defensive_threshold
        elif not current_is_equity and target_is_equity:
            effective_threshold = self.defensive_to_equity_threshold
        else:
            effective_threshold = self.switch_threshold

        strong_early_switch = momentum_diff >= max(effective_threshold, self.conditional_spread)
        weak_current = current_score.momentum_12m < 0.0
        if strong_early_switch or weak_current:
            return base_signal

        return Signal(
            date=calc_date,
            action=SignalAction.HOLD,
            asset=current_score.asset,
            reason=(
                f"Conditional cadence hold: waiting for quarter-end; "
                f"diff {momentum_diff:.2%} below early-switch bar {self.conditional_spread:.2%}"
            ),
            momentum_scores=scores,
        )


def build_strategy(config: CadenceConfig) -> DualMomentumStrategy:
    if config.frequency == "conditional":
        return ConditionalCadenceStrategy(config.conditional_spread)
    return DualMomentumStrategy(
        assets=ASSET_REGISTRY,
        lookback_months=12,
        switch_threshold=0.02,
        cash_rate=0.0,
        pilot_entry_enabled=False,
    )


def run_config(config: CadenceConfig, prices: pd.DataFrame, start: date, end: date) -> dict[str, Any]:
    engine = BacktestEngine(initial_capital=10000.0, use_ai=False, correlation_guard=False, sideways_hold=False)
    engine.dual_momentum = build_strategy(config)
    result = engine.run(
        prices=prices,
        start_date=start,
        end_date=end,
        benchmark_symbol="SPY",
        frequency="monthly" if config.frequency in {"monthly", "conditional"} else "quarterly",
    )
    result.calculate_metrics()

    years = (end - start).days / 365.25
    benchmark_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    benchmark_cagr = ((1.0 + benchmark_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    return {
        "name": config.name,
        "cagr": result.cagr,
        "bench_cagr": benchmark_cagr,
        "alpha_cagr": result.cagr - benchmark_cagr,
        "max_dd": result.max_drawdown,
        "trades": result.num_trades,
        "sharpe": result.sharpe_ratio,
    }


def main() -> None:
    console = Console()
    console.print("\n[bold cyan]ROBUST CADENCE EXPERIMENT[/bold cyan]")

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

        console.print(f"{'Config':<28} {'CAGR':>8} {'SPY':>8} {'Alpha':>8} {'MaxDD':>8} {'Trd':>4}")
        console.print("-" * 70)
        for row in rows:
            console.print(
                f"{row['name']:<28} {row['cagr']:+7.1%} {row['bench_cagr']:+7.1%} "
                f"{row['alpha_cagr']:+7.1%} {row['max_dd']:7.1%} {row['trades']:4d}"
            )

    output_path = Path("data/robust_cadence_experiment.json")
    output_path.write_text(json.dumps({"results": all_results}, indent=2))
    console.print(f"\nSaved results to {output_path}")


if __name__ == "__main__":
    main()
