#!/usr/bin/env python3
"""Experiment: volatility-scaled sizing on top of Static_RobustLongHorizon."""

from __future__ import annotations

import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aurel2.agent.orchestrator import AgentDecision, AgentOrchestrator
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import AssetClass
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
class SizingConfig:
    name: str
    mode: str
    target_vol: float
    min_size: float
    stress_drawdown: float
    stress_size: float
    extreme_vol: float


CONFIGS = [
    SizingConfig("Robust_Base", "none", 0.0, 1.0, 0.0, 1.0, 1.0),
    SizingConfig("Robust_VolTarget_18", "target", 0.18, 0.65, 0.15, 0.85, 0.28),
    SizingConfig("Robust_VolTarget_14", "target", 0.14, 0.50, 0.12, 0.75, 0.24),
    SizingConfig("Robust_StressClamp", "stress_clamp", 0.18, 0.60, 0.10, 0.70, 0.22),
]


class VolatilitySizingOrchestrator:
    """Delegate to the real orchestrator, then adjust only position sizing."""

    def __init__(self, base: AgentOrchestrator, config: SizingConfig):
        self.base = base
        self.config = config

    def analyze(self, signals: dict[str, Any], market_context: dict[str, Any], current_holding: str | None = None) -> AgentDecision:
        decision = self.base.analyze(signals=signals, market_context=market_context, current_holding=current_holding)
        if self.config.mode == "none":
            return decision
        if decision.action.value != "buy" or not decision.asset_symbol or decision.asset_symbol == "CASH":
            return decision

        asset_vols = market_context.get("asset_volatility_63d", {})
        target_vol = asset_vols.get(decision.asset_symbol)
        if target_vol is None or target_vol <= 0:
            return decision

        drawdown = market_context.get("drawdown", 0.0)
        size = 1.0

        if self.config.mode == "target":
            size = min(1.0, self.config.target_vol / target_vol)
            size = max(self.config.min_size, size)
            if drawdown >= self.config.stress_drawdown and target_vol >= self.config.extreme_vol:
                size = min(size, self.config.stress_size)
        elif self.config.mode == "stress_clamp":
            if target_vol >= self.config.extreme_vol:
                size = self.config.stress_size if drawdown >= self.config.stress_drawdown else 0.85
            elif drawdown >= self.config.stress_drawdown:
                size = 0.85
            size = max(self.config.min_size, min(1.0, size))

        if math.isclose(size, decision.position_size_pct, rel_tol=1e-9, abs_tol=1e-9):
            return decision

        return AgentDecision(
            decision_type=decision.decision_type,
            action=decision.action,
            asset_symbol=decision.asset_symbol,
            reasoning=(
                f"{decision.reasoning} | Vol sizing: {decision.asset_symbol} 63d vol "
                f"{target_vol:.1%}, size {size:.0%}"
            ),
            confidence=decision.confidence,
            strategy_signals=decision.strategy_signals,
            requires_approval=decision.requires_approval,
            timeout_hours=decision.timeout_hours,
            urgency=decision.urgency,
            market_context=decision.market_context,
            position_size_pct=size,
            regime=decision.regime,
        )


class VolatilitySizingBacktestEngine(BacktestEngine):
    """Backtest engine that enriches context with realized asset volatility."""

    def _build_market_context(self, prices: pd.DataFrame, calc_date: date) -> dict:
        context = super()._build_market_context(prices, calc_date)
        asset_volatility: dict[str, float] = {}

        for asset in ASSET_REGISTRY.values():
            symbol = asset.yahoo_symbol or asset.symbol
            symbol_prices = prices[prices["symbol"] == symbol].copy()
            if symbol_prices.empty:
                continue

            symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
            symbol_prices = symbol_prices[symbol_prices["date"] <= pd.Timestamp(calc_date)].sort_values("date")
            if len(symbol_prices) < 64:
                continue

            returns = symbol_prices.tail(64)["close"].pct_change().dropna()
            if len(returns) < 20:
                continue

            vol = float(returns.std() * math.sqrt(252))
            asset_volatility[asset.symbol] = vol

        context["asset_volatility_63d"] = asset_volatility
        return context


def build_engine(config: SizingConfig) -> BacktestEngine:
    engine = VolatilitySizingBacktestEngine(
        initial_capital=10000.0,
        use_ai=False,
        correlation_guard=False,
        sideways_hold=False,
    )
    engine.dual_momentum = DualMomentumStrategy(
        assets=ASSET_REGISTRY,
        lookback_months=12,
        switch_threshold=0.02,
        cash_rate=0.0,
        pilot_entry_enabled=False,
    )
    engine.orchestrator = VolatilitySizingOrchestrator(engine.orchestrator, config)
    return engine


def run_config(config: SizingConfig, prices: pd.DataFrame, start: date, end: date) -> dict[str, Any]:
    engine = build_engine(config)
    result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
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
    console.print("\n[bold cyan]ROBUST VOLATILITY SIZING EXPERIMENT[/bold cyan]")

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

        console.print(f"{'Config':<24} {'CAGR':>8} {'SPY':>8} {'Alpha':>8} {'MaxDD':>8} {'Trd':>4}")
        console.print("-" * 66)
        for row in rows:
            console.print(
                f"{row['name']:<24} {row['cagr']:+7.1%} {row['bench_cagr']:+7.1%} "
                f"{row['alpha_cagr']:+7.1%} {row['max_dd']:7.1%} {row['trades']:4d}"
            )

    output_path = Path("data/robust_volatility_sizing_experiment.json")
    output_path.write_text(json.dumps({"results": all_results}, indent=2))
    console.print(f"\nSaved results to {output_path}")


if __name__ == "__main__":
    main()
