#!/usr/bin/env python3
"""Experiment: refine the orchestrator calm-market-hold release condition.

Background — see scripts/trace_orchestrator_decisions.py:
  Calm-market hold suppresses any DM-signaled switch when SPY drawdown is
  below 5% AND the held asset's 12m momentum is positive. The escape hatch
  fires only when held momentum goes negative — by which time the new
  winner is often a defensive/commodity asset that took the lead by sheer
  absolute momentum (e.g. GLD in 2019-Q3, missing the XLK rally entirely).

  The trace also showed that calm-hold blocks the GLD→XLK fix in 2019-Q4
  even though XLK had +49% vs GLD's +17%. So calm-hold isn't just slow on
  entry to defensive winners, it's also slow on exit.

This experiment tests release-condition refinements without touching DM:

  Baseline       production (release only when held momentum < 0)
  CalmOff        calm-hold disabled entirely
  Gap10          ALSO release when winner_mom > held_mom + 0.10
  Gap15          ALSO release when winner_mom > held_mom + 0.15
  Gap20          ALSO release when winner_mom > held_mom + 0.20
  TimeBound4     ALSO release after 4 consecutive calm-hold suppressions

The release condition is checked AT THE ORCHESTRATOR LEVEL, so DM's
unchanged ranker chooses the new asset when the release fires. This is
what the trace suggested would matter — the ranker is fine, the gate is
the issue.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aurel2.agent.orchestrator import AgentOrchestrator
from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.core.models import SignalAction
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

STANDARD_PERIODS = [
    ("20y", date(2005, 1, 1), END_DATE),
    ("15y", date(2010, 1, 1), END_DATE),
    ("10y", date(2015, 1, 1), END_DATE),
    ("5y", date(2020, 1, 1), END_DATE),
    ("1y", date(2025, 4, 1), END_DATE),
]

FOCUS_PERIODS = [
    ("2013_lag", date(2012, 1, 1), date(2014, 6, 1)),
    ("2019_lag", date(2018, 1, 1), date(2020, 6, 1)),
    ("2023_recovery", date(2022, 1, 1), date(2024, 6, 1)),
]


@dataclass(frozen=True)
class CalmVariant:
    name: str
    disable_calm: bool = False        # turn off entirely
    gap_release: float | None = None  # release when winner_mom - held_mom > X
    time_bound: int | None = None     # release after N consecutive calm-hold suppressions


VARIANTS: list[CalmVariant] = [
    CalmVariant("Baseline"),
    CalmVariant("CalmOff", disable_calm=True),
    CalmVariant("Gap10", gap_release=0.10),
    CalmVariant("Gap15", gap_release=0.15),
    CalmVariant("Gap20", gap_release=0.20),
    CalmVariant("TimeBound4", time_bound=4),
]


# ---------------------------------------------------------------------------
# Orchestrator monkey-patch
# ---------------------------------------------------------------------------

original_analyze = AgentOrchestrator.analyze
_consecutive_calm_holds: dict[int, int] = defaultdict(int)


def install_variant(v: CalmVariant) -> None:
    """Patch AgentOrchestrator.analyze to apply this variant's release rules."""
    if v.disable_calm:
        # Easiest path: set threshold to 0 so `drawdown < threshold` is always False.
        AgentOrchestrator.analyze = original_analyze
        AgentOrchestrator._aurel2_calm_threshold_override = 0.0  # type: ignore[attr-defined]
        return

    if v.gap_release is None and v.time_bound is None:
        # baseline
        AgentOrchestrator.analyze = original_analyze
        if hasattr(AgentOrchestrator, "_aurel2_calm_threshold_override"):
            del AgentOrchestrator._aurel2_calm_threshold_override  # type: ignore[attr-defined]
        return

    gap = v.gap_release
    bound = v.time_bound

    def patched_analyze(self, signals, market_context=None, current_holding=None):
        # Reset consecutive counter when we change strategy instance
        key = id(self)

        # We need to know whether this call WOULD trigger calm-hold under the
        # original logic, and then decide whether to release it.
        if market_context is None:
            market_context = {}

        dm = signals.get("dual_momentum", {})
        dm_action = dm.get("action", "hold")
        dm_asset = dm.get("asset_symbol")
        dm_momentum = dm.get("momentum_scores", {}) or {}

        held_mom = dm_momentum.get(current_holding) if current_holding else None
        winner_mom = dm_momentum.get(dm_asset) if dm_asset else None
        drawdown = market_context.get("drawdown", 0.0)

        would_be_calm_hold = (
            current_holding is not None
            and current_holding not in ("CASH", None)
            and dm_action == "buy"
            and dm_asset != current_holding
            and drawdown < self.calm_market_hold_threshold
            and (held_mom is None or held_mom >= 0)
        )

        release = False
        if would_be_calm_hold:
            if gap is not None and held_mom is not None and winner_mom is not None:
                if (winner_mom - held_mom) > gap:
                    release = True
            if bound is not None:
                _consecutive_calm_holds[key] += 1
                if _consecutive_calm_holds[key] > bound:
                    release = True
                    _consecutive_calm_holds[key] = 0
        else:
            _consecutive_calm_holds[key] = 0

        if release:
            # Temporarily neutralize calm-hold by setting drawdown above threshold.
            patched_ctx = dict(market_context)
            patched_ctx["drawdown"] = self.calm_market_hold_threshold + 0.001
            return original_analyze(self, signals, market_context=patched_ctx, current_holding=current_holding)

        return original_analyze(self, signals, market_context=market_context, current_holding=current_holding)

    AgentOrchestrator.analyze = patched_analyze
    if hasattr(AgentOrchestrator, "_aurel2_calm_threshold_override"):
        del AgentOrchestrator._aurel2_calm_threshold_override  # type: ignore[attr-defined]


