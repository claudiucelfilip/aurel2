#!/usr/bin/env python3
"""Experiment: does the orchestrator's calm-market hold suppress performance?

Hypothesis:
- The orchestrator's calm-market hold (default 5% drawdown threshold) can block
  valid DM switches even when the portfolio is lagging the new leader.
- Disabling it may improve long-horizon CAGR, especially once cadence is already quarterly.

We test with the Robust_Quarterly DM config (12m, 2% threshold, 0% cash hurdle, pilot disabled),
comparing:
- Baseline universe
- NO_TLT universe (best universe tweak so far)
and within each:
- calm hold enabled (default)
- calm hold disabled (threshold = 0)

Backtest order: 15y, 10y, 5y, 1y, 1m; 20y optional.
"""

from __future__ import annotations

import argparse
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
from aurel2.core.models import Asset, AssetClass
from aurel2.data.providers.cache import CachedPriceProvider
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
    ("20y", date(2005, 1, 1), END_DATE),
]


@dataclass(frozen=True)
class Variant:
    name: str
    assets: dict[AssetClass, Asset]
    calm_hold_threshold: float


def _run(prices: pd.DataFrame, v: Variant, start: date, end: date) -> dict[str, Any]:
    engine = BacktestEngine(initial_capital=10000.0, use_ai=False, correlation_guard=False, sideways_hold=False)
    engine.dual_momentum = DualMomentumStrategy(
        assets=v.assets,
        lookback_months=12,
        switch_threshold=0.02,
        cash_rate=0.0,
        pilot_entry_enabled=False,
    )
    # Override calm-market hold threshold (default is 5% drawdown)
    engine.orchestrator.calm_market_hold_threshold = v.calm_hold_threshold

    result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY", frequency="quarterly")
    result.calculate_metrics()

    years = (end - start).days / 365.25
    bench_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    bench_cagr = ((1.0 + bench_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    return {"cagr": result.cagr, "bench_cagr": bench_cagr}


def main() -> None:
    parser = argparse.ArgumentParser(description="Calm-hold orchestrator experiment.")
    parser.add_argument("--include-20y", action="store_true", help="Include 20y window.")
    args = parser.parse_args()

    base_assets = dict(ASSET_REGISTRY)
    no_tlt_assets = dict(ASSET_REGISTRY)
    no_tlt_assets.pop(AssetClass.BONDS_TREASURY, None)

    variants = [
        Variant("BASELINE_calm_hold_5pct", base_assets, 0.05),
        Variant("BASELINE_calm_hold_OFF", base_assets, 0.0),
        Variant("NO_TLT_calm_hold_5pct", no_tlt_assets, 0.05),
        Variant("NO_TLT_calm_hold_OFF", no_tlt_assets, 0.0),
    ]

    provider = CachedPriceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    console = Console()
    console.print("\n[bold cyan]CALM-HOLD EXPERIMENT[/bold cyan]")
    console.print(f"Fetching {len(symbols)} symbols from {FETCH_START} (cached)...")
    prices = provider.get_multi_prices(symbols, FETCH_START, END_DATE + timedelta(days=5))
    console.print(f"Loaded {len(prices)} price rows")

    periods = [p for p in PERIODS if args.include_20y or p[0] != "20y"]
    results: dict[str, Any] = {"meta": {"end_date": END_DATE.isoformat()}, "periods": {}}

    def pct(x: float) -> str:
        return f"{x*100:+.1f}%"

    for label, start, end in periods:
        rows = []
        for v in variants:
            out = _run(prices, v, start, end)
            rows.append({"name": v.name, **out})
        results["periods"][label] = {"start": start.isoformat(), "end": end.isoformat(), "rows": rows}

        spy = rows[0]["bench_cagr"]
        best = max(rows, key=lambda r: r["cagr"])
        console.print(f"\n[bold yellow]{label} ({start} -> {end})[/bold yellow]")
        console.print(f"  SPY CAGR: {pct(spy)}")
        console.print(f"  Best: {best['name']}  {pct(best['cagr'])}")

    out_path = Path("data/calm_hold_experiment.json")
    out_path.write_text(json.dumps(results, indent=2))
    console.print(f"\nSaved results to {out_path}")


if __name__ == "__main__":
    main()

