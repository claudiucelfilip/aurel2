#!/usr/bin/env python3
"""Weekly overlay runner: production entrypoint for generating a fresh tilt.

Assembles the frozen context pack (src/aurel2/overlay/context_pack.py,
build_frozen_context_pack -- currently v2, per docs/plans/2026-07-10-
context-pack-spec.md) from the SAME live decision path checker.py uses
(DualMomentumStrategy over CachedPriceProvider data, current holding from
the trade journal), then calls the majority-of-5 runner
(src/aurel2/overlay/runner.py::run_overlay_decision) to write+commit
data/{mode}/overlay_tilt.json.

This script does not decide anything itself -- it is glue between "what does
the core currently hold and see" (read-only) and the existing overlay
package (Track C). It is NOT wired into any live cron/schedule by this
change; see docker/docker-compose.yml's overlay-runner profile and
docker/DEPLOY.md for the (inactive) scheduling design.

Usage:
    python3 scripts/run_weekly_overlay.py --mode paper
    python3 scripts/run_weekly_overlay.py --mode paper --no-commit   # dry run, don't git-commit the tilt

Also supports the event-trigger check (docs/plans/2026-07-10-ai-overlay-
design.md, "Cadence: weekly + event-triggered re-run if held asset moves >5%
in 3 days"):

    python3 scripts/run_weekly_overlay.py --mode paper --check-event-trigger-only

exits 0 (no trigger) or 3 (trigger fired) without writing a tilt, for a
lightweight daily cron check ahead of the Monday cadence.
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.stdlib.BoundLogger, logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")))
logging.disable(logging.CRITICAL)

import pandas as pd

from aurel2.config.canonical import CANONICAL_CONFIG, live_asset_registry
from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.cache import CachedPriceProvider
from aurel2.live.journal import TradeJournal, journal_path_for_mode
from aurel2.overlay.context_pack import build_frozen_context_pack
from aurel2.overlay.runner import run_overlay_decision, should_event_trigger
from aurel2.strategies.dual_momentum import DualMomentumStrategy


def _load_prices(as_of: date) -> pd.DataFrame:
    symbols = get_all_yahoo_symbols()
    for extra in ("SPY", "QQQ", "GLD"):
        if extra not in symbols:
            symbols = symbols + [extra]
    provider = CachedPriceProvider()
    prices = provider.get_multi_prices(symbols, as_of - timedelta(days=400), as_of)
    prices["date"] = pd.to_datetime(prices["date"]).dt.date
    return prices


def _current_holding(mode: str) -> tuple[str | None, int]:
    """(holding_symbol, days_held) from the trade journal's most recent
    executed switch. days_held is informational only (self-awareness block
    in the context pack) -- never gates a decision.
    """
    journal = TradeJournal(filepath=journal_path_for_mode(mode))
    decisions = [e for e in journal.entries if e.entry_type == "decision"]
    if not decisions:
        return None, 0
    latest = decisions[-1]
    holding = latest.current_holding_after or latest.current_holding_symbol
    switch_date = journal.last_switch_date()
    days_held = (date.today() - switch_date).days if switch_date else 0
    return holding, days_held


def build_pack_for_today(mode: str, as_of: date) -> dict:
    prices = _load_prices(as_of)
    dm_assets = live_asset_registry()
    dm_config = CANONICAL_CONFIG.dual_momentum
    strategy = DualMomentumStrategy(
        assets=dm_assets,
        lookback_months=dm_config.lookback_months,
        switch_threshold=dm_config.switch_threshold,
        cash_rate=dm_config.cash_rate,
    )

    holding_symbol, days_held = _current_holding(mode)
    current_holding_class = None
    if holding_symbol:
        for ac, asset in dm_assets.items():
            if asset.symbol == holding_symbol:
                current_holding_class = ac
                break

    signal = strategy.generate_signal(prices=prices, calc_date=as_of, current_holding=current_holding_class)
    deterministic_signal = {
        "action": signal.action.value if hasattr(signal.action, "value") else str(signal.action),
        "asset_symbol": signal.asset.symbol if signal.asset else None,
        "reason": signal.reason,
    }

    return build_frozen_context_pack(
        prices=prices,
        calc_date=as_of,
        dm_assets=dm_assets,
        current_holding_symbol=holding_symbol,
        days_held=days_held,
        deterministic_signal=deterministic_signal,
    )


def check_event_trigger(mode: str, as_of: date) -> bool:
    """3-day % move on the currently held asset -- pure predicate wrapper
    around should_event_trigger, sourcing pct_move_3d from cached prices.
    """
    holding_symbol, _ = _current_holding(mode)
    if not holding_symbol:
        return False

    provider = CachedPriceProvider()
    prices = provider.get_prices(holding_symbol, as_of - timedelta(days=10), as_of)
    if prices.empty:
        return False
    prices = prices.sort_values("date")
    prices = prices[pd.to_datetime(prices["date"]).dt.date <= as_of]
    if len(prices) < 4:
        return False

    latest_close = float(prices.iloc[-1]["close"])
    three_days_ago_close = float(prices.iloc[-4]["close"])
    if three_days_ago_close == 0:
        return False
    pct_move_3d = (latest_close / three_days_ago_close - 1) * 100

    return should_event_trigger(pct_move_3d)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="paper", choices=["paper", "live"])
    parser.add_argument("--model", default="claude-fable-5")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument(
        "--check-event-trigger-only",
        action="store_true",
        help="Only check the >5%%-move-in-3-days event trigger; don't run the overlay.",
    )
    args = parser.parse_args()

    today = date.today()

    if args.check_event_trigger_only:
        triggered = check_event_trigger(args.mode, today)
        print(f"event_trigger={triggered}")
        return 3 if triggered else 0

    pack = build_pack_for_today(args.mode, today)
    tilt = run_overlay_decision(pack, mode=args.mode, model=args.model, auto_commit=not args.no_commit)
    print(f"tilt written: regime_view={tilt['regime_view']} sample_agreement={tilt['sample_agreement']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
