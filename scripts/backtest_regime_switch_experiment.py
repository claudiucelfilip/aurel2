#!/usr/bin/env python3
"""Experiment 1: regime-switched policy backtest runner.

Compares:
- current paper-trading baseline
- static aggressive recent-cycle policy
- static robust long-horizon policy
- two regime-switched policy variants

Backtest order follows the updated research protocol:
1. 15y
2. 10y
3. 1y
4. 1m
5. optional 20y stress/history check

Usage:
    python3 scripts/backtest_regime_switch_experiment.py
    python3 scripts/backtest_regime_switch_experiment.py --include-20y
    python3 scripts/backtest_regime_switch_experiment.py --output data/regime_switch_experiment.json
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
from aurel2.core.models import AssetClass
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy


structlog.configure(
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")),
)
logging.disable(logging.CRITICAL)


DEFAULT_END = date(2026, 4, 1)
FETCH_START = date(2003, 6, 1)

PRIMARY_PERIODS = [
    ("15y", date(2010, 1, 1), DEFAULT_END),
    ("10y", date(2015, 1, 1), DEFAULT_END),
    ("1y", date(2025, 4, 1), DEFAULT_END),
    ("1m", date(2026, 3, 1), DEFAULT_END),
]

SUPPLEMENTAL_PERIODS = [
    ("5y", date(2020, 1, 1), DEFAULT_END),
]

OPTIONAL_PERIODS = [
    ("20y", date(2005, 1, 1), DEFAULT_END),
]


AGGRESSIVE_ASSETS = {
    AssetClass.US_STOCKS: ASSET_REGISTRY[AssetClass.US_STOCKS],
    AssetClass.INTL_DEVELOPED: ASSET_REGISTRY[AssetClass.INTL_DEVELOPED],
    AssetClass.BONDS_AGGREGATE: ASSET_REGISTRY[AssetClass.BONDS_AGGREGATE],
    AssetClass.CASH: ASSET_REGISTRY[AssetClass.CASH],
}


@dataclass(frozen=True)
class PolicyConfig:
    """Dual-momentum policy definition for a specific regime or static mode."""

    name: str
    assets: dict[AssetClass, Any]
    switch_threshold: float
    cash_rate: float
    pilot_entry_enabled: bool


AGGRESSIVE_RECENT = PolicyConfig(
    name="AggressiveRecent",
    assets=AGGRESSIVE_ASSETS,
    switch_threshold=0.08,
    cash_rate=0.0,
    pilot_entry_enabled=False,
)

ROBUST_LONG_HORIZON = PolicyConfig(
    name="RobustLongHorizon",
    assets=ASSET_REGISTRY,
    switch_threshold=0.02,
    cash_rate=0.0,
    pilot_entry_enabled=False,
)

BASELINE_CONFIG = PolicyConfig(
    name="Baseline",
    assets=ASSET_REGISTRY,
    switch_threshold=0.10,
    cash_rate=0.04,
    pilot_entry_enabled=True,
)


class RegimeSwitchedDMStrategy(DualMomentumStrategy):
    """Switch dual-momentum policy by detected regime at each rebalance date."""

    def __init__(
        self,
        bull_policy: PolicyConfig,
        sideways_policy: PolicyConfig,
        bear_policy: PolicyConfig,
    ):
        self._policies = {
            "bull": bull_policy,
            "sideways": sideways_policy,
            "bear": bear_policy,
        }
        self._strategies = {
            regime: DualMomentumStrategy(
                assets=policy.assets,
                lookback_months=12,
                switch_threshold=policy.switch_threshold,
                cash_rate=policy.cash_rate,
                pilot_entry_enabled=policy.pilot_entry_enabled,
            )
            for regime, policy in self._policies.items()
        }

        super().__init__(
            assets=bull_policy.assets,
            lookback_months=12,
            switch_threshold=bull_policy.switch_threshold,
            cash_rate=bull_policy.cash_rate,
            pilot_entry_enabled=bull_policy.pilot_entry_enabled,
        )
        self.last_regime = "bull"

    def _detect_regime(self, prices: pd.DataFrame, calc_date: date) -> str:
        """Simple consensus regime detector using drawdown, SMA, and volatility."""
        spy = prices[prices["symbol"] == "SPY"].copy()
        if spy.empty:
            return "sideways"

        spy["date"] = pd.to_datetime(spy["date"])
        spy = spy[spy["date"] <= pd.Timestamp(calc_date)].sort_values("date")
        if len(spy) < 252:
            return "sideways"

        close = float(spy.iloc[-1]["close"])

        # Drawdown vote
        high_252 = float(spy.tail(252)["close"].max())
        drawdown = (high_252 - close) / high_252 if high_252 else 0.0
        if drawdown < 0.05:
            drawdown_regime = "bull"
        elif drawdown < 0.15:
            drawdown_regime = "sideways"
        else:
            drawdown_regime = "bear"

        # SMA vote
        sma_200 = float(spy.tail(200)["close"].mean()) if len(spy) >= 200 else close
        sma_prev = float(spy.tail(220).head(20)["close"].mean()) if len(spy) >= 220 else sma_200
        sma_slope = sma_200 - sma_prev
        if close > sma_200 and sma_slope >= 0:
            sma_regime = "bull"
        elif close < sma_200 and sma_slope <= 0:
            sma_regime = "bear"
        else:
            sma_regime = "sideways"

        # Volatility vote
        returns = spy["close"].pct_change().dropna()
        if len(returns) >= 252:
            vol_20 = float(returns.tail(20).std()) * (252 ** 0.5)
            rolling = returns.tail(252).rolling(20).std().dropna() * (252 ** 0.5)
            vol_pctl = float((rolling < vol_20).mean()) if not rolling.empty else 0.5
        else:
            vol_pctl = 0.5
        if vol_pctl < 0.30:
            vol_regime = "bull"
        elif vol_pctl > 0.70:
            vol_regime = "bear"
        else:
            vol_regime = "sideways"

        votes = [drawdown_regime, sma_regime, vol_regime]
        for regime in ("bull", "bear", "sideways"):
            if votes.count(regime) >= 2:
                return regime
        return "sideways"

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ):
        regime = self._detect_regime(prices, calc_date)
        self.last_regime = regime

        strategy = self._strategies[regime]
        strategy.current_holding = current_holding if current_holding is not None else self.current_holding
        strategy.is_pilot_position = self.is_pilot_position

        signal = strategy.generate_signal(
            prices=prices,
            calc_date=calc_date,
            current_holding=current_holding if current_holding is not None else self.current_holding,
        )

        self.current_holding = strategy.current_holding
        self.is_pilot_position = strategy.is_pilot_position
        self.assets = strategy.assets
        return signal


def build_engine(
    dm_policy: PolicyConfig | None = None,
    regime_map: dict[str, PolicyConfig] | None = None,
    correlation_guard: bool = False,
    sideways_hold: bool = False,
) -> BacktestEngine:
    """Create a backtest engine for a static or regime-switched candidate."""
    engine = BacktestEngine(
        initial_capital=10000.0,
        use_ai=False,
        correlation_guard=correlation_guard,
        sideways_hold=sideways_hold,
    )

    if regime_map is not None:
        engine.dual_momentum = RegimeSwitchedDMStrategy(
            bull_policy=regime_map["bull"],
            sideways_policy=regime_map["sideways"],
            bear_policy=regime_map["bear"],
        )
    elif dm_policy is not None:
        engine.dual_momentum = DualMomentumStrategy(
            assets=dm_policy.assets,
            lookback_months=12,
            switch_threshold=dm_policy.switch_threshold,
            cash_rate=dm_policy.cash_rate,
            pilot_entry_enabled=dm_policy.pilot_entry_enabled,
        )

    return engine


def run_config(
    name: str,
    prices: pd.DataFrame,
    start: date,
    end: date,
    dm_policy: PolicyConfig | None = None,
    regime_map: dict[str, PolicyConfig] | None = None,
    correlation_guard: bool = False,
    sideways_hold: bool = False,
) -> dict[str, Any]:
    """Run a single backtest configuration."""
    engine = build_engine(
        dm_policy=dm_policy,
        regime_map=regime_map,
        correlation_guard=correlation_guard,
        sideways_hold=sideways_hold,
    )
    result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
    result.calculate_metrics()

    years = (end - start).days / 365.25
    benchmark_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    benchmark_cagr = ((1.0 + benchmark_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    strategy_total = (result.final_value / 10000.0 - 1.0) * 100.0
    benchmark_total = benchmark_return * 100.0

    return {
        "name": name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cagr": result.cagr,
        "bench_cagr": benchmark_cagr,
        "alpha_cagr": result.cagr - benchmark_cagr,
        "strat_total": strategy_total,
        "bench_total": benchmark_total,
        "alpha_total": strategy_total - benchmark_total,
        "max_dd": result.max_drawdown,
        "sharpe": result.sharpe_ratio,
        "turnover": result.turnover,
        "trades": result.num_trades,
        "final_value": result.final_value,
    }


def candidate_definitions() -> list[tuple[str, dict[str, Any]]]:
    """Return candidate configurations for Experiment 1."""
    return [
        (
            "Baseline_CurrentPaper",
            {
                "dm_policy": BASELINE_CONFIG,
                "correlation_guard": True,
                "sideways_hold": True,
            },
        ),
        (
            "Static_AggressiveRecent",
            {
                "dm_policy": AGGRESSIVE_RECENT,
                "correlation_guard": False,
                "sideways_hold": False,
            },
        ),
        (
            "Static_RobustLongHorizon",
            {
                "dm_policy": ROBUST_LONG_HORIZON,
                "correlation_guard": False,
                "sideways_hold": False,
            },
        ),
        (
            "Regime_BullAgg_SideAgg_BearRobust",
            {
                "regime_map": {
                    "bull": AGGRESSIVE_RECENT,
                    "sideways": AGGRESSIVE_RECENT,
                    "bear": ROBUST_LONG_HORIZON,
                },
                "correlation_guard": False,
                "sideways_hold": False,
            },
        ),
        (
            "Regime_BullAgg_SideRobust_BearRobust",
            {
                "regime_map": {
                    "bull": AGGRESSIVE_RECENT,
                    "sideways": ROBUST_LONG_HORIZON,
                    "bear": ROBUST_LONG_HORIZON,
                },
                "correlation_guard": False,
                "sideways_hold": False,
            },
        ),
    ]


def evaluate_gates(results_by_name: dict[str, dict[str, dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Evaluate shared research gates for non-baseline candidates."""
    baseline = results_by_name["Baseline_CurrentPaper"]
    evaluations: dict[str, list[dict[str, Any]]] = {}

    for name, period_results in results_by_name.items():
        if name == "Baseline_CurrentPaper":
            continue

        gates = []
        p15 = period_results["15y"]
        b15 = baseline["15y"]
        p10 = period_results["10y"]
        p1y = period_results["1y"]
        b1y = baseline["1y"]
        p1m = period_results["1m"]
        b1m = baseline["1m"]

        gates.append({
            "gate": "15y alpha improves vs baseline",
            "passed": p15["alpha_total"] > b15["alpha_total"],
            "detail": f"{b15['alpha_total']:+.1f}% -> {p15['alpha_total']:+.1f}%",
        })
        gates.append({
            "gate": "10y alpha remains positive",
            "passed": p10["alpha_total"] > 0,
            "detail": f"{p10['alpha_total']:+.1f}%",
        })
        gates.append({
            "gate": "1y alpha regression <= 15pp",
            "passed": (b1y["alpha_total"] - p1y["alpha_total"]) <= 15.0,
            "detail": f"regression {b1y['alpha_total'] - p1y['alpha_total']:+.1f}pp",
        })
        gates.append({
            "gate": "1m alpha regression <= 10pp",
            "passed": (b1m["alpha_total"] - p1m["alpha_total"]) <= 10.0,
            "detail": f"regression {b1m['alpha_total'] - p1m['alpha_total']:+.1f}pp",
        })
        gates.append({
            "gate": "15y max drawdown worsening <= 3pp",
            "passed": (p15["max_dd"] - b15["max_dd"]) <= 0.03,
            "detail": f"worsening {(p15['max_dd'] - b15['max_dd']):+.1%}",
        })
        gates.append({
            "gate": "15y trade count increase <= 40%",
            "passed": b15["trades"] == 0 or ((p15["trades"] - b15["trades"]) / b15["trades"]) <= 0.40,
            "detail": (
                "baseline has 0 trades"
                if b15["trades"] == 0
                else f"change {((p15['trades'] - b15['trades']) / b15['trades']):+.1%}"
            ),
        })

        evaluations[name] = gates

    return evaluations


