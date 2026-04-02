#!/usr/bin/env python3
"""Experiment: stronger short-term override variants on Static_RobustLongHorizon.

Goal:
- Preserve the strong long-horizon behavior of the robust policy
- Improve recent responsiveness with short-term overrides that can actually change holdings

Variants:
- Robust_Base
- Robust_3m_Override
- Robust_3m6m_Consensus
- Robust_3m_WeakExit
"""

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
from aurel2.core.models import AssetClass, Signal, SignalAction
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
class ConfirmationConfig:
    name: str
    mode: str
    short_lookbacks: tuple[int, ...]
    override_gap: float
    long_tolerance: float


BASE_SWITCH_THRESHOLD = 0.02
BASE_CASH_RATE = 0.0
BASE_PILOT_ENTRY = False

CONFIGS = [
    ConfirmationConfig("Robust_Base", "none", (), 0.00, 0.00),
    ConfirmationConfig("Robust_3m_Override", "override", (3,), 0.05, 0.04),
    ConfirmationConfig("Robust_3m6m_Consensus", "consensus", (3, 6), 0.04, 0.04),
    ConfirmationConfig("Robust_3m_WeakExit", "weak_exit", (3,), 0.03, 0.06),
]


class ConfirmedRobustStrategy(DualMomentumStrategy):
    """Robust long-horizon strategy with stronger short-term override logic."""

    def __init__(self, config: ConfirmationConfig):
        super().__init__(
            assets=ASSET_REGISTRY,
            lookback_months=12,
            switch_threshold=BASE_SWITCH_THRESHOLD,
            cash_rate=BASE_CASH_RATE,
            pilot_entry_enabled=BASE_PILOT_ENTRY,
        )
        self.config = config

    def _scores_for_lookback(self, prices: pd.DataFrame, calc_date: date, lookback_months: int) -> dict[AssetClass, Any]:
        return calculate_momentum_scores(
            prices=prices,
            assets=self.assets,
            calc_date=calc_date,
            lookback_months=lookback_months,
            cash_rate=self.cash_rate,
        )

    def _winner_for_lookback(self, prices: pd.DataFrame, calc_date: date, lookback_months: int) -> AssetClass | None:
        scores = self._scores_for_lookback(prices, calc_date, lookback_months)
        risky_scores = {k: v for k, v in scores.items() if k != AssetClass.CASH}
        if not risky_scores:
            return None
        return max(risky_scores.keys(), key=lambda k: risky_scores[k].momentum_12m)

    def generate_signal(self, prices: pd.DataFrame, calc_date: date, current_holding: AssetClass | None = None) -> Signal:
        base_signal = super().generate_signal(prices=prices, calc_date=calc_date, current_holding=current_holding)

        if self.config.mode == "none":
            return base_signal

        scores = base_signal.momentum_scores
        if not scores or self.current_holding is None or self.current_holding == AssetClass.CASH:
            return base_signal
        if base_signal.action == SignalAction.SELL:
            return base_signal

        current_score = scores.get(self.current_holding)
        if current_score is None:
            return base_signal

        short_winners = [self._winner_for_lookback(prices, calc_date, lookback) for lookback in self.config.short_lookbacks]
        short_winners = [winner for winner in short_winners if winner is not None]
        if not short_winners:
            return base_signal

        candidate_class: AssetClass | None = None
        if self.config.mode == "override":
            candidate_class = short_winners[0]
        elif self.config.mode == "consensus":
            if len(short_winners) == len(self.config.short_lookbacks) and len(set(short_winners)) == 1:
                candidate_class = short_winners[0]
        elif self.config.mode == "weak_exit":
            candidate_class = short_winners[0]
        else:
            return base_signal

        if candidate_class is None or candidate_class == self.current_holding:
            return base_signal

        candidate_score = scores.get(candidate_class)
        if candidate_score is None:
            return base_signal

        candidate_short_scores = [
            self._scores_for_lookback(prices, calc_date, lookback).get(candidate_class)
            for lookback in self.config.short_lookbacks
        ]
        current_short_scores = [
            self._scores_for_lookback(prices, calc_date, lookback).get(self.current_holding)
            for lookback in self.config.short_lookbacks
        ]
        if any(score is None for score in candidate_short_scores) or any(score is None for score in current_short_scores):
            return base_signal

        candidate_short_avg = sum(score.momentum_12m for score in candidate_short_scores) / len(candidate_short_scores)
        current_short_avg = sum(score.momentum_12m for score in current_short_scores) / len(current_short_scores)
        short_gap = candidate_short_avg - current_short_avg
        long_gap_vs_current = candidate_score.momentum_12m - current_score.momentum_12m

        cash_score = scores.get(AssetClass.CASH)
        candidate_beats_cash = cash_score is None or candidate_score.momentum_12m > cash_score.momentum_12m
        candidate_long_ok = long_gap_vs_current >= -self.config.long_tolerance

        if self.config.mode == "weak_exit":
            weak_current = current_short_avg < 0.0
            strong_candidate = candidate_short_avg > 0.0
            should_override = weak_current and strong_candidate
        else:
            should_override = True

        if should_override and candidate_beats_cash and candidate_long_ok and short_gap >= self.config.override_gap:
            self.current_holding = candidate_class
            self.is_pilot_position = False
            return Signal(
                date=calc_date,
                action=SignalAction.BUY,
                asset=candidate_score.asset,
                reason=(
                    f"Short-term override to {candidate_class.value}: "
                    f"short gap {short_gap:.2%}, 12m gap {long_gap_vs_current:.2%}"
                ),
                momentum_scores=scores,
            )

        return base_signal


