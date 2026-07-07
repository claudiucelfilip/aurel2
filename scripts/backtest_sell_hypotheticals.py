#!/usr/bin/env python3
"""Backtest sell/rotation hypotheticals against the live Aurel2 path.

This answers a narrow operational question:
if we make Aurel2 more willing to leave the current winner, what would that
have done historically on the same live-style pipeline?

The scenarios here only change the knobs that matter for "finally sell":
- same-category switch threshold
- equity -> defensive switch threshold
- defensive -> equity switch threshold
- correlation guard
- sideways-hold

Everything else mirrors the current live checker/backtest defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve()
for candidate in (
    _HERE.parent.parent / "src",
    Path("/root/aurel2/src"),
    Path("/app/src"),
):
    if candidate.exists():
        sys.path.insert(0, str(candidate))
        break

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import AssetClass, SignalAction
from aurel2.data.providers.cache import CACHE_DIR, CachedPriceProvider
from aurel2.engine.backtest import BacktestEngine, BacktestResult
from aurel2.strategies.dual_momentum import DualMomentumStrategy


WATCHLIST = ["SPY", "QQQ", "GLD", "TLT", "IWM", "EFA", "EEM", "XLE", "XLK", "AGG", "IEF"]
NO_TLT_ASSETS = {
    asset_class: asset for asset_class, asset in ASSET_REGISTRY.items() if asset_class != AssetClass.BONDS_TREASURY
}


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    switch_threshold: float = 0.02
    equity_to_defensive_threshold: float = 0.15
    defensive_to_equity_threshold: float = 0.05
    correlation_guard: bool = False
    sideways_hold: bool = False


SCENARIOS: list[Scenario] = [
    Scenario(
        name="live_baseline",
        description="Current live path.",
    ),
    Scenario(
        name="same_cat_zero",
        description="Switch between risk assets as soon as another one leads.",
        switch_threshold=0.0,
    ),
    Scenario(
        name="defensive_10",
        description="Need only a 10% momentum edge to leave equity for defense.",
        equity_to_defensive_threshold=0.10,
    ),
    Scenario(
        name="defensive_05",
        description="Need only a 5% momentum edge to leave equity for defense.",
        equity_to_defensive_threshold=0.05,
    ),
    Scenario(
        name="defensive_00",
        description="Leave equity for defense as soon as defense leads at all.",
        equity_to_defensive_threshold=0.0,
    ),
    Scenario(
        name="corr_guard_on",
        description="Baseline thresholds, but redirect bond rotations when SPY/AGG are too correlated.",
        correlation_guard=True,
    ),
    Scenario(
        name="sideways_on",
        description="Baseline thresholds plus sideways-hold suppression.",
        sideways_hold=True,
    ),
    Scenario(
        name="fast_exit_combo",
        description="Most eager exit: zero same-cat threshold, zero equity->defensive threshold, correlation guard on.",
        switch_threshold=0.0,
        equity_to_defensive_threshold=0.0,
        correlation_guard=True,
    ),
]


def load_prices(start_date: date, end_date: date, refresh_missing: bool) -> pd.DataFrame:
    symbols = sorted(set(get_all_yahoo_symbols() + WATCHLIST))
    cache_dir = Path(os.environ.get("AUREL2_PRICE_CACHE_DIR", str(CACHE_DIR)))

    if refresh_missing:
        provider = CachedPriceProvider(cache_dir=cache_dir)
        return provider.get_multi_prices(symbols, start_date, end_date)

    frames = []
    for symbol in symbols:
        cache_path = cache_dir / f"{symbol}.parquet"
        if not cache_path.exists():
            continue
        df = pd.read_parquet(cache_path)
        if df.empty:
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[(df["date"] >= start_date - timedelta(days=400)) & (df["date"] <= end_date)]
        if not df.empty:
            frames.append(df[["date", "close", "symbol"]])

    if not frames:
        return pd.DataFrame(columns=["date", "close", "symbol"])

    return pd.concat(frames, ignore_index=True)


def _spy_cagr(prices: pd.DataFrame, start: date, end: date, initial_capital: float = 10000.0) -> float | None:
    spy = prices[(prices["symbol"] == "SPY") & (prices["date"] >= start) & (prices["date"] <= end)].copy()
    if spy.empty:
        return None
    start_px = float(spy.iloc[0]["close"])
    end_px = float(spy.iloc[-1]["close"])
    if start_px <= 0:
        return None
    final = initial_capital * (end_px / start_px)
    years = (end - start).days / 365.25
    if years <= 0:
        return None
    return (final / initial_capital) ** (1 / years) - 1


def _rotation_events(result: BacktestResult) -> int:
    return len([trade for trade in result.trades if trade.action == SignalAction.BUY])


def run_scenario(prices: pd.DataFrame, start: date, end: date, scenario: Scenario) -> dict:
    engine = BacktestEngine(
        initial_capital=10000.0,
        use_ai=False,
        correlation_guard=scenario.correlation_guard,
        sideways_hold=scenario.sideways_hold,
    )
    engine.dual_momentum = DualMomentumStrategy(
        assets=NO_TLT_ASSETS,
        lookback_months=12,
        switch_threshold=scenario.switch_threshold,
        equity_to_defensive_threshold=scenario.equity_to_defensive_threshold,
        defensive_to_equity_threshold=scenario.defensive_to_equity_threshold,
        cash_rate=0.0,
        pilot_entry_enabled=False,
    )

    old_stdout = sys.stdout
    sys.stdout = StringIO()
    try:
        result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
    finally:
        sys.stdout = old_stdout
    result.calculate_metrics()

    spy_cagr = _spy_cagr(prices, start, end, initial_capital=10000.0)
    return {
        "scenario": scenario.name,
        "description": scenario.description,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cagr": round(result.cagr, 6),
        "total_return": round(result.total_return, 6),
        "max_drawdown": round(result.max_drawdown, 6),
        "sharpe": round(result.sharpe_ratio, 6),
        "trades": _rotation_events(result),
        "final_value": round(result.final_value, 2),
        "spy_cagr": round(spy_cagr, 6) if spy_cagr is not None else None,
        "alpha_vs_spy_cagr": round(result.cagr - spy_cagr, 6) if spy_cagr is not None else None,
        "params": {
            "switch_threshold": scenario.switch_threshold,
            "equity_to_defensive_threshold": scenario.equity_to_defensive_threshold,
            "defensive_to_equity_threshold": scenario.defensive_to_equity_threshold,
            "correlation_guard": scenario.correlation_guard,
            "sideways_hold": scenario.sideways_hold,
        },
    }


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2%}"


def _format_money(value: float) -> str:
    return f"${value:,.0f}"


def print_report(rows: list[dict], label: str) -> None:
    baseline = next((row for row in rows if row["scenario"] == "live_baseline"), None)
    baseline_cagr = baseline["cagr"] if baseline else None
    print()
    print(f"Period: {label}")
    print("-" * 126)
    print(
        f"{'scenario':<18} {'CAGR':>8} {'vs base':>9} {'vs SPY':>9} {'MaxDD':>9} "
        f"{'Sharpe':>8} {'Trades':>7} {'Final':>10}"
    )
    print("-" * 126)
    for row in rows:
        vs_base = None if baseline_cagr is None else row["cagr"] - baseline_cagr
        print(
            f"{row['scenario']:<18} "
            f"{_format_pct(row['cagr']):>8} "
            f"{_format_pct(vs_base):>9} "
            f"{_format_pct(row['alpha_vs_spy_cagr']):>9} "
            f"{_format_pct(-row['max_drawdown']):>9} "
            f"{row['sharpe']:>8.2f} "
            f"{row['trades']:>7} "
            f"{_format_money(row['final_value']):>10}"
        )
    print("-" * 126)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest Aurel2 sell-threshold hypotheticals.")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="End date (YYYY-MM-DD). Default: today.")
    parser.add_argument(
        "--period",
        action="append",
        dest="periods",
        help="Period spec label:years, for example 1y:1 or 5y:5. Repeatable.",
    )
    parser.add_argument(
        "--refresh-missing",
        action="store_true",
        help="Fetch missing cached prices from Yahoo instead of using cache-only mode.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the text table.",
    )
    return parser.parse_args()


def parse_periods(raw_periods: list[str] | None, end_date: date) -> list[tuple[str, date, date]]:
    if not raw_periods:
        raw_periods = ["1y:1", "3y:3", "5y:5"]

    periods: list[tuple[str, date, date]] = []
    for spec in raw_periods:
        label, years_str = spec.split(":", 1)
        years = int(years_str)
        start = date(end_date.year - years, end_date.month, 1)
        periods.append((label, start, end_date))
    return periods


def main() -> int:
    args = parse_args()
    end_date = date.fromisoformat(args.end_date)
    periods = parse_periods(args.periods, end_date)
    fetch_start = min(start for _, start, _ in periods) - timedelta(days=500)
    prices = load_prices(fetch_start, end_date, refresh_missing=args.refresh_missing)
    if prices.empty:
        raise SystemExit("No price data available. Re-run with --refresh-missing if needed.")

    payload: dict[str, list[dict]] = {}
    for label, start, end in periods:
        rows = [run_scenario(prices, start, end, scenario) for scenario in SCENARIOS]
        rows.sort(key=lambda row: row["cagr"], reverse=True)
        payload[label] = rows
        if not args.json:
            print_report(rows, f"{label} ({start} -> {end})")

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
