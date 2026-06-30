#!/usr/bin/env python3
"""Backtest Aurel2 sell/rotation trigger hypotheses.

This compares the live baseline against narrower variants of the concrete
conditions that can make the current algorithm leave a holding:

- a lower same-category momentum switch threshold,
- an easier equity-to-defensive threshold,
- a higher cash hurdle for absolute momentum,
- non-DM-primary weighted voting across all three sleeves.

It is research-only: it does not touch live state or deployment files.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

SCRIPT_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
CWD_REPO_SRC = Path.cwd() / "src"
sys.path.insert(0, str(SCRIPT_REPO_SRC if SCRIPT_REPO_SRC.is_dir() else CWD_REPO_SRC))

from aurel2.agent.orchestrator import AgentOrchestrator
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import AssetClass
from aurel2.data.providers.cache import CachedPriceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy


structlog.configure(
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(file=open(os.devnull, "w")),
)
logging.disable(logging.CRITICAL)


END_DATE = date(2026, 6, 30)
FETCH_START = date(2003, 6, 1)
OUTPUT_PATH = Path(
    os.environ.get(
        "AUREL2_SELL_TRIGGER_OUTPUT_PATH",
        "data/sell_trigger_hypotheses_2026_06_30.json",
    )
)

PERIODS = [
    ("20y", date(2005, 1, 1), END_DATE),
    ("15y", date(2010, 1, 1), END_DATE),
    ("10y", date(2015, 1, 1), END_DATE),
    ("5y", date(2020, 1, 1), END_DATE),
    ("1y", date(2025, 6, 30), END_DATE),
    ("1m", date(2026, 5, 30), END_DATE),
]

QUICK_PERIODS = [
    ("1m", date(2026, 5, 30), END_DATE),
]


@dataclass(frozen=True)
class Variant:
    name: str
    hypothesis: str
    switch_threshold: float = 0.02
    equity_to_defensive_threshold: float = 0.15
    defensive_to_equity_threshold: float = 0.05
    cash_rate: float = 0.0
    dm_primary: bool = True


VARIANTS = [
    Variant(
        name="baseline_live",
        hypothesis="Current live-equivalent DM-primary config.",
    ),
    Variant(
        name="rotate_on_any_leader",
        hypothesis="Sell/switch as soon as any non-held asset leads at all.",
        switch_threshold=0.0,
    ),
    Variant(
        name="rotate_after_1pp_lead",
        hypothesis="Sell/switch when a same-category leader beats current by 1 percentage point.",
        switch_threshold=0.01,
    ),
    Variant(
        name="easy_defensive_exit",
        hypothesis="Leave equities for defensive assets after a 2 percentage point advantage instead of 15.",
        equity_to_defensive_threshold=0.02,
    ),
    Variant(
        name="cash_hurdle_2pct",
        hypothesis="Require held assets to beat a 2 percent cash hurdle.",
        cash_rate=0.02,
    ),
    Variant(
        name="weighted_three_sleeves",
        hypothesis="Let all three sleeves choose the final action instead of DM-primary mode.",
        dm_primary=False,
    ),
]


def no_tlt_assets() -> dict[AssetClass, Any]:
    return {ac: asset for ac, asset in ASSET_REGISTRY.items() if ac != AssetClass.BONDS_TREASURY}


def build_engine(variant: Variant) -> BacktestEngine:
    assets = no_tlt_assets()
    engine = BacktestEngine(initial_capital=10000.0, use_ai=False, correlation_guard=False, sideways_hold=False)
    engine.dual_momentum = DualMomentumStrategy(
        assets=assets,
        lookback_months=12,
        switch_threshold=variant.switch_threshold,
        equity_to_defensive_threshold=variant.equity_to_defensive_threshold,
        defensive_to_equity_threshold=variant.defensive_to_equity_threshold,
        cash_rate=variant.cash_rate,
        pilot_entry_enabled=False,
    )
    engine.multi_timeframe = MultiTimeframeTrendStrategy(
        target_assets=[ac for ac in assets if ac != AssetClass.CASH],
    )
    engine.orchestrator = AgentOrchestrator(
        calm_market_hold_threshold=0.0,
        correlation_guard_enabled=False,
        min_hold_enabled=False,
        sideways_hold_enabled=False,
        dm_primary_enabled=variant.dm_primary,
        use_dynamic_weights=not variant.dm_primary,
        use_regime_selection=not variant.dm_primary,
    )
    return engine


def benchmark_cagr(result: Any, start: date, end: date) -> float:
    years = (end - start).days / 365.25
    if years <= 0 or not result.benchmark_final:
        return 0.0
    benchmark_return = (result.benchmark_final / 10000.0) - 1.0
    return (1.0 + benchmark_return) ** (1.0 / years) - 1.0


def run_variant(variant: Variant, prices: pd.DataFrame, period: str, start: date, end: date) -> dict[str, Any]:
    engine = build_engine(variant)
    with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
        result = engine.run(
            prices=prices,
            start_date=start,
            end_date=end,
            frequency="daily",
            benchmark_symbol="SPY",
        )
    bench_cagr = benchmark_cagr(result, start, end)
    return {
        "period": period,
        "variant": variant.name,
        "cagr_pct": round(result.cagr * 100.0, 4),
        "spy_cagr_pct": round(bench_cagr * 100.0, 4),
        "alpha_cagr_pct": round((result.cagr - bench_cagr) * 100.0, 4),
        "total_return_pct": round(result.total_return * 100.0, 4),
        "max_drawdown_pct": round(result.max_drawdown * 100.0, 4),
        "sharpe": round(result.sharpe_ratio, 4),
        "trades": result.num_trades,
        "final_value": round(result.final_value, 2),
    }


def main() -> int:
    periods = QUICK_PERIODS if os.environ.get("AUREL2_SELL_TRIGGER_QUICK") == "1" else PERIODS
    provider = CachedPriceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    print(f"Loading {len(symbols)} symbols from {FETCH_START} to {END_DATE}...", flush=True)
    prices = provider.get_multi_prices(symbols, FETCH_START, END_DATE + timedelta(days=5))
    if prices.empty:
        raise RuntimeError("no price data loaded")
    print(f"Loaded {len(prices)} price rows", flush=True)

    rows: list[dict[str, Any]] = []
    for period, start, end in periods:
        print(f"\n{period} {start} -> {end}", flush=True)
        baseline_row: dict[str, Any] | None = None
        for variant in VARIANTS:
            row = run_variant(variant, prices, period, start, end)
            if variant.name == "baseline_live":
                baseline_row = row
            if baseline_row is not None:
                row["delta_vs_baseline_cagr_pct"] = round(
                    row["cagr_pct"] - baseline_row["cagr_pct"], 4
                )
                row["delta_vs_baseline_dd_pct"] = round(
                    row["max_drawdown_pct"] - baseline_row["max_drawdown_pct"], 4
                )
            rows.append(row)
            print(
                f"  {variant.name:<24} "
                f"CAGR {row['cagr_pct']:>8.2f}% "
                f"DD {row['max_drawdown_pct']:>7.2f}% "
                f"Sharpe {row['sharpe']:>5.2f} "
                f"Trades {row['trades']:>3}",
                flush=True,
            )

    payload = {
        "generated_at": "2026-06-30",
        "question": "How do earlier sell/rotation trigger hypotheses compare to current Aurel2?",
        "periods": [
            {"name": name, "start": start.isoformat(), "end": end.isoformat()}
            for name, start, end in periods
        ],
        "variants": [asdict(variant) for variant in VARIANTS],
        "rows": rows,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nSaved {OUTPUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