def print_table(console: Console, period: str, rows: list[dict[str, Any]]) -> None:
    """Print result table for a single period."""
    console.print(f"\n[bold]{period}[/bold]")
    console.print(
        f"{'Config':<34} {'CAGR':>8} {'SPY':>8} {'Alpha':>8} | {'TR Alpha':>9} {'MaxDD':>8} {'Sharpe':>7} {'Trd':>4}"
    )
    console.print("-" * 92)
    for row in rows:
        alpha_color = "green" if row["alpha_total"] > 0 else "red"
        console.print(
            f"{row['name']:<34} "
            f"{row['cagr']:+7.1%} "
            f"{row['bench_cagr']:+7.1%} "
            f"{row['alpha_cagr']:+7.1%} | "
            f"[{alpha_color}]{row['alpha_total']:+8.1f}%[/{alpha_color}] "
            f"{row['max_dd']:7.1%} "
            f"{row['sharpe']:7.2f} "
            f"{row['trades']:4d}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Experiment 1 regime-switched policy backtests.")
    parser.add_argument(
        "--include-5y",
        action="store_true",
        help="Include supplemental 5y window.",
    )
    parser.add_argument(
        "--include-20y",
        action="store_true",
        help="Include optional 20y GFC stress/history check.",
    )
    parser.add_argument(
        "--output",
        default="data/regime_switch_experiment.json",
        help="Where to write machine-readable results.",
    )
    args = parser.parse_args()

    console = Console()
    console.print("\n[bold cyan]EXPERIMENT 1: REGIME-SWITCHED POLICY[/bold cyan]")
    order_parts = ["15y", "10y"]
    if args.include_5y:
        order_parts.append("5y")
    order_parts.extend(["1y", "1m"])
    if args.include_20y:
        order_parts.append("optional 20y")
    console.print("Backtest order: " + " -> ".join(order_parts))

    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    console.print(f"\nFetching {len(symbols)} symbols from {FETCH_START}...")
    prices = provider.get_multi_prices(symbols, FETCH_START, DEFAULT_END + timedelta(days=5))
    console.print(f"Loaded {len(prices)} price rows")

    periods = list(PRIMARY_PERIODS)
    if args.include_5y:
        periods = periods[:2] + SUPPLEMENTAL_PERIODS + periods[2:]
    if args.include_20y:
        periods.extend(OPTIONAL_PERIODS)

    candidates = candidate_definitions()
    all_rows: list[dict[str, Any]] = []
    results_by_name: dict[str, dict[str, dict[str, Any]]] = {name: {} for name, _ in candidates}

    for period_label, start, end in periods:
        console.print(f"\n[bold yellow]Running {period_label} ({start} -> {end})[/bold yellow]")
        period_rows = []
        for name, kwargs in candidates:
            console.print(f"  [dim]Running {name}...[/dim]")
            row = run_config(name=name, prices=prices, start=start, end=end, **kwargs)
            row["period"] = period_label
            period_rows.append(row)
            all_rows.append(row)
            results_by_name[name][period_label] = row
        print_table(console, period_label, period_rows)

    evaluations = evaluate_gates(results_by_name)

    console.print("\n[bold cyan]Gate Evaluation[/bold cyan]")
    for name, gates in evaluations.items():
        console.print(f"\n[bold]{name}[/bold]")
        for gate in gates:
            status = "[green]PASS[/green]" if gate["passed"] else "[red]FAIL[/red]"
            console.print(f"  {status} {gate['gate']} ({gate['detail']})")

    payload = {
        "experiment": "regime_switch_policy",
        "generated_at": date.today().isoformat(),
        "period_order": [p[0] for p in periods],
        "candidates": [name for name, _ in candidates],
        "results": all_rows,
        "gate_evaluation": evaluations,
        "notes": {
            "20y": "Optional stress/history check including the GFC." if args.include_20y else "Not run.",
            "baseline": "Current paper-trading baseline approximation using current production-style parameters.",
        },
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2))
    console.print(f"\nSaved results to {output_path}")


if __name__ == "__main__":
    main()
