#!/usr/bin/env python3
"""Diagnostic: dump quarter-end momentum rankings during the failing windows.

Goal: see EXACTLY what the 12m ranker was looking at during the periods where
Robust_Quarterly_NO_TLT got stuck in stale defensive winners (2013, 2019, 2023).

This is observation only — no overrides, no proposed fixes. We're trying to
distinguish:
  (a) "Defensive really did have higher 12m momentum" → ranker is doing what
      it's told; the issue is the 12m signal itself.
  (b) "Offensive was close behind but blocked by the 15% equity→defensive
      threshold or the 5% defensive→equity threshold" → the asymmetric
      threshold is the culprit, not the 12m signal.
  (c) "Offensive winners switched too often inside the equity bucket and the
      held one happened to be defensive-flavored (XLV)" → tie-break or
      same-category churn issue.

For each quarter end in the focus windows, prints:
  - which asset RobustQuarterlyStrategy actually picked (replayed quarter by
    quarter so we get the correct held state)
  - the full ranked momentum table
  - the held asset's momentum vs the top equity contender
  - what the threshold check would have looked like
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import structlog
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import AssetCategory, AssetClass
from aurel2.data.momentum import calculate_momentum_scores
from aurel2.data.providers.cache import CachedPriceProvider
from aurel2.strategies.robust_quarterly import build_robust_quarterly_no_tlt_strategy


structlog.configure(
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")),
)
logging.disable(logging.CRITICAL)


END_DATE = date(2026, 4, 1)
FETCH_START = date(2003, 6, 1)

# Focus windows: the years where Robust_Quarterly_NO_TLT lagged SPY badly
# according to the handoff doc.
FOCUS_WINDOWS = [
    ("2013_lag", date(2012, 6, 1), date(2014, 6, 1)),
    ("2019_lag", date(2018, 6, 1), date(2020, 6, 1)),
    ("2023_recovery", date(2022, 6, 1), date(2024, 6, 1)),
]


def quarter_end_dates(prices: pd.DataFrame, start: date, end: date) -> list[date]:
    """Return the last available SPY trading date of each quarter-end month in window."""
    spy = prices[prices["symbol"] == "SPY"].copy()
    spy["date"] = pd.to_datetime(spy["date"])
    spy = spy[(spy["date"] >= pd.Timestamp(start)) & (spy["date"] <= pd.Timestamp(end))]
    out: list[date] = []
    for (year, month), grp in spy.groupby([spy["date"].dt.year, spy["date"].dt.month]):
        if month not in {3, 6, 9, 12}:
            continue
        out.append(grp["date"].max().date())
    return sorted(out)


def replay_quarterly_holdings(prices: pd.DataFrame, dates: list[date]) -> dict[date, AssetClass]:
    """Replay the production strategy across the given quarter-end dates and
    record what it held going INTO each quarter-end check.

    We do this by feeding the strategy the same dates in order; its internal
    state tracks the holding.
    """
    strategy = build_robust_quarterly_no_tlt_strategy()
    held_at: dict[date, AssetClass] = {}
    for d in dates:
        # Pre-call holding (what we held coming in)
        held_at[d] = strategy.current_holding or AssetClass.CASH
        strategy.generate_signal(prices=prices, calc_date=d, current_holding=strategy.current_holding)
    return held_at


def warmup_holdings(prices: pd.DataFrame, end_dates: list[date], window_start: date) -> dict[date, AssetClass]:
    """Replay from a much earlier date so the strategy has a realistic holding
    state by the time we hit the focus window. Otherwise we'd start from CASH
    every window.
    """
    # Get every quarter end from 2005 up through window
    full_dates = quarter_end_dates(prices, date(2005, 1, 1), end_dates[-1])
    strategy = build_robust_quarterly_no_tlt_strategy()
    snapshots: dict[date, AssetClass] = {}
    for d in full_dates:
        snapshots[d] = strategy.current_holding or AssetClass.CASH
        strategy.generate_signal(prices=prices, calc_date=d, current_holding=strategy.current_holding)
    # Filter to only the dates we care about
    return {d: snapshots[d] for d in end_dates if d in snapshots}


def build_ranking_table(scores: dict[AssetClass, object], held: AssetClass) -> Table:
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Rank", justify="right", style="dim")
    table.add_column("Asset")
    table.add_column("Symbol", style="dim")
    table.add_column("Category", style="dim")
    table.add_column("12m mom", justify="right")
    table.add_column("vs held", justify="right")

    held_score = scores.get(held)
    held_mom = held_score.momentum_12m if held_score else 0.0

    sorted_items = sorted(scores.items(), key=lambda x: x[1].momentum_12m, reverse=True)
    for i, (cls, score) in enumerate(sorted_items, start=1):
        is_held = cls == held
        marker = "*" if is_held else ""
        diff = score.momentum_12m - held_mom
        diff_str = f"{diff:+.2%}" if not is_held else "—"
        cat = score.asset.category.value if score.asset and score.asset.category else "?"
        name = f"{marker}{cls.value}"
        table.add_row(
            str(i),
            name,
            score.asset.symbol if score.asset else "?",
            cat,
            f"{score.momentum_12m:+.2%}",
            diff_str,
        )
    return table


def threshold_diagnosis(
    held: AssetClass,
    held_score,
    winner_class: AssetClass,
    winner_score,
    eq_to_def: float = 0.15,
    def_to_eq: float = 0.05,
    same_cat: float = 0.02,
) -> str:
    if held_score is None or winner_score is None:
        return "(missing score data)"
    if held == winner_class:
        return "winner == held: no switch needed"
    held_eq = held_score.asset.category == AssetCategory.EQUITY
    win_eq = winner_score.asset.category == AssetCategory.EQUITY
    if held_eq and not win_eq:
        thresh = eq_to_def
        kind = "equity→defensive"
    elif not held_eq and win_eq:
        thresh = def_to_eq
        kind = "defensive→equity"
    else:
        thresh = same_cat
        kind = "same-category"
    diff = winner_score.momentum_12m - held_score.momentum_12m
    decision = "SWITCH" if diff > thresh else "BLOCKED"
    return f"{kind} threshold={thresh:.0%}, diff={diff:+.2%} → {decision}"


def main() -> None:
    console = Console()
    console.print("\n[bold cyan]STALE DEFENSIVE RANKING DIAGNOSTIC[/bold cyan]")
    console.print("Observation only — no fixes proposed. Replays Robust_Quarterly_NO_TLT.\n")

    provider = CachedPriceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")
    console.print(f"Loading {len(symbols)} symbols from {FETCH_START}…")
    prices = provider.get_multi_prices(symbols, FETCH_START, END_DATE + timedelta(days=5))
    console.print(f"Loaded {len(prices)} rows.\n")

    # Strategy assets (excludes TLT) — used for the per-date momentum calcs.
    assets_no_tlt = {k: v for k, v in ASSET_REGISTRY.items() if k != AssetClass.BONDS_TREASURY}

    for label, start, end in FOCUS_WINDOWS:
        console.rule(f"[bold yellow]{label}  ({start} → {end})")
        focus_dates = quarter_end_dates(prices, start, end)
        held_state = warmup_holdings(prices, focus_dates, start)

        # The warmup replay records holding *coming in* to each quarter end.
        # We need to extend it through the focus window so each focus date has
        # the correct pre-decision holding.
        all_dates = quarter_end_dates(prices, date(2005, 1, 1), end)
        strategy = build_robust_quarterly_no_tlt_strategy()
        full_held: dict[date, AssetClass] = {}
        for d in all_dates:
            full_held[d] = strategy.current_holding or AssetClass.CASH
            strategy.generate_signal(prices=prices, calc_date=d, current_holding=strategy.current_holding)

        for d in focus_dates:
            held = full_held.get(d, AssetClass.CASH)
            scores = calculate_momentum_scores(
                prices=prices,
                assets=assets_no_tlt,
                calc_date=d,
                lookback_months=12,
                cash_rate=0.0,
            )
            risky = {k: v for k, v in scores.items() if k != AssetClass.CASH}
            if not risky:
                continue
            winner_class = max(risky.keys(), key=lambda k: risky[k].momentum_12m)
            held_score = scores.get(held)
            winner_score = scores.get(winner_class)

            console.print()
            console.print(f"[bold]{d}[/bold]  held coming in: [cyan]{held.value}[/cyan]")
            console.print(build_ranking_table(scores, held))
            console.print(threshold_diagnosis(held, held_score, winner_class, winner_score))

        console.print()


if __name__ == "__main__":
    main()
