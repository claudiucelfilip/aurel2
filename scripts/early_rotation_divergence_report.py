#!/usr/bin/env python3
"""Report where the early-rotation variant diverges from the baseline.

The goal is to answer a narrow question:
when the constrained early-rotation rule fires and baseline does not,
does that earlier rotation improve forward returns over 5d / 20d?
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scripts.replay_live_trader_vs_aurel2 import (
    WATCHLIST,
    build_features,
    choose_target,
    infer_regime,
    load_prices,
    should_rotate_decision,
)


@dataclass
class DecisionSnapshot:
    date: date
    current_symbol: str | None
    action: str
    next_symbol: str | None
    target_symbol: str | None
    score_gap: float | None
    trigger: str
    regime: str


def _daily_check_dates(prices: pd.DataFrame, start_date: date, end_date: date) -> list[date]:
    spy = prices[
        (prices["symbol"] == "SPY")
        & (prices["date"] >= start_date)
        & (prices["date"] <= end_date)
    ]["date"].sort_values().unique()
    return [pd.Timestamp(d).date() for d in spy]


def simulate_mode(prices: pd.DataFrame, start_date: date, end_date: date, mode: str) -> list[DecisionSnapshot]:
    decisions: list[DecisionSnapshot] = []
    current_symbol: str | None = None
    last_rotation_date: date | None = None

    for check_date in _daily_check_dates(prices, start_date, end_date):
        features = build_features(prices, WATCHLIST, check_date)
        if not features:
            continue

        regime = infer_regime(features)
        target_symbol, target_row, ranked = choose_target(features, regime)

        if current_symbol is None:
            current_symbol = target_symbol
            decisions.append(
                DecisionSnapshot(
                    date=check_date,
                    current_symbol=None,
                    action="buy",
                    next_symbol=current_symbol,
                    target_symbol=target_symbol,
                    score_gap=None,
                    trigger="initial_buy",
                    regime=regime["name"],
                )
            )
            last_rotation_date = check_date
            continue

        current_row = next(
            (r for r in ranked if r["symbol"] == current_symbol),
            {"symbol": current_symbol, "score": -999.0},
        )
        rotate, score_gap, _, trigger = should_rotate_decision(
            current_symbol=current_symbol,
            target_symbol=target_symbol,
            current_row=current_row,
            target_row=target_row,
            regime=regime,
            last_rotation_date=last_rotation_date,
            check_date=check_date,
            mode=mode,
        )

        if rotate:
            next_symbol = target_symbol
            decisions.append(
                DecisionSnapshot(
                    date=check_date,
                    current_symbol=current_symbol,
                    action="rotate",
                    next_symbol=next_symbol,
                    target_symbol=target_symbol,
                    score_gap=score_gap,
                    trigger=trigger,
                    regime=regime["name"],
                )
            )
            current_symbol = next_symbol
            last_rotation_date = check_date
        else:
            decisions.append(
                DecisionSnapshot(
                    date=check_date,
                    current_symbol=current_symbol,
                    action="hold",
                    next_symbol=current_symbol,
                    target_symbol=target_symbol,
                    score_gap=score_gap,
                    trigger=trigger,
                    regime=regime["name"],
                )
            )

    return decisions


def _forward_return(prices: pd.DataFrame, symbol: str, start_date: date, trading_days: int) -> float | None:
    series = prices[(prices["symbol"] == symbol) & (prices["date"] >= start_date)].sort_values("date")
    if series.empty or len(series) <= trading_days:
        return None
    start_px = float(series.iloc[0]["close"])
    end_px = float(series.iloc[trading_days]["close"])
    if start_px <= 0:
        return None
    return end_px / start_px - 1.0


def summarize_divergences(prices: pd.DataFrame, start_date: date, end_date: date) -> tuple[list[dict], dict]:
    baseline = {d.date: d for d in simulate_mode(prices, start_date, end_date, "baseline")}
    early = {d.date: d for d in simulate_mode(prices, start_date, end_date, "early_rotation")}

    rows: list[dict] = []
    for check_date in sorted(set(baseline) & set(early)):
        base = baseline[check_date]
        alt = early[check_date]
        if base.action == alt.action and base.next_symbol == alt.next_symbol:
            continue

        base_symbol = base.next_symbol
        alt_symbol = alt.next_symbol

        row = {
            "date": check_date.isoformat(),
            "baseline_action": base.action,
            "baseline_symbol": base_symbol,
            "baseline_trigger": base.trigger,
            "baseline_gap": base.score_gap,
            "early_action": alt.action,
            "early_symbol": alt_symbol,
            "early_trigger": alt.trigger,
            "early_gap": alt.score_gap,
            "regime": alt.regime,
            "base_fwd_5d": _forward_return(prices, base_symbol, check_date, 5) if base_symbol else None,
            "early_fwd_5d": _forward_return(prices, alt_symbol, check_date, 5) if alt_symbol else None,
            "base_fwd_20d": _forward_return(prices, base_symbol, check_date, 20) if base_symbol else None,
            "early_fwd_20d": _forward_return(prices, alt_symbol, check_date, 20) if alt_symbol else None,
        }
        rows.append(row)

    def _better(a: float | None, b: float | None) -> bool | None:
        if a is None or b is None:
            return None
        return a > b

    early_better_5d = [r for r in rows if _better(r["early_fwd_5d"], r["base_fwd_5d"]) is True]
    early_better_20d = [r for r in rows if _better(r["early_fwd_20d"], r["base_fwd_20d"]) is True]
    comparable_5d = [r for r in rows if r["early_fwd_5d"] is not None and r["base_fwd_5d"] is not None]
    comparable_20d = [r for r in rows if r["early_fwd_20d"] is not None and r["base_fwd_20d"] is not None]

    summary = {
        "divergence_count": len(rows),
        "comparable_5d": len(comparable_5d),
        "comparable_20d": len(comparable_20d),
        "early_better_5d": len(early_better_5d),
        "early_better_20d": len(early_better_20d),
    }
    return rows, summary


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2%}"


def main() -> None:
    end_date = date(2026, 4, 20)
    periods = [
        ("1y", date(2025, 4, 1), end_date),
        ("5y", date(2021, 4, 1), end_date),
    ]

    print("=" * 128)
    print("EARLY-ROTATION DIVERGENCE REPORT")
    print("=" * 128)
    prices = load_prices(date(2019, 1, 1), end_date, refresh_missing=False)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    for label, start, end in periods:
        rows, summary = summarize_divergences(prices, start, end)
        print(f"\nPeriod: {label} ({start} -> {end})")
        print("-" * 128)
        print(
            f"Divergences: {summary['divergence_count']} | "
            f"Early better over 5d: {summary['early_better_5d']}/{summary['comparable_5d']} | "
            f"Early better over 20d: {summary['early_better_20d']}/{summary['comparable_20d']}"
        )
        print("-" * 128)
        print(
            f"{'Date':10s} {'Base':16s} {'Early':16s} {'Regime':8s} "
            f"{'Base 5d':>10s} {'Early 5d':>10s} {'Base 20d':>10s} {'Early 20d':>10s} {'Trigger':22s}"
        )
        print("-" * 128)
        for row in rows:
            base = f"{row['baseline_action']} {row['baseline_symbol']}"
            early = f"{row['early_action']} {row['early_symbol']}"
            print(
                f"{row['date']:10s} {base:16s} {early:16s} {row['regime']:8s} "
                f"{_fmt_pct(row['base_fwd_5d']):>10s} {_fmt_pct(row['early_fwd_5d']):>10s} "
                f"{_fmt_pct(row['base_fwd_20d']):>10s} {_fmt_pct(row['early_fwd_20d']):>10s} "
                f"{row['early_trigger']:22s}"
            )


if __name__ == "__main__":
    main()
