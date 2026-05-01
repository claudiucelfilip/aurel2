#!/usr/bin/env python3
"""Episode-level P&L report for early-rotation divergences.

This compresses daily divergence rows into distinct divergence episodes:
- trigger date = first day baseline and early_rotation choose different holdings
- end date = first later decision date where holdings reconverge, or period end
- score the whole episode path, including simple transaction costs on rotation
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scripts.replay_live_trader_vs_aurel2 import load_prices
from scripts.early_rotation_divergence_report import simulate_mode


TRANSACTION_COST = 0.001


@dataclass
class Episode:
    start_idx: int
    end_idx: int
    trigger_date: date
    end_date: date
    reconverged: bool
    baseline_start_symbol: str
    early_start_symbol: str
    baseline_end_symbol: str
    early_end_symbol: str
    baseline_trigger: str
    early_trigger: str
    regime: str


def _price_lookup(prices: pd.DataFrame) -> dict[tuple[str, date], float]:
    return {
        (row.symbol, row.date): float(row.close)
        for row in prices.itertuples(index=False)
    }


def _business_dates(prices: pd.DataFrame, start_date: date, end_date: date) -> list[date]:
    spy = prices[
        (prices["symbol"] == "SPY")
        & (prices["date"] >= start_date)
        & (prices["date"] <= end_date)
    ]["date"].sort_values().unique()
    return [pd.Timestamp(d).date() for d in spy]


def find_episodes(
    baseline_decisions: list,
    early_decisions: list,
) -> list[Episode]:
    episodes: list[Episode] = []
    in_episode = False
    start_idx = -1

    for idx, (base, early) in enumerate(zip(baseline_decisions, early_decisions)):
        diverged = base.next_symbol != early.next_symbol

        if diverged and not in_episode:
            in_episode = True
            start_idx = idx
        elif not diverged and in_episode:
            start_base = baseline_decisions[start_idx]
            start_early = early_decisions[start_idx]
            end_base = baseline_decisions[idx]
            end_early = early_decisions[idx]
            episodes.append(
                Episode(
                    start_idx=start_idx,
                    end_idx=idx,
                    trigger_date=start_base.date,
                    end_date=end_base.date,
                    reconverged=True,
                    baseline_start_symbol=start_base.next_symbol,
                    early_start_symbol=start_early.next_symbol,
                    baseline_end_symbol=end_base.next_symbol,
                    early_end_symbol=end_early.next_symbol,
                    baseline_trigger=start_base.trigger,
                    early_trigger=start_early.trigger,
                    regime=start_early.regime,
                )
            )
            in_episode = False

    if in_episode:
        start_base = baseline_decisions[start_idx]
        start_early = early_decisions[start_idx]
        end_base = baseline_decisions[-1]
        end_early = early_decisions[-1]
        episodes.append(
            Episode(
                start_idx=start_idx,
                end_idx=len(baseline_decisions) - 1,
                trigger_date=start_base.date,
                end_date=end_base.date,
                reconverged=False,
                baseline_start_symbol=start_base.next_symbol,
                early_start_symbol=start_early.next_symbol,
                baseline_end_symbol=end_base.next_symbol,
                early_end_symbol=end_early.next_symbol,
                baseline_trigger=start_base.trigger,
                early_trigger=start_early.trigger,
                regime=start_early.regime,
            )
        )

    return episodes


def episode_path_return(
    decisions: list,
    price_map: dict[tuple[str, date], float],
    start_idx: int,
    end_idx: int,
    transaction_cost: float = TRANSACTION_COST,
) -> float | None:
    if start_idx > end_idx:
        return None

    capital = 1.0
    prev_symbol = decisions[start_idx].current_symbol

    for idx in range(start_idx, end_idx + 1):
        decision = decisions[idx]
        held_symbol = decision.next_symbol
        if held_symbol is None:
            return None

        if idx > start_idx:
            prev_date = decisions[idx - 1].date
            cur_date = decision.date
            prev_price = price_map.get((prev_symbol, prev_date))
            cur_price = price_map.get((prev_symbol, cur_date))
            if prev_price is None or cur_price is None or prev_price <= 0:
                return None
            capital *= cur_price / prev_price

        if idx == start_idx:
            if decision.action in {"buy", "rotate"}:
                capital *= 1.0 - transaction_cost
        else:
            if decision.action == "rotate":
                capital *= 1.0 - transaction_cost

        prev_symbol = held_symbol

    return capital - 1.0


def run_period(prices: pd.DataFrame, start_date: date, end_date: date) -> tuple[list[dict], dict]:
    baseline = simulate_mode(prices, start_date, end_date, "baseline")
    early = simulate_mode(prices, start_date, end_date, "early_rotation")
    episodes = find_episodes(baseline, early)
    price_map = _price_lookup(prices)

    rows: list[dict] = []
    for ep in episodes:
        base_ret = episode_path_return(baseline, price_map, ep.start_idx, ep.end_idx)
        early_ret = episode_path_return(early, price_map, ep.start_idx, ep.end_idx)
        rows.append(
            {
                "trigger_date": ep.trigger_date.isoformat(),
                "end_date": ep.end_date.isoformat(),
                "days": ep.end_idx - ep.start_idx + 1,
                "baseline_start": ep.baseline_start_symbol,
                "early_start": ep.early_start_symbol,
                "baseline_end": ep.baseline_end_symbol,
                "early_end": ep.early_end_symbol,
                "reconverged": ep.reconverged,
                "regime": ep.regime,
                "baseline_trigger": ep.baseline_trigger,
                "early_trigger": ep.early_trigger,
                "baseline_return": base_ret,
                "early_return": early_ret,
                "delta": (None if base_ret is None or early_ret is None else early_ret - base_ret),
            }
        )

    comparable = [r for r in rows if r["delta"] is not None]
    summary = {
        "episodes": len(rows),
        "comparable": len(comparable),
        "early_better": sum(1 for r in comparable if r["delta"] > 0),
        "avg_delta": (sum(r["delta"] for r in comparable) / len(comparable)) if comparable else None,
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
    print("EARLY-ROTATION EPISODE REPORT")
    print("=" * 128)
    prices = load_prices(date(2019, 1, 1), end_date, refresh_missing=False)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    for label, start, end in periods:
        rows, summary = run_period(prices, start, end)
        print(f"\nPeriod: {label} ({start} -> {end})")
        print("-" * 128)
        avg_delta = _fmt_pct(summary["avg_delta"]) if summary["avg_delta"] is not None else "n/a"
        print(
            f"Episodes: {summary['episodes']} | "
            f"Early better: {summary['early_better']}/{summary['comparable']} | "
            f"Average delta: {avg_delta}"
        )
        print("-" * 128)
        print(
            f"{'Trigger':10s} {'End':10s} {'Len':>4s} {'Base':12s} {'Early':12s} "
            f"{'Base Ret':>10s} {'Early Ret':>10s} {'Delta':>10s} {'Reconverged':>11s} {'Trigger':22s}"
        )
        print("-" * 128)
        for row in rows:
            print(
                f"{row['trigger_date']:10s} {row['end_date']:10s} {row['days']:>4d} "
                f"{row['baseline_start']:12s} {row['early_start']:12s} "
                f"{_fmt_pct(row['baseline_return']):>10s} {_fmt_pct(row['early_return']):>10s} "
                f"{_fmt_pct(row['delta']):>10s} {str(row['reconverged']):>11s} {row['early_trigger']:22s}"
            )


if __name__ == "__main__":
    main()
