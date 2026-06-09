#!/usr/bin/env python3
"""Read-only Aurel2 paper PnL divergence scan.

This replaces brittle cron-time jq generation. It reads the paper trade journal
from the running container, compares recent realized movement to the latest
expectation band file, and writes a compact JSONL heartbeat record.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path("/root/aurel2")
EXPECTATION_PATH = REPO / "data/expectation_bands_may2026.json"
MEMORY_PATH = Path("/root/.openclaw/workspace/memory/aurel2-pnl-scan.jsonl")
CONTAINER = "aurel2-trading-aurel2-1"
TRADE_JOURNAL = "/app/data/paper/trade_journal.json"


@dataclass(frozen=True)
class Point:
    ts: datetime
    date: str
    value: float


def load_container_json(path: str) -> Any:
    completed = subprocess.run(
        ["docker", "exec", CONTAINER, "cat", path],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


def parse_ts(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_points(entries: list[dict[str, Any]]) -> list[Point]:
    points: list[Point] = []
    for entry in entries:
        raw_ts = str(entry.get("timestamp") or "").strip()
        raw_value = entry.get("account_value_before")
        if not raw_ts or raw_value is None:
            continue
        try:
            ts = parse_ts(raw_ts)
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        points.append(Point(ts=ts, date=ts.date().isoformat(), value=value))
    return sorted(points, key=lambda item: item.ts)


def pct_change(start: float, end: float) -> float | None:
    if start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def max_drawdown_pct(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value / peak - 1.0) * 100.0)
    return worst


def load_expectations() -> dict[str, Any]:
    if not EXPECTATION_PATH.is_file():
        raise RuntimeError(f"missing expectation file: {EXPECTATION_PATH}")
    payload = json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expectation file is not an object: {EXPECTATION_PATH}")
    return payload


def append_memory(record: dict[str, Any]) -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MEMORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def main() -> int:
    raw_entries = load_container_json(TRADE_JOURNAL)
    if not isinstance(raw_entries, list):
        raise RuntimeError("trade journal is not a JSON list")
    points = extract_points(raw_entries)
    if len(points) < 2:
        raise RuntimeError("not enough paper value points for divergence scan")

    expectations = load_expectations()
    paper_realized = expectations.get("paper_realized") or {}
    periods = expectations.get("periods") or {}
    paper_run = periods.get("paper_run") or {}

    last = points[-1]
    previous = points[-2]
    last_day_pnl = round(last.value - previous.value, 2)
    last_day_return_pct = pct_change(previous.value, last.value)

    recent = points[-8:]
    recent_returns = [
        pct_change(a.value, b.value)
        for a, b in zip(recent, recent[1:])
        if pct_change(a.value, b.value) is not None
    ]
    negative_recent_returns = [value for value in recent_returns[-5:] if value < 0]
    rolling_7d_pnl = round(recent[-1].value - recent[0].value, 2)
    rolling_7d_return_pct = pct_change(recent[0].value, recent[-1].value)
    rolling_7d_drawdown_pct = max_drawdown_pct([point.value for point in recent])

    expected_daily_std_pct = float(
        paper_realized.get("daily_std_pct") or paper_run.get("daily_std_pct") or 0.0
    )
    expected_rolling_7d_dd_p95_pct = float(
        paper_realized.get("rolling_7d_dd_p95_pct")
        or paper_run.get("rolling_7d_dd_p95_pct")
        or 0.0
    )

    flags: list[dict[str, Any]] = []
    # Require 4+ of the last 5 days down. The old ">2" (3 of 5) fired on ~31% of
    # normal weeks by chance — pure noise for a momentum strategy that routinely
    # has mixed down-days. 4 of 5 is a genuine losing bias (~19% by chance), and
    # magnitude is already covered by the rolling-drawdown and daily-move rules.
    if len(negative_recent_returns) >= 4:
        flags.append(
            {
                "rule": "losses_at_least_4_of_last_5",
                "observed": len(negative_recent_returns),
                "threshold": 4,
            }
        )
    if expected_rolling_7d_dd_p95_pct < 0 and rolling_7d_drawdown_pct < expected_rolling_7d_dd_p95_pct:
        flags.append(
            {
                "rule": "rolling_7d_drawdown_below_p95",
                "observed_pct": round(rolling_7d_drawdown_pct, 4),
                "threshold_pct": expected_rolling_7d_dd_p95_pct,
            }
        )
    if (
        last_day_return_pct is not None
        and expected_daily_std_pct > 0
        and abs(last_day_return_pct) > expected_daily_std_pct * 1.5
    ):
        flags.append(
            {
                "rule": "daily_move_gt_1_5x_expected_std",
                "observed_pct": round(last_day_return_pct, 4),
                "threshold_pct": round(expected_daily_std_pct * 1.5, 4),
            }
        )

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "divergence" if flags else "ok",
        "last_trading_day": last.date,
        "previous_trading_day": previous.date,
        "last_value": last.value,
        "previous_value": previous.value,
        "last_day_pnl": last_day_pnl,
        "last_day_return_pct": round(last_day_return_pct or 0.0, 4),
        "rolling_7d_pnl": rolling_7d_pnl,
        "rolling_7d_return_pct": round(rolling_7d_return_pct or 0.0, 4),
        "rolling_7d_drawdown_pct": round(rolling_7d_drawdown_pct, 4),
        "expected_daily_std_pct": expected_daily_std_pct,
        "expected_rolling_7d_dd_p95_pct": expected_rolling_7d_dd_p95_pct,
        "flags": flags,
    }
    append_memory(record)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
