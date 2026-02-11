#!/usr/bin/env python3
"""Search dual-momentum parameters that improve 15y while protecting other alphas.

This is a constrained search utility:
- Objective: maximize 15y alpha (total-return alpha, same as dashboard metric)
- Guardrails: do not regress selected periods beyond a tolerance

Example:
    python3 scripts/optimize_15y_guarded.py --max-candidates 10
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import product
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy


PERIODS = [
    ("20y", timedelta(days=20 * 365)),
    ("15y", timedelta(days=15 * 365)),
    ("10y", timedelta(days=10 * 365)),
    ("5y", timedelta(days=5 * 365)),
    ("1y", timedelta(days=365)),
    ("3m", timedelta(days=90)),
]

GUARDRAIL_PERIODS = ["20y", "10y", "5y", "1y", "3m"]


@dataclass
class DMParams:
    switch_threshold: float
    equity_to_defensive_threshold: float
    defensive_to_equity_threshold: float
    pilot_entry_enabled: bool


def _alpha_total_return_pct(result) -> float:
    if not result.benchmark_final:
        return 0.0
    bench_return = ((result.benchmark_final / result.initial_capital) - 1) * 100
    return (result.total_return * 100) - bench_return


def _run_periods(prices: pd.DataFrame, params: DMParams, end_date: date) -> dict[str, float]:
    engine = BacktestEngine(initial_capital=10000, use_ai=False)
    engine.dual_momentum = DualMomentumStrategy(
        assets=ASSET_REGISTRY,
        switch_threshold=params.switch_threshold,
        equity_to_defensive_threshold=params.equity_to_defensive_threshold,
        defensive_to_equity_threshold=params.defensive_to_equity_threshold,
        pilot_entry_enabled=params.pilot_entry_enabled,
    )

    out: dict[str, float] = {}
    for label, delta in PERIODS:
        start_date = end_date - delta
        result = engine.run(
            prices=prices,
            start_date=start_date,
            end_date=end_date,
            benchmark_symbol="SPY",
        )
        out[label] = _alpha_total_return_pct(result)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guardrail-delta", type=float, default=0.0, help="Allowed alpha regression (pct points) on guardrail periods")
    parser.add_argument("--max-candidates", type=int, default=5)
    args = parser.parse_args()

    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    fetch_start = date.today() - timedelta(days=20 * 365 + 600)
    fetch_end = date.today() + timedelta(days=5)
    prices = provider.get_multi_prices(symbols, fetch_start, fetch_end)

    if prices.empty:
        print("No price data available (cache+fetch failed).")
        return

    prices["date"] = pd.to_datetime(prices["date"]).dt.date
    end_date = prices["date"].max()

    baseline_params = DMParams(
        switch_threshold=0.10,
        equity_to_defensive_threshold=0.15,
        defensive_to_equity_threshold=0.05,
        pilot_entry_enabled=True,
    )
    print("Running baseline...")
    baseline = _run_periods(prices, baseline_params, end_date)

    grid = [
        DMParams(*vals)
        for vals in product(
            [0.09, 0.10, 0.11],
            [0.13, 0.15, 0.17],
            [0.04, 0.05, 0.06],
            [True, False],
        )
    ]

    candidates: list[tuple[DMParams, dict[str, float]]] = []
    for params in grid:
        scores = _run_periods(prices, params, end_date)
        guardrail_ok = all(
            scores[p] >= baseline[p] - args.guardrail_delta
            for p in GUARDRAIL_PERIODS
        )
        if guardrail_ok and scores["15y"] > baseline["15y"]:
            candidates.append((params, scores))

    if not candidates:
        print("No candidates improved 15y while respecting guardrails.")
        print(f"Baseline alpha summary: {baseline}")
        return

    candidates.sort(key=lambda x: x[1]["15y"], reverse=True)

    print("\nTop candidates:")
    for i, (params, scores) in enumerate(candidates[: args.max_candidates], 1):
        print(
            f"{i}. switch={params.switch_threshold:.2f} "
            f"eq->def={params.equity_to_defensive_threshold:.2f} "
            f"def->eq={params.defensive_to_equity_threshold:.2f} "
            f"pilot={params.pilot_entry_enabled} "
            f"| alpha15y={scores['15y']:.1f} "
            f"(baseline {baseline['15y']:.1f})"
        )


if __name__ == "__main__":
    main()
