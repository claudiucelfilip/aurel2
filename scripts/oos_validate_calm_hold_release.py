#!/usr/bin/env python3
"""Out-of-sample validation for the calm-hold gap-release rule.

Methodology:
  IN-SAMPLE  (IS):  2005-01-01 → 2015-01-01   (10y)
  OUT-OF-SAMPLE:    2015-01-01 → 2026-04-01   (~11y)

Procedure:
  1. Sweep gap thresholds [None, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
     on the IS window. Record each variant's CAGR / alpha vs SPY.
  2. Pick the IS winner by alpha vs SPY.
  3. Run that single chosen threshold on the OOS window.
  4. Compare OOS performance to baseline (no release rule).

  Decision rule:
  - If the chosen threshold improves OOS CAGR above baseline by a meaningful
    margin AND no window degrades materially, the rule is robust.
  - If the chosen threshold underperforms baseline OOS, the rule was overfit.
  - If a DIFFERENT threshold would have won OOS, the hyperparameter is
    unstable and we can't trust the rule.

Caveat: I have already extensively inspected 2015-2026 in this session
(diagnostic dump, calm-hold trace, calendar 2019 trace). So this OOS is
partially contaminated. The test is still useful — if Gap20 fails this
weakened OOS check, it would certainly fail a stricter one.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import structlog
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aurel2.agent.orchestrator import AgentOrchestrator
from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.cache import CachedPriceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.robust_quarterly import build_robust_quarterly_no_tlt_strategy


structlog.configure(
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")),
)
logging.disable(logging.CRITICAL)


END_DATE = date(2026, 4, 1)
FETCH_START = date(2003, 6, 1)

IS_START = date(2005, 1, 1)
IS_END = date(2015, 1, 1)
OOS_START = date(2015, 1, 1)
OOS_END = END_DATE

GAP_CANDIDATES: list[float | None] = [None, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]


original_analyze = AgentOrchestrator.analyze


def install_gap_release(gap: float | None) -> None:
    if gap is None:
        AgentOrchestrator.analyze = original_analyze
        return

    g = gap

    def patched(self, signals, market_context=None, current_holding=None):
        if market_context is None:
            market_context = {}
        dm = signals.get("dual_momentum", {})
        dm_action = dm.get("action", "hold")
        dm_asset = dm.get("asset_symbol")
        mom = dm.get("momentum_scores", {}) or {}
        held_mom = mom.get(current_holding) if current_holding else None
        winner_mom = mom.get(dm_asset) if dm_asset else None
        drawdown = market_context.get("drawdown", 0.0)

        would_be_calm_hold = (
            current_holding is not None
            and current_holding not in ("CASH", None)
            and dm_action == "buy"
            and dm_asset != current_holding
            and drawdown < self.calm_market_hold_threshold
            and (held_mom is None or held_mom >= 0)
        )
        if (
            would_be_calm_hold
            and held_mom is not None
            and winner_mom is not None
            and (winner_mom - held_mom) > g
        ):
            patched_ctx = dict(market_context)
            patched_ctx["drawdown"] = self.calm_market_hold_threshold + 0.001
            return original_analyze(self, signals, market_context=patched_ctx, current_holding=current_holding)

        return original_analyze(self, signals, market_context=market_context, current_holding=current_holding)

    AgentOrchestrator.analyze = patched


def restore() -> None:
    AgentOrchestrator.analyze = original_analyze


def run(prices, gap: float | None, start: date, end: date) -> dict[str, Any]:
    install_gap_release(gap)
    try:
        engine = BacktestEngine(
            initial_capital=10000.0,
            use_ai=False,
            correlation_guard=False,
            sideways_hold=False,
        )
        engine.dual_momentum = build_robust_quarterly_no_tlt_strategy()
        result = engine.run(
            prices=prices,
            start_date=start,
            end_date=end,
            benchmark_symbol="SPY",
            frequency="quarterly",
        )
        result.calculate_metrics()
    finally:
        restore()
    years = (end - start).days / 365.25
    bench_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    bench_cagr = ((1.0 + bench_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    return {
        "gap": gap,
        "cagr": result.cagr,
        "bench_cagr": bench_cagr,
        "alpha": result.cagr - bench_cagr,
        "max_dd": result.max_drawdown,
        "trades": result.num_trades,
    }


def label(gap: float | None) -> str:
    return "Baseline" if gap is None else f"Gap{int(gap*100)}"


def print_table(console: Console, title: str, rows: list[dict[str, Any]]) -> None:
    console.print(f"\n[bold yellow]{title}[/bold yellow]")
    console.print(f"  {'Variant':<10} {'CAGR':>7} {'SPY':>7} {'Alpha':>7} {'MaxDD':>7} {'Trd':>4}")
    base_alpha = next((r["alpha"] for r in rows if r["gap"] is None), None)
    for r in rows:
        marker = "*" if r["gap"] is None else " "
        delta = ""
        if base_alpha is not None and r["gap"] is not None:
            delta = f"  ({r['alpha']-base_alpha:+.1%} vs base)"
        console.print(
            f"  {marker}{label(r['gap']):<9} "
            f"{r['cagr']:+6.1%} "
            f"{r['bench_cagr']:+6.1%} "
            f"{r['alpha']:+6.1%} "
            f"{r['max_dd']:6.1%} "
            f"{r['trades']:4d}"
            f"{delta}"
        )


def main() -> None:
    console = Console()
    console.print("\n[bold cyan]OOS VALIDATION — calm-hold gap release[/bold cyan]")
    console.print(f"  IN-SAMPLE  : {IS_START} → {IS_END}")
    console.print(f"  OUT-OF-SAMPLE: {OOS_START} → {OOS_END}")
    console.print("  Caveat: 2015-2026 has been inspected this session, so OOS is partially contaminated.\n")

    provider = CachedPriceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")
    console.print(f"Loading {len(symbols)} symbols from {FETCH_START}…")
    prices = provider.get_multi_prices(symbols, FETCH_START, END_DATE + timedelta(days=5))
    console.print(f"Loaded {len(prices)} rows.\n")

    # IS sweep
    console.rule("[bold cyan]IN-SAMPLE SWEEP")
    is_results = [run(prices, g, IS_START, IS_END) for g in GAP_CANDIDATES]
    print_table(console, f"IS  ({IS_START} → {IS_END})", is_results)

    # Pick the IS winner by alpha
    is_winner = max(is_results, key=lambda r: r["alpha"])
    console.print(f"\n[bold green]IS winner by alpha:[/bold green] {label(is_winner['gap'])} "
                  f"(alpha {is_winner['alpha']:+.1%})")

    # OOS sweep — also test all candidates so we can see if a DIFFERENT threshold would have won
    console.rule("[bold cyan]OUT-OF-SAMPLE SWEEP (for inspection only — winner is chosen from IS)")
    oos_results = [run(prices, g, OOS_START, OOS_END) for g in GAP_CANDIDATES]
    print_table(console, f"OOS ({OOS_START} → {OOS_END})", oos_results)

    # Critical test: how does the IS-chosen threshold perform OOS?
    chosen_oos = next(r for r in oos_results if r["gap"] == is_winner["gap"])
    baseline_oos = next(r for r in oos_results if r["gap"] is None)
    chosen_delta = chosen_oos["cagr"] - baseline_oos["cagr"]

    console.rule("[bold magenta]VERDICT")
    console.print(f"  IS-chosen rule:        {label(is_winner['gap'])}")
    console.print(f"  IS  alpha vs baseline: {is_winner['alpha'] - is_results[0]['alpha']:+.1%}")
    console.print(f"  OOS CAGR (chosen):     {chosen_oos['cagr']:+.1%}")
    console.print(f"  OOS CAGR (baseline):   {baseline_oos['cagr']:+.1%}")
    console.print(f"  OOS Δ (chosen vs base):{chosen_delta:+.1%}")

    # Stability check: did a DIFFERENT threshold win OOS?
    oos_winner = max(oos_results, key=lambda r: r["alpha"])
    if oos_winner["gap"] != is_winner["gap"]:
        console.print(
            f"\n  [yellow]⚠ Hyperparameter unstable:[/yellow] OOS winner is "
            f"{label(oos_winner['gap'])} (alpha {oos_winner['alpha']:+.1%}), "
            f"not the IS winner {label(is_winner['gap'])}"
        )
    else:
        console.print(f"\n  [green]✓ Hyperparameter stable:[/green] OOS winner == IS winner")

    if chosen_delta > 0.005:
        console.print(f"\n  [bold green]ROBUST:[/bold green] IS-chosen rule beats baseline OOS by {chosen_delta:+.1%}")
    elif chosen_delta > -0.005:
        console.print(f"\n  [bold yellow]NEUTRAL:[/bold yellow] IS-chosen rule is within noise of baseline OOS")
    else:
        console.print(f"\n  [bold red]OVERFIT:[/bold red] IS-chosen rule underperforms baseline OOS by {chosen_delta:+.1%}")

    out_path = Path("data/oos_calm_hold_release.json")
    out_path.write_text(json.dumps({
        "_meta": {
            "is_start": str(IS_START), "is_end": str(IS_END),
            "oos_start": str(OOS_START), "oos_end": str(OOS_END),
            "is_winner_gap": is_winner["gap"],
            "chosen_oos_delta": chosen_delta,
        },
        "is": is_results,
        "oos": oos_results,
    }, indent=2))
    console.print(f"\n[bold green]Saved[/bold green] {out_path}")


if __name__ == "__main__":
    main()
