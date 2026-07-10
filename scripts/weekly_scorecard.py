#!/usr/bin/env python3
"""Weekly scorecard: the automated referee for the A2+overlay parallel run.

Computes, per arm (A2+overlay, live-trader, A2-bare shadow replay, QQQ):
cumulative return since run start, alpha vs QQQ, max DD, Sharpe-so-far.
Also reports overlay accounting (interventions this week, sample_agreement,
regime_view history) and appends a row to data/{mode}/scorecard_history.jsonl.

Exact rule this implements: docs/plans/2026-07-10-graduation-rule.md.
Metric formulas: src/aurel2/scorecard/metrics.py. Arm series construction:
src/aurel2/scorecard/arms.py.

Usage:
    python3 scripts/weekly_scorecard.py --mode paper --run-start 2026-07-14
    python3 scripts/weekly_scorecard.py --mode paper --run-start 2026-07-14 --no-notify

Any arm's data being missing or partial is reported, never a crash -- see
"graceful degradation" throughout.
"""

import argparse
import json
import sys
from datetime import date, datetime
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
from aurel2.live.journal import journal_path_for_mode
from aurel2.notifications.ntfy import NtfyNotifier
from aurel2.overlay.claw_shadow import claw_shadow_path_for_mode
from aurel2.scorecard.arms import (
    live_trader_daily_series,
    overlay_accounting,
    paper_journal_daily_series,
)
from aurel2.scorecard.metrics import alpha_vs_benchmark, cumulative_return, max_drawdown, sharpe_ratio

LIVE_TRADER_TRADES_PATH = Path("/Users/claudiu/.openclaw/workspace/live-trader/trades.jsonl")


def scorecard_history_path(mode: str) -> Path:
    return Path(f"data/{mode}/scorecard_history.jsonl")


def scorecard_latest_path(mode: str) -> Path:
    return Path(f"data/{mode}/scorecard_latest.json")


def a2_overlay_series(mode: str, start: date, end: date) -> list[tuple[date, float]]:
    return paper_journal_daily_series(Path(journal_path_for_mode(mode)), start, end)


def a2_bare_shadow_series(
    start: date, end: date, initial_capital: float
) -> tuple[list[tuple[date, float]], str | None]:
    """Replay the core (overlay disabled) daily over the run window via
    BacktestEngine, same canonical config as production. Returns
    (series, error) -- error is a string reason if the replay couldn't run
    (e.g. no price data), never a raised exception.
    """
    try:
        symbols = get_all_yahoo_symbols()
        for extra in ("SPY", "QQQ", "AGG"):
            if extra not in symbols:
                symbols = symbols + [extra]

        provider = CachedPriceProvider()
        prices = provider.get_multi_prices(symbols, start - pd.Timedelta(days=400), end)
        prices["date"] = pd.to_datetime(prices["date"]).dt.date

        engine = BacktestEngine(initial_capital=initial_capital, use_ai=False, overlay_enabled=False)
        result = engine.run(prices, start_date=start, end_date=end, frequency="daily")

        series = [(s.date, float(s.total_value)) for s in result.snapshots]
        return series, None
    except Exception as e:  # noqa: BLE001 -- shadow replay must never crash the scorecard
        return [], str(e)


def qqq_series(start: date, end: date, initial_capital: float) -> tuple[list[tuple[date, float]], str | None]:
    try:
        provider = CachedPriceProvider()
        prices = provider.get_prices("QQQ", start, end)
        if not prices.empty:
            prices["date"] = pd.to_datetime(prices["date"]).dt.date
            # get_prices returns a 400-day lookback buffer before start_date
            # (intended for callers needing momentum history) -- the scorecard
            # only wants the run window itself.
            prices = prices[(prices["date"] >= start) & (prices["date"] <= end)]
        if prices.empty:
            return [], "no QQQ price data in range"
        prices = prices.sort_values("date")
        first_close = float(prices.iloc[0]["close"])
        if first_close <= 0:
            return [], "invalid QQQ starting price"
        shares = initial_capital / first_close
        series = [
            (row["date"] if isinstance(row["date"], date) else pd.to_datetime(row["date"]).date(), shares * float(row["close"]))
            for _, row in prices.iterrows()
        ]
        return series, None
    except Exception as e:  # noqa: BLE001
        return [], str(e)


def regime_view_history(mode: str, start: date, end: date) -> list[dict]:
    """Sample-agreement / regime_view history from claw_shadow.jsonl (read-only
    telemetry) for the run window. Missing/unreadable file -> empty list.
    """
    path = Path(claw_shadow_path_for_mode(mode))
    if not path.exists():
        return []
    history = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                as_of = record.get("as_of")
                if not as_of:
                    continue
                try:
                    d = date.fromisoformat(as_of)
                except ValueError:
                    continue
                if start <= d <= end:
                    history.append(record)
    except OSError:
        return []
    return history


