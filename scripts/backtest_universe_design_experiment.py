#!/usr/bin/env python3
"""Experiment: asset universe design for Robust_Quarterly.

This tests two things:
1) Ablation: remove one asset class at a time (leave-one-out).
2) Replacements: swap a symbol within an existing AssetClass (keeps categories).

Goal: improve 15y/10y/5y vs SPY without adding strategy complexity.
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

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import Asset, AssetCategory, AssetClass
from aurel2.data.providers.cache import CachedPriceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy


structlog.configure(
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")),
)
logging.disable(logging.CRITICAL)


DEFAULT_END_DATE = date(2026, 4, 1)
FETCH_START = date(2003, 6, 1)
PERIODS = [
    ("15y", date(2010, 1, 1), DEFAULT_END_DATE),
    ("10y", date(2015, 1, 1), DEFAULT_END_DATE),
    ("5y", date(2020, 1, 1), DEFAULT_END_DATE),
    ("1y", date(2025, 4, 1), DEFAULT_END_DATE),
    ("1m", date(2026, 3, 1), DEFAULT_END_DATE),
    ("20y", date(2005, 1, 1), DEFAULT_END_DATE),
]


def _symbols_for_assets(assets: dict[AssetClass, Asset]) -> list[str]:
    symbols: list[str] = []
    for asset in assets.values():
        if asset.yahoo_symbol:
            symbols.append(asset.yahoo_symbol)
    if "SPY" not in symbols:
        symbols.append("SPY")
    return sorted(set(symbols))


def _run_backtest(
    prices: pd.DataFrame,
    assets: dict[AssetClass, Asset],
    start: date,
    end: date,
) -> dict[str, Any]:
    engine = BacktestEngine(initial_capital=10000.0, use_ai=False, correlation_guard=False, sideways_hold=False)
    engine.dual_momentum = DualMomentumStrategy(
        assets=assets,
        lookback_months=12,
        switch_threshold=0.02,
        cash_rate=0.0,
        pilot_entry_enabled=False,
    )
    result = engine.run(
        prices=prices,
        start_date=start,
        end_date=end,
        benchmark_symbol="SPY",
        frequency="quarterly",
    )
    result.calculate_metrics()

    years = (end - start).days / 365.25
    benchmark_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    benchmark_cagr = ((1.0 + benchmark_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    alpha_total = (result.total_return - benchmark_return) * 100
    return {
        "cagr": result.cagr,
        "bench_cagr": benchmark_cagr,
        "alpha_cagr": result.cagr - benchmark_cagr,
        "alpha_total": alpha_total / 100,
        "max_dd": result.max_drawdown,
        "trades": result.num_trades,
        "sharpe": result.sharpe_ratio,
    }


@dataclass(frozen=True)
class Variant:
    name: str
    assets: dict[AssetClass, Asset]


def _baseline_assets() -> dict[AssetClass, Asset]:
    # Copy to allow safe mutations in variants.
    return dict(ASSET_REGISTRY)


def _ablation_variants(base: dict[AssetClass, Asset]) -> list[Variant]:
    variants: list[Variant] = [Variant("BASELINE", base)]

    removable = [k for k in base.keys() if k != AssetClass.CASH and k != AssetClass.US_STOCKS]
    for k in removable:
        v = dict(base)
        v.pop(k, None)
        variants.append(Variant(f"NO_{k.value.upper()}", v))

    # A few grouped removals that commonly reduce churn/noise.
    sectors = {AssetClass.TECH_SECTOR, AssetClass.FINANCIAL_SECTOR, AssetClass.ENERGY_SECTOR, AssetClass.HEALTHCARE_SECTOR}
    v = dict(base)
    for k in sectors:
        v.pop(k, None)
    variants.append(Variant("NO_SECTORS", v))

    alts = {AssetClass.GOLD, AssetClass.COMMODITIES, AssetClass.REITS}
    v = dict(base)
    for k in alts:
        v.pop(k, None)
    variants.append(Variant("NO_ALTS", v))

    return variants


def _replacement_variants(base: dict[AssetClass, Asset]) -> list[Variant]:
    variants: list[Variant] = [Variant("BASELINE", base)]

    def swap_symbol(asset_class: AssetClass, yahoo_symbol: str, name: str) -> Variant:
        v = dict(base)
        old = v[asset_class]
        v[asset_class] = Asset(
            symbol=yahoo_symbol,
            name=name,
            asset_class=asset_class,
            isin=old.isin,
            yahoo_symbol=yahoo_symbol,
            category=old.category,
            ucits_symbol=old.ucits_symbol,
        )
        return Variant(f"{asset_class.value.upper()}_{yahoo_symbol}", v)

    # Keep this list to long-history replacements to avoid "missing data" artifacts.
    # EM: EEM → VWO (inception ~2005)
    variants.append(swap_symbol(AssetClass.EMERGING_MARKETS, "VWO", "Vanguard FTSE Emerging Markets ETF"))
    # REITs: VNQ → IYR (inception ~2000)
    variants.append(swap_symbol(AssetClass.REITS, "IYR", "iShares U.S. Real Estate ETF"))
    # Small cap value: IJS → VBR (inception ~2004)
    variants.append(swap_symbol(AssetClass.SMALL_CAP_VALUE, "VBR", "Vanguard Small-Cap Value ETF"))

    # A couple of "risk-off sleeve" alternates.
    # Aggregate bonds: AGG → LQD (inception ~2002; corporates)
    variants.append(swap_symbol(AssetClass.BONDS_AGGREGATE, "LQD", "iShares iBoxx $ Investment Grade Corporate Bond ETF"))

    return variants


def main() -> None:
    parser = argparse.ArgumentParser(description="Universe design experiment runner.")
    parser.add_argument(
        "--mode",
        choices=["ablation", "replacement", "both"],
        default="both",
        help="Which experiment set to run.",
    )
    parser.add_argument(
        "--include-20y",
        action="store_true",
        help="Include 20y window (stress history).",
    )
    parser.add_argument(
        "--output",
        default="data/universe_design_experiment.json",
        help="Output JSON path.",
    )
    args = parser.parse_args()

    console = Console()
    console.print("\n[bold cyan]UNIVERSE DESIGN EXPERIMENT[/bold cyan]")

    end_date = DEFAULT_END_DATE
    periods = [p for p in PERIODS if args.include_20y or p[0] != "20y"]

    base = _baseline_assets()
    variant_sets: list[tuple[str, list[Variant]]] = []
    if args.mode in {"ablation", "both"}:
        variant_sets.append(("ablation", _ablation_variants(base)))
    if args.mode in {"replacement", "both"}:
        variant_sets.append(("replacement", _replacement_variants(base)))

    # Fetch a superset of symbols required by all variants once.
    required_symbols: set[str] = set()
    for _, variants in variant_sets:
        for v in variants:
            required_symbols.update(_symbols_for_assets(v.assets))
    symbols = sorted(required_symbols)

    provider = CachedPriceProvider()
    console.print(f"Fetching {len(symbols)} symbols from {FETCH_START}...")
    prices = provider.get_multi_prices(symbols, FETCH_START, end_date + timedelta(days=5))
    console.print(f"Loaded {len(prices)} price rows")

    results: dict[str, Any] = {
        "meta": {
            "end_date": end_date.isoformat(),
            "mode": args.mode,
            "include_20y": bool(args.include_20y),
            "frequency": "quarterly",
            "strategy": {
                "lookback_months": 12,
                "switch_threshold": 0.02,
                "cash_rate": 0.0,
                "pilot_entry_enabled": False,
            },
        },
        "periods": {},
    }

    for period_label, start, end in periods:
        console.print(f"\n[bold yellow]{period_label} ({start} -> {end})[/bold yellow]")
        period_out: dict[str, Any] = {}

        for group_name, variants in variant_sets:
            console.print(f"  [dim]{group_name} variants: {len(variants)}[/dim]")
            rows: list[dict[str, Any]] = []
            for variant in variants:
                r = _run_backtest(prices=prices, assets=variant.assets, start=start, end=end)
                rows.append({"name": variant.name, **r})
            # Sort by alpha_cagr desc for quick scanning.
            rows.sort(key=lambda x: x["alpha_cagr"], reverse=True)
            period_out[group_name] = rows

            best = rows[0]
            console.print(
                f"    best {group_name}: {best['name']} "
                f"CAGR {best['cagr']:+.1%} vs SPY {best['bench_cagr']:+.1%} "
                f"alpha {best['alpha_cagr']:+.1%}, MaxDD {best['max_dd']:.1%}, trades {best['trades']}"
            )

        results["periods"][period_label] = period_out

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    console.print(f"\nSaved results to {out}")


if __name__ == "__main__":
    main()
