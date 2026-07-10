#!/usr/bin/env python3
"""Weekly fidelity guard: replay the last N live decision days through the
backtest engine's decision path and diff against what actually happened
(data/{mode}/trade_journal.json). Sends an ntfy alert on ANY divergence.

This is the continuous version of the Phase-1 actual-vs-replay diff
(docs/plans/2026-07-09-edge-decomposition-goal.md, Phase 2 non-negotiable #2):
backtest != live must be structurally unable to persist unnoticed. Both the
live checker (aurel2.live.checker.Checker) and this guard's replay engine
(aurel2.engine.backtest.BacktestEngine) load strategy/orchestrator parameters
from the same aurel2.config.canonical.CANONICAL_CONFIG, so a divergence here
means actual decision-path drift, not a config mismatch between two files.

Usage:
    python3 scripts/fidelity_guard.py --mode paper --days 10
    python3 scripts/fidelity_guard.py --mode paper --days 10 --no-notify   # dry run, print only

Exit code: 0 if no divergence (or nothing to check), 1 if any divergence found.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.stdlib.BoundLogger, logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")))
logging.disable(logging.CRITICAL)

import pandas as pd

from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.cache import CachedPriceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.live.journal import TradeJournal, journal_path_for_mode
from aurel2.notifications.ntfy import NtfyNotifier


@dataclass
class LiveDecisionDay:
    day: date
    action: str
    symbol: str | None
    current_holding_before: str | None


@dataclass
class Divergence:
    day: date
    live_action: str
    live_symbol: str | None
    replay_action: str
    replay_symbol: str | None


def load_recent_live_decisions(mode: str, days: int) -> list[LiveDecisionDay]:
    """Pull the last N distinct decision days from the live trade journal.

    Only "decision" entries are considered (not execution-only follow-ups).
    One entry per calendar day is kept (the last decision recorded that day).
    """
    journal = TradeJournal(filepath=journal_path_for_mode(mode))
    decisions = [e for e in journal.entries if e.entry_type == "decision"]

    by_day: dict[date, LiveDecisionDay] = {}
    for entry in decisions:
        try:
            day = datetime.fromisoformat(entry.timestamp).date()
        except (ValueError, TypeError):
            continue
        by_day[day] = LiveDecisionDay(
            day=day,
            action=entry.action or "hold",
            symbol=entry.decision_symbol if entry.decision_symbol is not None else entry.symbol,
            current_holding_before=entry.current_holding_before,
        )

    ordered_days = sorted(by_day.keys())[-days:]
    return [by_day[d] for d in ordered_days]


def replay_decisions(live_days: list[LiveDecisionDay]) -> dict[date, tuple[str, str | None]]:
    """Replay each live decision day's date through the backtest engine's
    decision path (dual_momentum -> orchestrator), using the held asset
    that preceded that decision so the replay compares apples-to-apples.

    Returns {day: (action, asset_symbol)}.
    """
    if not live_days:
        return {}

    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols = symbols + ["SPY"]
    if "AGG" not in symbols:
        symbols = symbols + ["AGG"]

    start = live_days[0].day - timedelta(days=400)
    end = live_days[-1].day

    provider = CachedPriceProvider()
    prices = provider.get_multi_prices(symbols, start, end)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    engine = BacktestEngine(initial_capital=10000, use_ai=False)

    results: dict[date, tuple[str, str | None]] = {}
    for live_day in live_days:
        current_holding = None
        if live_day.current_holding_before and live_day.current_holding_before != "CASH":
            current_holding = engine._symbol_to_asset_class(live_day.current_holding_before)

        signal = engine.dual_momentum.generate_signal(
            prices=prices,
            calc_date=live_day.day,
            current_holding=current_holding,
        )
        normalized = engine._normalize_signal(signal)
        market_context = engine._build_market_context(prices, live_day.day)

        decision = engine.orchestrator.analyze(
            signals={"dual_momentum": normalized},
            market_context=market_context,
            current_holding=live_day.current_holding_before,
        )
        results[live_day.day] = (decision.action.value, decision.asset_symbol)

    return results


def find_divergences(
    live_days: list[LiveDecisionDay],
    replayed: dict[date, tuple[str, str | None]],
) -> list[Divergence]:
    divergences = []
    for live_day in live_days:
        replay = replayed.get(live_day.day)
        if replay is None:
            continue
        replay_action, replay_symbol = replay
        if replay_action != live_day.action or replay_symbol != live_day.symbol:
            divergences.append(Divergence(
                day=live_day.day,
                live_action=live_day.action,
                live_symbol=live_day.symbol,
                replay_action=replay_action,
                replay_symbol=replay_symbol,
            ))
    return divergences


def notify_divergences(divergences: list[Divergence], mode: str, ntfy_topic: str) -> None:
    lines = [f"{d.day}: live={d.live_action}/{d.live_symbol or 'cash'} vs replay={d.replay_action}/{d.replay_symbol or 'cash'}" for d in divergences]
    message = f"Fidelity guard found {len(divergences)} divergence(s) in {mode} mode:\n\n" + "\n".join(lines)
    notifier = NtfyNotifier(topic=ntfy_topic)
    notifier.send(
        message=message,
        title=f"Aurel2: Fidelity guard divergence ({mode})",
        priority="high",
        tags=["warning", "mag"],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="paper", help="Trading mode: paper or live")
    parser.add_argument("--days", type=int, default=10, help="Number of recent live decision days to replay")
    parser.add_argument("--ntfy-topic", default="aurel2", help="Ntfy topic for divergence alerts")
    parser.add_argument("--no-notify", action="store_true", help="Print findings without sending ntfy")
    args = parser.parse_args()

    live_days = load_recent_live_decisions(args.mode, args.days)
    if not live_days:
        print(f"No decision entries found in {args.mode} journal — nothing to check.")
        return 0

    print(f"Replaying {len(live_days)} live decision day(s) from {live_days[0].day} to {live_days[-1].day}...")
    replayed = replay_decisions(live_days)
    divergences = find_divergences(live_days, replayed)

    if not divergences:
        print(f"OK — {len(live_days)} day(s) checked, no divergence.")
        return 0

    print(f"DIVERGENCE — {len(divergences)} of {len(live_days)} day(s) differ:")
    for d in divergences:
        print(f"  {d.day}: live={d.live_action}/{d.live_symbol or 'cash'} vs replay={d.replay_action}/{d.replay_symbol or 'cash'}")

    if not args.no_notify:
        notify_divergences(divergences, args.mode, args.ntfy_topic)

    return 1


if __name__ == "__main__":
    sys.exit(main())
