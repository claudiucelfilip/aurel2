#!/usr/bin/env python3
"""Final validation: regime guardrails + transaction cost sensitivity.

Addresses Codex Round 3 concerns:
1. Regime-level underperformance guardrail (2015-2020 sub-period)
2. Transaction cost/slippage sensitivity (0.1%, 0.25%, 0.5%, 1.0%)
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.stdlib.BoundLogger, logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")))
logging.disable(logging.CRITICAL)

from rich.console import Console

from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy


def run_config(name, prices, start, end, dm_kwargs=None, engine_kwargs=None):
    dm_kwargs = dm_kwargs or {}
    engine_kwargs = engine_kwargs or {}
    engine = BacktestEngine(initial_capital=10000, use_ai=False, **engine_kwargs)
    if dm_kwargs:
        engine.dual_momentum = DualMomentumStrategy(**dm_kwargs)
    result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")

    years = (end - start).days / 365.25
    bench_return = ((result.benchmark_final / 10000) - 1) if result.benchmark_final else 0
    bench_cagr = ((1 + bench_return) ** (1 / years) - 1) if years > 0 else 0
    strat_total = (result.final_value / 10000 - 1) * 100
    bench_total = bench_return * 100
    return {
        "name": name,
        "cagr": result.cagr,
        "bench_cagr": bench_cagr,
        "alpha_cagr": result.cagr - bench_cagr,
        "strat_total": strat_total,
        "bench_total": bench_total,
        "alpha_total": strat_total - bench_total,
        "max_dd": result.max_drawdown,
        "sharpe": result.sharpe_ratio,
        "trades": result.num_trades,
        "final": result.final_value,
    }


def main():
    console = Console()
    console.print("\n[bold cyan]FINAL VALIDATION: REGIME GUARDRAILS + TRANSACTION COSTS[/bold cyan]")
    console.print("=" * 120)

    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    console.print(f"\nFetching {len(symbols)} symbols...")
    prices = provider.get_multi_prices(symbols, date(2003, 6, 1), date.today() + timedelta(days=5))
    console.print(f"Total: {len(prices)} price records\n")

    if prices.empty:
        console.print("[red]ERROR: No price data[/red]")
        return

    baseline_dm = {
        "assets": ASSET_REGISTRY, "switch_threshold": 0.10,
        "cash_rate": 0.04, "pilot_entry_enabled": True,
    }
    baseline_eng = {"correlation_guard": True, "sideways_hold": True}

    base_candidate = {"assets": ASSET_REGISTRY, "cash_rate": 0.0, "pilot_entry_enabled": False}
    no_guard = {"correlation_guard": False, "sideways_hold": False}

    # ================================================================
    # TEST 1: Regime-level sub-period analysis for all 3 candidates
    # ================================================================
    console.print("[bold yellow]TEST 1: REGIME-LEVEL SUB-PERIOD ANALYSIS[/bold yellow]")
    console.print("Max acceptable underperformance vs baseline in any single sub-period.\n")

    regimes = [
        ("2005-2010 Housing+GFC", date(2005, 1, 1), date(2010, 1, 1)),
        ("2010-2015 QE Recovery", date(2010, 1, 1), date(2015, 1, 1)),
        ("2015-2020 Late Bull", date(2015, 1, 1), date(2020, 1, 1)),
        ("2020-2026 COVID+AI", date(2020, 1, 1), date(2026, 2, 1)),
    ]

    configs = [
        ("BASELINE", baseline_dm, baseline_eng),
        ("thr=0.02", {**base_candidate, "switch_threshold": 0.02}, no_guard),
        ("thr=0.03", {**base_candidate, "switch_threshold": 0.03}, no_guard),
        ("thr=0.05", {**base_candidate, "switch_threshold": 0.05}, no_guard),
    ]

    regime_results = {}
    for regime_label, start, end in regimes:
        console.print(f"[bold]{regime_label}[/bold]")
        for cname, dm_kw, eng_kw in configs:
            console.print(f"  [dim]Running {cname}...[/dim]")
            try:
                r = run_config(cname, prices, start, end, dm_kw, eng_kw)
                regime_results[(cname, regime_label)] = r
            except Exception as e:
                regime_results[(cname, regime_label)] = {"error": str(e)}

    console.print(f"\n  {'Config':<12} {'Regime':<25} {'CAGR':>7} {'SPY':>7} {'Alpha':>7} | {'StratTR':>8} {'SPYTR':>8} {'TR Alpha':>9} | {'Trd':>4}")
    console.print(f"  {'-'*100}")

    for regime_label, _, _ in regimes:
        for cname, _, _ in configs:
            r = regime_results.get((cname, regime_label))
            if not r or "error" in r:
                console.print(f"  {cname:<12} {regime_label:<25} ERROR")
                continue
            alpha_color = "green" if r["alpha_total"] > 0 else "red"
            console.print(
                f"  {cname:<12} {regime_label:<25} "
                f"{r['cagr']:+6.1%} {r['bench_cagr']:+6.1%} {r['alpha_cagr']:+6.1%} | "
                f"{r['strat_total']:+7.0f}% {r['bench_total']:+7.0f}% [{alpha_color}]{r['alpha_total']:+8.0f}%[/{alpha_color}] | "
                f"{r['trades']:4d}"
            )
        console.print()

    # Regime guardrail check: max underperformance vs baseline in any sub-period
    console.print("\n[bold yellow]REGIME GUARDRAIL CHECK[/bold yellow]")
    console.print("Worst sub-period regression vs BASELINE (TR alpha delta):\n")

    for cname in ["thr=0.02", "thr=0.03", "thr=0.05"]:
        worst_regime = None
        worst_delta = float('inf')
        for regime_label, _, _ in regimes:
            b = regime_results.get(("BASELINE", regime_label))
            c = regime_results.get((cname, regime_label))
            if b and c and "error" not in b and "error" not in c:
                delta = c["alpha_total"] - b["alpha_total"]
                if delta < worst_delta:
                    worst_delta = delta
                    worst_regime = regime_label
        color = "green" if worst_delta >= -50 else "red"
        console.print(f"  {cname}: worst = [{color}]{worst_delta:+.0f}pp[/{color}] in {worst_regime}")

    # ================================================================
    # TEST 2: Transaction cost sensitivity
    # ================================================================
    console.print(f"\n\n[bold yellow]TEST 2: TRANSACTION COST SENSITIVITY[/bold yellow]")
    console.print("Testing if 0.02 vs 0.05 ranking is stable under higher costs.\n")

    cost_levels = [0.001, 0.0025, 0.005, 0.010]  # 0.1%, 0.25%, 0.5%, 1.0%
    cost_configs = [
        ("thr=0.02", {**base_candidate, "switch_threshold": 0.02}),
        ("thr=0.05", {**base_candidate, "switch_threshold": 0.05}),
    ]

    # Test on 20y (where the difference exists)
    test_periods = [
        ("20y", date(2005, 1, 1), date(2026, 2, 1)),
        ("15y", date(2010, 1, 1), date(2026, 2, 1)),
    ]

    for period_label, start, end in test_periods:
        console.print(f"[bold]{period_label} ({start} → {end})[/bold]")
        console.print(f"  {'Config':<12} {'Cost':>6} {'CAGR':>7} {'Alpha':>7} | {'StratTR':>8} {'TR Alpha':>9} | {'Trd':>4}")
        console.print(f"  {'-'*70}")

        for cost in cost_levels:
            for cname, dm_kw in cost_configs:
                eng_kw = {**no_guard, "transaction_cost_pct": cost}
                console.print(f"  [dim]Running {cname} @ {cost:.1%} cost...[/dim]", end="\r")
                try:
                    r = run_config(cname, prices, start, end, dm_kw, eng_kw)
                    alpha_color = "green" if r["alpha_total"] > 0 else "red"
                    console.print(
                        f"  {cname:<12} {cost:>5.1%} "
                        f"{r['cagr']:+6.1%} {r['alpha_cagr']:+6.1%} | "
                        f"{r['strat_total']:+7.0f}% [{alpha_color}]{r['alpha_total']:+8.0f}%[/{alpha_color}] | "
                        f"{r['trades']:4d}"
                    )
                except Exception as e:
                    console.print(f"  {cname:<12} {cost:>5.1%}  ERROR: {e}")
            console.print()

    # ================================================================
    # TEST 3: Extreme stress test - 1% cost on 2005-2010 GFC only
    # ================================================================
    console.print(f"\n[bold yellow]TEST 3: GFC STRESS TEST (2005-2010) WITH HIGH COSTS[/bold yellow]")
    console.print("This is where extra rotations fire. Does 0.02 still win at 1% cost?\n")

    start, end = date(2005, 1, 1), date(2010, 1, 1)
    console.print(f"  {'Config':<12} {'Cost':>6} {'CAGR':>7} {'Alpha':>7} | {'StratTR':>8} {'TR Alpha':>9} | {'Trd':>4}")
    console.print(f"  {'-'*70}")

    for cost in cost_levels:
        for cname, dm_kw in cost_configs:
            eng_kw = {**no_guard, "transaction_cost_pct": cost}
            try:
                r = run_config(cname, prices, start, end, dm_kw, eng_kw)
                alpha_color = "green" if r["alpha_total"] > 0 else "red"
                console.print(
                    f"  {cname:<12} {cost:>5.1%} "
                    f"{r['cagr']:+6.1%} {r['alpha_cagr']:+6.1%} | "
                    f"{r['strat_total']:+7.0f}% [{alpha_color}]{r['alpha_total']:+8.0f}%[/{alpha_color}] | "
                    f"{r['trades']:4d}"
                )
            except Exception as e:
                console.print(f"  {cname:<12} {cost:>5.1%}  ERROR: {e}")
        console.print()

    console.print(f"\n[bold cyan]{'='*120}[/bold cyan]")
    console.print("[bold cyan]SUMMARY[/bold cyan]")
    console.print("If thr=0.02 still beats thr=0.05 at 0.5-1% transaction costs,")
    console.print("the extra rotations are robust to real-world execution costs.")
    console.print(f"[bold cyan]{'='*120}[/bold cyan]")


if __name__ == "__main__":
    main()