def arm_metrics(series: list[tuple[date, float]], benchmark: list[tuple[date, float]]) -> dict:
    if not series:
        return {
            "cumulative_return": None,
            "alpha_vs_qqq": None,
            "max_drawdown": None,
            "sharpe": None,
            "data_points": 0,
            "gap": True,
        }
    return {
        "cumulative_return": cumulative_return(series),
        "alpha_vs_qqq": alpha_vs_benchmark(series, benchmark),
        "max_drawdown": max_drawdown(series),
        "sharpe": sharpe_ratio(series),
        "data_points": len(series),
        "gap": False,
    }


def build_scorecard(mode: str, run_start: date, today: date | None = None) -> dict:
    """Assemble one full scorecard record. Pure w.r.t. its inputs except for
    the filesystem/price-provider reads it performs -- no ntfy/notify side
    effects here (caller decides whether to send/persist).
    """
    if today is None:
        today = date.today()

    a2_series = a2_overlay_series(mode, run_start, today)
    starting_capital = a2_series[0][1] if a2_series else 10000.0

    lt_series, lt_gap = live_trader_daily_series(LIVE_TRADER_TRADES_PATH, run_start, today)
    bare_series, bare_error = a2_bare_shadow_series(run_start, today, starting_capital)
    qqq_series_, qqq_error = qqq_series(run_start, today, starting_capital)

    arms = {
        "a2_overlay": arm_metrics(a2_series, qqq_series_),
        "live_trader": {**arm_metrics(lt_series, qqq_series_), "gap": lt_gap or not lt_series},
        "a2_bare": {**arm_metrics(bare_series, qqq_series_), "error": bare_error},
        "qqq": {**arm_metrics(qqq_series_, qqq_series_), "error": qqq_error},
    }

    overlay = overlay_accounting(Path(journal_path_for_mode(mode)), run_start, today)
    regime_history = regime_view_history(mode, run_start, today)

    trading_days_elapsed = len(a2_series) if a2_series else len(qqq_series_)

    return {
        "mode": mode,
        "run_start": run_start.isoformat(),
        "as_of": today.isoformat(),
        "trading_days_elapsed": trading_days_elapsed,
        "arms": arms,
        "overlay": {
            "applied_count": overlay["applied_count"],
            "ignored_count": overlay["ignored_count"],
            "activity": overlay["activity"],
            "regime_view_history": [
                {
                    "as_of": r.get("as_of"),
                    "sample_agreement": (r.get("parsed") or {}).get("sample_agreement"),
                    "regime_view": (r.get("parsed") or {}).get("regime_view"),
                }
                for r in regime_history
            ],
        },
    }


def format_ntfy_summary(card: dict) -> str:
    lines = [f"Scorecard {card['mode']} — as of {card['as_of']} ({card['trading_days_elapsed']} trading days)"]
    for name, key in [("A2+overlay", "a2_overlay"), ("live-trader", "live_trader"), ("A2-bare", "a2_bare"), ("QQQ", "qqq")]:
        m = card["arms"][key]
        if m.get("gap") or m.get("error"):
            lines.append(f"{name}: DATA GAP")
            continue
        ret = m["cumulative_return"]
        dd = m["max_drawdown"]
        sharpe = m["sharpe"]
        ret_s = f"{ret:+.1%}" if ret is not None else "n/a"
        dd_s = f"{dd:.1%}" if dd is not None else "n/a"
        sharpe_s = f"{sharpe:.2f}" if sharpe is not None else "n/a"
        lines.append(f"{name}: ret {ret_s}  DD {dd_s}  Sharpe {sharpe_s}")
    lines.append(f"Overlay: {card['overlay']['applied_count']} applied, {card['overlay']['ignored_count']} ignored this window")
    return "\n".join(lines)


def append_history(mode: str, card: dict) -> None:
    path = scorecard_history_path(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(card) + "\n")


def write_latest(mode: str, card: dict) -> None:
    path = scorecard_latest_path(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card, indent=2))


def notify(card: dict, ntfy_topic: str) -> None:
    notifier = NtfyNotifier(topic=ntfy_topic)
    notifier.send(
        message=format_ntfy_summary(card),
        title=f"Aurel2: Weekly scorecard ({card['mode']})",
        tags=["bar_chart"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="paper", choices=["paper", "live"])
    parser.add_argument("--run-start", required=True, help="Run start date, YYYY-MM-DD")
    parser.add_argument("--ntfy-topic", default="aurel2")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    run_start = date.fromisoformat(args.run_start)
    card = build_scorecard(args.mode, run_start)

    append_history(args.mode, card)
    write_latest(args.mode, card)

    print(format_ntfy_summary(card))

    if not args.no_notify:
        notify(card, args.ntfy_topic)

    return 0


if __name__ == "__main__":
    sys.exit(main())
