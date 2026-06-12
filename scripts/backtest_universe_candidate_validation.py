#!/usr/bin/env python3
"""Quick validation: Robust_Quarterly baseline vs universe tweak candidates.

Goal: find an asset-universe tweak that improves returns (esp. 15y) without
touching the live paper config.

Outputs:
- Prints a compact CAGR-only table (baseline, experiment, SPY) per interval.
- Writes JSON to data/universe_candidate_validation.json for record-keeping.
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


@dataclass(frozen=True)
class Candidate:
    name: str
    assets: dict[AssetClass, Asset]


def _run(prices: pd.DataFrame, assets: dict[AssetClass, Asset], start: date, end: date) -> dict[str, Any]:
    engine = BacktestEngine(initial_capital=10000.0, use_ai=False, correlation_guard=False, sideways_hold=False)
    engine.dual_momentum = DualMomentumStrategy(
        assets=assets,
        lookback_months=12,
        switch_threshold=0.02,
        cash_rate=0.0,
        pilot_entry_enabled=False,
    )
    result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY", frequency="quarterly")
    result.calculate_metrics()

    years = (end - start).days / 365.25
    bench_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    bench_cagr = ((1.0 + bench_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    strategy_total = (result.final_value / 10000.0 - 1.0) * 100.0
    benchmark_total = bench_return * 100.0
    return {
        "cagr": result.cagr,
        "bench_cagr": bench_cagr,
        "alpha_cagr": result.cagr - bench_cagr,
        "strat_total": strategy_total,
        "bench_total": benchmark_total,
        "alpha_total": strategy_total - benchmark_total,
        "max_dd": result.max_drawdown,
        "sharpe": result.sharpe_ratio,
        "turnover": result.turnover,
        "trades": result.num_trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Universe candidate validation runner.")
    parser.add_argument("--include-20y", action="store_true", help="Include 20y window.")
    parser.add_argument(
        "--force-20y",
        action="store_true",
        help="Run 20y even if no candidate beats baseline in the shorter windows.",
    )
    parser.add_argument(
        "--shortlist",
        action="store_true",
        help="Run only Robust_Quarterly_BASELINE vs Robust_Quarterly_NO_TLT.",
    )
    args = parser.parse_args()

    base_periods = [p for p in PERIODS if p[0] != "20y"]
    periods = list(base_periods)
    if args.include_20y:
        periods.append(next(p for p in PERIODS if p[0] == "20y"))

    baseline_assets = dict(ASSET_REGISTRY)
    no_tlt_assets = dict(ASSET_REGISTRY)
    no_tlt_assets.pop(AssetClass.BONDS_TREASURY, None)

    def swap(existing: dict[AssetClass, Asset], asset_class: AssetClass, symbol: str, name: str) -> dict[AssetClass, Asset]:
        v = dict(existing)
        old = v[asset_class]
        v[asset_class] = Asset(
            symbol=symbol,
            name=name,
            asset_class=asset_class,
            isin=old.isin,
            yahoo_symbol=symbol,
            category=old.category,
            ucits_symbol=old.ucits_symbol,
        )
        return v

    # Replacement ideas for the "TLT sleeve" (long duration has been a drag post-2022).
    # These are intentionally limited to symbols we already use (or have cached data for),
    # so this can run reliably even when Yahoo is flaky.
    tlt_to_ief_assets = swap(baseline_assets, AssetClass.BONDS_TREASURY, "IEF", "iShares 7-10 Year Treasury Bond ETF")
    tlt_to_shy_assets = swap(baseline_assets, AssetClass.BONDS_TREASURY, "SHY", "iShares 1-3 Year Treasury Bond ETF")
    tlt_to_agg_assets = swap(baseline_assets, AssetClass.BONDS_TREASURY, "AGG", "iShares Core U.S. Aggregate Bond ETF")
    tlt_to_tip_assets = swap(baseline_assets, AssetClass.BONDS_TREASURY, "TIP", "iShares TIPS Bond ETF")

    candidates = [
        Candidate("Robust_Quarterly_BASELINE", baseline_assets),
        Candidate("Robust_Quarterly_NO_TLT", no_tlt_assets),
        Candidate("Robust_Quarterly_TLT_TO_IEF", tlt_to_ief_assets),
        Candidate("Robust_Quarterly_TLT_TO_SHY", tlt_to_shy_assets),
        Candidate("Robust_Quarterly_TLT_TO_AGG", tlt_to_agg_assets),
        Candidate("Robust_Quarterly_TLT_TO_TIP", tlt_to_tip_assets),
    ]
    if args.shortlist:
        candidates = candidates[:2]

    provider = CachedPriceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    console = Console()
    console.print("\n[bold cyan]UNIVERSE CANDIDATE VALIDATION[/bold cyan]")
    console.print(f"Fetching {len(symbols)} symbols from {FETCH_START} (cached)...")
    prices = provider.get_multi_prices(symbols, FETCH_START, DEFAULT_END_DATE + timedelta(days=5))
    console.print(f"Loaded {len(prices)} price rows")

    results: dict[str, Any] = {
        "meta": {
            "end_date": DEFAULT_END_DATE.isoformat(),
            "frequency": "quarterly",
            "switch_threshold": 0.02,
            "cash_rate": 0.0,
            "pilot_entry_enabled": False,
        },
        "periods": {},
    }

    def run_period(label: str, start: date, end: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for cand in candidates:
            out = _run(prices, cand.assets, start, end)
            rows.append({"name": cand.name, **out})
        results["periods"][label] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": rows,
        }
        return rows

    def fmt_pct(x: float) -> str:
        return f"{x*100:+.1f}%"

    # Run short windows first; only run 20y if something beats baseline (unless forced).
    short_labels = [p[0] for p in base_periods]
    short_rows_by_period = {}
    for label, start, end in base_periods:
        rows = run_period(label, start, end)
        short_rows_by_period[label] = rows

        spy = rows[0]["bench_cagr"]
        base = next(r for r in rows if r["name"] == "Robust_Quarterly_BASELINE")["cagr"]
        # Best candidate by CAGR for this period (excluding baseline)
        best = max([r for r in rows if r["name"] != "Robust_Quarterly_BASELINE"], key=lambda r: r["cagr"])

        console.print(f"\n[bold yellow]{label} ({start} -> {end})[/bold yellow]")
        console.print(f"  Baseline: {fmt_pct(base)}   SPY: {fmt_pct(spy)}")
        console.print(
            f"  Best:     {best['name']}  {fmt_pct(best['cagr'])}  "
            f"Sharpe {best['sharpe']:.2f}  MaxDD {best['max_dd']:.1%}  "
            f"Turnover {best['turnover']:.2f}  Trades {best['trades']}"
        )

    should_run_20y = False
    if args.force_20y:
        should_run_20y = True
    elif args.include_20y:
        # Gate: require a candidate to beat baseline on 15y, 10y, 5y.
        def best_name(period: str) -> str:
            rows = short_rows_by_period[period]
            best = max([r for r in rows if r["name"] != "Robust_Quarterly_BASELINE"], key=lambda r: r["cagr"])
            return best["name"]

        # Use "NO_TLT" as the baseline expectation for this lever; if replacements cannot beat it,
        # we still consider "NO_TLT" the winner for this step.
        base15 = next(r for r in short_rows_by_period["15y"] if r["name"] == "Robust_Quarterly_BASELINE")["cagr"]
        base10 = next(r for r in short_rows_by_period["10y"] if r["name"] == "Robust_Quarterly_BASELINE")["cagr"]
        base5 = next(r for r in short_rows_by_period["5y"] if r["name"] == "Robust_Quarterly_BASELINE")["cagr"]

        best15 = max([r for r in short_rows_by_period["15y"] if r["name"] != "Robust_Quarterly_BASELINE"], key=lambda r: r["cagr"])
        best10 = max([r for r in short_rows_by_period["10y"] if r["name"] != "Robust_Quarterly_BASELINE"], key=lambda r: r["cagr"])
        best5 = max([r for r in short_rows_by_period["5y"] if r["name"] != "Robust_Quarterly_BASELINE"], key=lambda r: r["cagr"])

        should_run_20y = (best15["cagr"] > base15) and (best10["cagr"] > base10) and (best5["cagr"] > base5)

    if args.include_20y and should_run_20y:
        label, start, end = next(p for p in PERIODS if p[0] == "20y")
        rows = run_period(label, start, end)
        spy = rows[0]["bench_cagr"]
        base = next(r for r in rows if r["name"] == "Robust_Quarterly_BASELINE")["cagr"]
        best = max([r for r in rows if r["name"] != "Robust_Quarterly_BASELINE"], key=lambda r: r["cagr"])

        console.print(f"\n[bold yellow]{label} ({start} -> {end})[/bold yellow]")
        console.print(f"  Baseline: {fmt_pct(base)}   SPY: {fmt_pct(spy)}")
        console.print(
            f"  Best:     {best['name']}  {fmt_pct(best['cagr'])}  "
            f"Sharpe {best['sharpe']:.2f}  MaxDD {best['max_dd']:.1%}  "
            f"Turnover {best['turnover']:.2f}  Trades {best['trades']}"
        )
    elif args.include_20y:
        console.print("\n[dim]Skipping 20y (gate not met). Use --force-20y to run anyway.[/dim]")

    out_path = Path("data/universe_candidate_validation.json")
    out_path.write_text(json.dumps(results, indent=2))
    console.print(f"\nSaved results to {out_path}")


if __name__ == "__main__":
    main()
