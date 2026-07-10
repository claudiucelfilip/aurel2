#!/usr/bin/env python3
"""Track A acceptance test: replay the live decision path over a fixed window
using the pinned price snapshot, and dump a decision log (date, action,
symbol, decision_type) for before/after diffing.

Usage:
    python3 scripts/acceptance_replay.py --out /tmp/pre_cleanup.json
    python3 scripts/acceptance_replay.py --out /tmp/post_cleanup.json
    python3 scripts/diff_acceptance_replays.py /tmp/pre_cleanup.json /tmp/post_cleanup.json
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import logging
import structlog
structlog.configure(wrapper_class=structlog.stdlib.BoundLogger, logger_factory=structlog.PrintLoggerFactory(file=open("/dev/null", "w")))
logging.disable(logging.CRITICAL)

import pandas as pd

from aurel2.engine.backtest import BacktestEngine

SNAPSHOT_PATH = Path(__file__).parent.parent / "data" / "acceptance" / "price_snapshot_2026-02-11_2026-07-09.csv"
START = date(2026, 2, 11)
END = date(2026, 7, 9)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="Path to write the decision log JSON")
    args = parser.parse_args()

    prices = pd.read_csv(SNAPSHOT_PATH, parse_dates=["date"])
    prices["date"] = prices["date"].dt.date

    engine = BacktestEngine(initial_capital=10000, use_ai=False)
    result = engine.run(prices=prices, start_date=START, end_date=END, frequency="monthly")

    decisions = []
    for snap in result.snapshots:
        decisions.append({
            "date": str(snap.date),
            "holding_symbol": snap.holding_symbol,
        })

    trades = []
    for t in result.trades:
        trades.append({
            "date": str(t.date),
            "action": t.action.value if hasattr(t.action, "value") else str(t.action),
            "symbol": t.asset.symbol if getattr(t, "asset", None) else None,
        })

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"trades": trades, "num_trades": len(trades), "decisions": decisions}, f, indent=2)

    print(f"\nWrote {len(trades)} trades to {out_path}")
    for t in trades:
        print(f"  {t['date']}  {t['action']:5s}  {t['symbol']}")


if __name__ == "__main__":
    main()