def restore_orchestrator() -> None:
    AgentOrchestrator.analyze = original_analyze
    if hasattr(AgentOrchestrator, "_aurel2_calm_threshold_override"):
        del AgentOrchestrator._aurel2_calm_threshold_override  # type: ignore[attr-defined]


def make_engine(v: CalmVariant) -> BacktestEngine:
    engine = BacktestEngine(
        initial_capital=10000.0,
        use_ai=False,
        correlation_guard=False,
        sideways_hold=False,
    )
    engine.dual_momentum = build_robust_quarterly_no_tlt_strategy()
    if v.disable_calm:
        # Set the threshold to 0 on this engine's orchestrator so calm-hold
        # never fires regardless of drawdown.
        engine.orchestrator.calm_market_hold_threshold = 0.0
    return engine


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_one(prices, v: CalmVariant, start: date, end: date) -> dict[str, Any]:
    install_variant(v)
    try:
        engine = make_engine(v)
        result = engine.run(
            prices=prices,
            start_date=start,
            end_date=end,
            benchmark_symbol="SPY",
            frequency="quarterly",
        )
        result.calculate_metrics()
    finally:
        restore_orchestrator()

    years = (end - start).days / 365.25
    bench_return = ((result.benchmark_final / 10000.0) - 1.0) if result.benchmark_final else 0.0
    bench_cagr = ((1.0 + bench_return) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    return {
        "variant": v.name,
        "cagr": result.cagr,
        "bench_cagr": bench_cagr,
        "alpha": result.cagr - bench_cagr,
        "max_dd": result.max_drawdown,
        "trades": result.num_trades,
        "sharpe": result.sharpe_ratio,
    }


def print_window(console: Console, label: str, rows: list[dict[str, Any]]) -> None:
    console.print(f"\n[bold yellow]{label}[/bold yellow]")
    console.print(
        f"  {'Variant':<11} {'CAGR':>7} {'SPY':>7} {'Alpha':>7} {'MaxDD':>7} {'Trd':>4} {'Δ vs base':>11}"
    )
    baseline = next((r for r in rows if r["variant"] == "Baseline"), None)
    base_cagr = baseline["cagr"] if baseline else None
    for r in rows:
        marker = "*" if r["variant"] == "Baseline" else " "
        delta = ""
        if base_cagr is not None and r["variant"] != "Baseline":
            d = r["cagr"] - base_cagr
            delta = f"{d:+.1%}"
        console.print(
            f"  {marker}{r['variant']:<10} "
            f"{r['cagr']:+6.1%} "
            f"{r['bench_cagr']:+6.1%} "
            f"{r['alpha']:+6.1%} "
            f"{r['max_dd']:6.1%} "
            f"{r['trades']:4d} "
            f"{delta:>11}"
        )


def main() -> None:
    console = Console()
    console.print("\n[bold cyan]CALM-HOLD RELEASE EXPERIMENT[/bold cyan]")
    console.print("Robust_Quarterly_NO_TLT base. Variants change calm-hold release only.\n")

    provider = CachedPriceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")
    console.print(f"Loading {len(symbols)} symbols from {FETCH_START}…")
    prices = provider.get_multi_prices(symbols, FETCH_START, END_DATE + timedelta(days=5))
    console.print(f"Loaded {len(prices)} rows.\n")

    out: dict[str, Any] = {
        "_meta": {
            "generated_at": str(date.today()),
            "base": "Robust_Quarterly_NO_TLT",
            "variants": [v.name for v in VARIANTS],
            "rationale": (
                "Trace showed calm-hold blocks DM switches in calm windows. "
                "Test loosening the release condition without touching DM."
            ),
        },
        "standard": {},
        "focus": {},
    }

    for label, start, end in STANDARD_PERIODS:
        rows = [run_one(prices, v, start, end) for v in VARIANTS]
        out["standard"][label] = rows
        print_window(console, f"{label}  ({start} → {end})", rows)

    console.rule("[bold magenta]FOCUS WINDOWS")
    for label, start, end in FOCUS_PERIODS:
        rows = [run_one(prices, v, start, end) for v in VARIANTS]
        out["focus"][label] = rows
        print_window(console, f"{label}  ({start} → {end})", rows)

    out_path = Path("data/calm_hold_release_experiment.json")
    out_path.write_text(json.dumps(out, indent=2))
    console.print(f"\n[bold green]Saved[/bold green] {out_path}")


if __name__ == "__main__":
    main()
