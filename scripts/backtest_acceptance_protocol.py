#!/usr/bin/env python3
"""Acceptance protocol: test candidate params against objective gates.

Gates (from Codex review):
1. Primary: improve 15y total-return alpha vs current production baseline.
2. Guardrails:
   - 20y alpha must not regress more than 25pp
   - 10y alpha must remain positive
   - 5y, 1y, 3m alpha must not regress more than 15pp each
3. Risk controls:
   - Max drawdown cannot worsen by more than 3pp on 20y or 15y
   - Trade count increase capped at +40% vs baseline on 20y
4. Choose final params only from candidates that pass all gates.
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
    console.print("\n[bold cyan]ACCEPTANCE PROTOCOL: CANDIDATE VALIDATION[/bold cyan]")
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

    # Periods to test
    today = date(2026, 2, 1)
    periods = [
        ("20y",  date(2005, 1, 1),  today),
        ("15y",  date(2010, 1, 1),  today),
        ("10y",  date(2015, 1, 1),  today),
        ("5y",   date(2020, 1, 1),  today),
        ("1y",   date(2025, 2, 1),  today),
        ("3m",   date(2025, 11, 1), today),
    ]

    # Baseline: current production
    baseline_dm = {
        "assets": ASSET_REGISTRY, "switch_threshold": 0.10,
        "cash_rate": 0.04, "pilot_entry_enabled": True,
    }
    baseline_eng = {"correlation_guard": True, "sideways_hold": True}

    # Candidates: all use 0% cash, no pilot, no orchestrator overrides
    base_candidate = {"assets": ASSET_REGISTRY, "cash_rate": 0.0, "pilot_entry_enabled": False}
    no_guard = {"correlation_guard": False, "sideways_hold": False}

    candidates = [
        ("thr=0.02", {**base_candidate, "switch_threshold": 0.02}, no_guard),
        ("thr=0.03", {**base_candidate, "switch_threshold": 0.03}, no_guard),
        ("thr=0.05", {**base_candidate, "switch_threshold": 0.05}, no_guard),
    ]

    # Run all backtests
    all_results = {}  # {(config_name, period_label): result}

    for period_label, start, end in periods:
        console.print(f"\n[bold yellow]PERIOD: {period_label} ({start} → {end})[/bold yellow]")

        console.print(f"  [dim]Running BASELINE...[/dim]")
        try:
            r = run_config("BASELINE", prices, start, end, baseline_dm, baseline_eng)
            all_results[("BASELINE", period_label)] = r
        except Exception as e:
            console.print(f"  [red]BASELINE FAILED: {e}[/red]")
            all_results[("BASELINE", period_label)] = {"error": str(e)}

        for name, dm_kw, eng_kw in candidates:
            console.print(f"  [dim]Running {name}...[/dim]")
            try:
                r = run_config(name, prices, start, end, dm_kw, eng_kw)
                all_results[(name, period_label)] = r
            except Exception as e:
                console.print(f"  [red]{name} FAILED: {e}[/red]")
                all_results[(name, period_label)] = {"error": str(e)}

    # ================================================================
    # RESULTS TABLE
    # ================================================================
    console.print(f"\n\n[bold cyan]{'='*120}[/bold cyan]")
    console.print("[bold cyan]FULL RESULTS[/bold cyan]")
    console.print(f"[bold cyan]{'='*120}[/bold cyan]")

    console.print(f"\n  {'Config':<12} {'Period':>6} {'CAGR':>7} {'SPY':>7} {'Alpha':>7} | {'StratTR':>8} {'SPYTR':>8} {'TR Alpha':>9} | {'MaxDD':>7} {'Sharpe':>7} {'Trd':>4}")
    console.print(f"  {'-'*100}")

    for period_label, _, _ in periods:
        for config_name in ["BASELINE"] + [c[0] for c in candidates]:
            key = (config_name, period_label)
            r = all_results.get(key)
            if not r or "error" in r:
                console.print(f"  {config_name:<12} {period_label:>6}  ERROR")
                continue
            alpha_color = "green" if r["alpha_total"] > 0 else "red"
            console.print(
                f"  {config_name:<12} {period_label:>6} "
                f"{r['cagr']:+6.1%} {r['bench_cagr']:+6.1%} {r['alpha_cagr']:+6.1%} | "
                f"{r['strat_total']:+7.0f}% {r['bench_total']:+7.0f}% [{alpha_color}]{r['alpha_total']:+8.0f}%[/{alpha_color}] | "
                f"{r['max_dd']:6.1%} {r['sharpe']:7.2f} {r['trades']:4d}"
            )
        console.print()

    # ================================================================
    # ACCEPTANCE GATE EVALUATION
    # ================================================================
    console.print(f"\n[bold cyan]{'='*120}[/bold cyan]")
    console.print("[bold cyan]ACCEPTANCE GATE EVALUATION[/bold cyan]")
    console.print(f"[bold cyan]{'='*120}[/bold cyan]\n")

    for name, _, _ in candidates:
        console.print(f"[bold yellow]Candidate: {name}[/bold yellow]")
        passes_all = True
        gate_results = []

        def get(config, period):
            return all_results.get((config, period))

        def check_gate(desc, passed, detail=""):
            nonlocal passes_all
            status = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
            if not passed:
                passes_all = False
            gate_results.append((desc, passed, detail))
            console.print(f"  {status} {desc} {detail}")

        # Gate 1: 15y TR alpha must improve
        b15 = get("BASELINE", "15y")
        c15 = get(name, "15y")
        if b15 and c15 and "error" not in b15 and "error" not in c15:
            improved = c15["alpha_total"] > b15["alpha_total"]
            check_gate(
                "15y TR alpha improves",
                improved,
                f"(baseline {b15['alpha_total']:+.0f}% → candidate {c15['alpha_total']:+.0f}%)"
            )
        else:
            check_gate("15y TR alpha improves", False, "(data missing)")

        # Gate 2a: 20y alpha must not regress >25pp
        b20 = get("BASELINE", "20y")
        c20 = get(name, "20y")
        if b20 and c20 and "error" not in b20 and "error" not in c20:
            regression = b20["alpha_total"] - c20["alpha_total"]
            check_gate(
                "20y alpha regression <= 25pp",
                regression <= 25,
                f"(baseline {b20['alpha_total']:+.0f}% → candidate {c20['alpha_total']:+.0f}%, regression {regression:+.0f}pp)"
            )
        else:
            check_gate("20y alpha regression <= 25pp", False, "(data missing)")

        # Gate 2b: 10y alpha must remain positive
        c10 = get(name, "10y")
        if c10 and "error" not in c10:
            check_gate(
                "10y alpha remains positive",
                c10["alpha_total"] > 0,
                f"(candidate {c10['alpha_total']:+.0f}%)"
            )
        else:
            check_gate("10y alpha remains positive", False, "(data missing)")

        # Gate 2c: 5y alpha must not regress >15pp
        b5 = get("BASELINE", "5y")
        c5 = get(name, "5y")
        if b5 and c5 and "error" not in b5 and "error" not in c5:
            regression = b5["alpha_total"] - c5["alpha_total"]
            check_gate(
                "5y alpha regression <= 15pp",
                regression <= 15,
                f"(baseline {b5['alpha_total']:+.0f}% → candidate {c5['alpha_total']:+.0f}%, regression {regression:+.0f}pp)"
            )
        else:
            check_gate("5y alpha regression <= 15pp", False, "(data missing)")

        # Gate 2d: 1y alpha must not regress >15pp
        b1 = get("BASELINE", "1y")
        c1 = get(name, "1y")
        if b1 and c1 and "error" not in b1 and "error" not in c1:
            regression = b1["alpha_total"] - c1["alpha_total"]
            check_gate(
                "1y alpha regression <= 15pp",
                regression <= 15,
                f"(baseline {b1['alpha_total']:+.0f}% → candidate {c1['alpha_total']:+.0f}%, regression {regression:+.0f}pp)"
            )
        else:
            check_gate("1y alpha regression <= 15pp", False, "(data missing)")

        # Gate 2e: 3m alpha must not regress >15pp
        b3 = get("BASELINE", "3m")
        c3 = get(name, "3m")
        if b3 and c3 and "error" not in b3 and "error" not in c3:
            regression = b3["alpha_total"] - c3["alpha_total"]
            check_gate(
                "3m alpha regression <= 15pp",
                regression <= 15,
                f"(baseline {b3['alpha_total']:+.0f}% → candidate {c3['alpha_total']:+.0f}%, regression {regression:+.0f}pp)"
            )
        else:
            check_gate("3m alpha regression <= 15pp", False, "(data missing)")

        # Gate 3a: 20y MaxDD must not worsen >3pp
        if b20 and c20 and "error" not in b20 and "error" not in c20:
            dd_worsening = c20["max_dd"] - b20["max_dd"]
            check_gate(
                "20y MaxDD worsening <= 3pp",
                dd_worsening <= 0.03,
                f"(baseline {b20['max_dd']:.1%} → candidate {c20['max_dd']:.1%}, delta {dd_worsening:+.1%})"
            )
        else:
            check_gate("20y MaxDD worsening <= 3pp", False, "(data missing)")

        # Gate 3b: 15y MaxDD must not worsen >3pp
        if b15 and c15 and "error" not in b15 and "error" not in c15:
            dd_worsening = c15["max_dd"] - b15["max_dd"]
            check_gate(
                "15y MaxDD worsening <= 3pp",
                dd_worsening <= 0.03,
                f"(baseline {b15['max_dd']:.1%} → candidate {c15['max_dd']:.1%}, delta {dd_worsening:+.1%})"
            )
        else:
            check_gate("15y MaxDD worsening <= 3pp", False, "(data missing)")

        # Gate 3c: 20y trade count increase <= 40%
        if b20 and c20 and "error" not in b20 and "error" not in c20:
            trade_increase = (c20["trades"] - b20["trades"]) / max(b20["trades"], 1)
            check_gate(
                "20y trade count increase <= 40%",
                trade_increase <= 0.40,
                f"(baseline {b20['trades']} → candidate {c20['trades']}, increase {trade_increase:+.0%})"
            )
        else:
            check_gate("20y trade count increase <= 40%", False, "(data missing)")

        verdict = "[bold green]ALL GATES PASSED[/bold green]" if passes_all else "[bold red]FAILED[/bold red]"
        console.print(f"\n  {verdict}\n")

    # ================================================================
    # FINAL RECOMMENDATION
    # ================================================================
    console.print(f"\n[bold cyan]{'='*120}[/bold cyan]")
    console.print("[bold cyan]FINAL RANKING (passing candidates by 15y alpha)[/bold cyan]")
    console.print(f"[bold cyan]{'='*120}[/bold cyan]\n")

    passing = []
    for cname, _, _ in candidates:
        # Check if all gates pass (simplified re-check)
        c15 = get(cname, "15y")
        b15 = get("BASELINE", "15y")
        c20 = get(cname, "20y")
        b20 = get("BASELINE", "20y")
        c10 = get(cname, "10y")
        c5 = get(cname, "5y")
        b5 = get("BASELINE", "5y")
        c1 = get(cname, "1y")
        b1 = get("BASELINE", "1y")
        c3 = get(cname, "3m")
        b3 = get("BASELINE", "3m")

        all_ok = True
        checks = []
        # Gate 1
        if not (c15 and b15 and "error" not in c15 and "error" not in b15 and c15["alpha_total"] > b15["alpha_total"]):
            all_ok = False
        # Gate 2a
        if not (c20 and b20 and "error" not in c20 and "error" not in b20 and (b20["alpha_total"] - c20["alpha_total"]) <= 25):
            all_ok = False
        # Gate 2b
        if not (c10 and "error" not in c10 and c10["alpha_total"] > 0):
            all_ok = False
        # Gate 2c
        if not (c5 and b5 and "error" not in c5 and "error" not in b5 and (b5["alpha_total"] - c5["alpha_total"]) <= 15):
            all_ok = False
        # Gate 2d
        if not (c1 and b1 and "error" not in c1 and "error" not in b1 and (b1["alpha_total"] - c1["alpha_total"]) <= 15):
            all_ok = False
        # Gate 2e
        if not (c3 and b3 and "error" not in c3 and "error" not in b3 and (b3["alpha_total"] - c3["alpha_total"]) <= 15):
            all_ok = False
        # Gate 3a
        if not (c20 and b20 and "error" not in c20 and "error" not in b20 and (c20["max_dd"] - b20["max_dd"]) <= 0.03):
            all_ok = False
        # Gate 3b
        if not (c15 and b15 and "error" not in c15 and "error" not in b15 and (c15["max_dd"] - b15["max_dd"]) <= 0.03):
            all_ok = False
        # Gate 3c
        if not (c20 and b20 and "error" not in c20 and "error" not in b20 and ((c20["trades"] - b20["trades"]) / max(b20["trades"], 1)) <= 0.40):
            all_ok = False

        if all_ok and c15 and "error" not in c15:
            passing.append((cname, c15["alpha_total"], c20["alpha_total"] if c20 and "error" not in c20 else None))

    if passing:
        passing.sort(key=lambda x: x[1], reverse=True)
        for i, (cname, alpha15, alpha20) in enumerate(passing):
            marker = " ← RECOMMENDED" if i == 0 else ""
            console.print(f"  #{i+1} {cname}: 15y alpha {alpha15:+.0f}%, 20y alpha {alpha20:+.0f}%{marker}")
    else:
        console.print("  [red]No candidates passed all gates.[/red]")


if __name__ == "__main__":
    main()