def run_config(name: str, strategy: DualMomentumStrategy, prices: pd.DataFrame, start: date, end: date) -> dict[str, Any]:
    engine = BacktestEngine(initial_capital=10000.0, use_ai=False, correlation_guard=False, sideways_hold=False)
    engine.dual_momentum = strategy
    result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
    result.calculate_metrics()

    years = (end - start).days / 365.25
    benchmark_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    benchmark_cagr = ((1.0 + benchmark_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    return {
        "name": name,
        "cagr": result.cagr,
        "bench_cagr": benchmark_cagr,
        "alpha_cagr": result.cagr - benchmark_cagr,
        "max_dd": result.max_drawdown,
        "trades": result.num_trades,
        "sharpe": result.sharpe_ratio,
    }


def build_strategy(config: ConfirmationConfig) -> DualMomentumStrategy:
    if config.mode == "none":
        return DualMomentumStrategy(
            assets=ASSET_REGISTRY,
            lookback_months=12,
            switch_threshold=BASE_SWITCH_THRESHOLD,
            cash_rate=BASE_CASH_RATE,
            pilot_entry_enabled=BASE_PILOT_ENTRY,
        )
    return ConfirmedRobustStrategy(config)


def main() -> None:
    console = Console()
    console.print("\n[bold cyan]ROBUST CONFIRMATION EXPERIMENT[/bold cyan]")

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
            row = run_config(config.name, build_strategy(config), prices, start, end)
            row["period"] = label
            rows.append(row)
            all_results.append(row)

        console.print(f"{'Config':<24} {'CAGR':>8} {'SPY':>8} {'Alpha':>8} {'MaxDD':>8} {'Trd':>4}")
        console.print("-" * 66)
        for row in rows:
            console.print(
                f"{row['name']:<24} {row['cagr']:+7.1%} {row['bench_cagr']:+7.1%} "
                f"{row['alpha_cagr']:+7.1%} {row['max_dd']:7.1%} {row['trades']:4d}"
            )

    output_path = Path("data/robust_confirmation_experiment.json")
    output_path.write_text(json.dumps({"results": all_results}, indent=2))
    console.print(f"\nSaved results to {output_path}")


if __name__ == "__main__":
    main()
