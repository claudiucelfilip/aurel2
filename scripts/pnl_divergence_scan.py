#!/usr/bin/env python3
"""Read-only Aurel2 paper PnL divergence scan.

This replaces brittle cron-time jq generation. It reads the paper trade journal
from the running container, compares recent realized movement to the latest
expectation band file, and writes a compact JSONL heartbeat record.
"""

from __future__ import annotations

import json
import os
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
SWITCH_THRESHOLD_PCT_POINTS = 2.0


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


def latest_journal_entry(entries: list[dict[str, Any]]) -> dict[str, Any]:
    for entry in reversed(entries):
        if isinstance(entry, dict) and entry.get("account_value_before") is not None:
            return entry
    return {}


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct_change(start: float, end: float) -> float | None:
    if start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def score_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 100.0, 4)


def build_momentum_context(entry: dict[str, Any]) -> dict[str, Any]:
    signals = entry.get("strategy_signals") or {}
    dual_momentum = signals.get("dual_momentum") or {}
    raw_scores = dual_momentum.get("momentum_scores") or {}
    scores: dict[str, float] = {}
    if isinstance(raw_scores, dict):
        for symbol, value in raw_scores.items():
            parsed = safe_float(value)
            if parsed is not None:
                scores[str(symbol)] = parsed

    current_holding = (
        entry.get("current_holding_before")
        or entry.get("current_holding_symbol")
        or entry.get("current_holding")
    )
    current_holding = str(current_holding) if current_holding else None
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_symbol, top_score = ranked[0] if ranked else (None, None)

    current_score = scores.get(current_holding) if current_holding else None
    next_best_symbol = None
    next_best_score = None
    for symbol, value in ranked:
        if symbol != current_holding:
            next_best_symbol = symbol
            next_best_score = value
            break

    lead_pct_points = None
    if current_score is not None and next_best_score is not None:
        lead_pct_points = round((current_score - next_best_score) * 100.0, 4)

    if current_holding and top_symbol == current_holding:
        verdict = "current_holding_is_momentum_leader"
    elif current_holding and current_score is not None:
        verdict = "current_holding_is_not_momentum_leader"
    else:
        verdict = "momentum_context_unavailable"

    return {
        "current_holding": current_holding,
        "current_score_pct": score_pct(current_score),
        "top_symbol": top_symbol,
        "top_score_pct": score_pct(top_score),
        "next_best_symbol": next_best_symbol,
        "next_best_score_pct": score_pct(next_best_score),
        "lead_pct_points": lead_pct_points,
        "switch_threshold_pct_points": SWITCH_THRESHOLD_PCT_POINTS,
        "verdict": verdict,
    }


def recent_account_values(points: list[Point]) -> list[dict[str, Any]]:
    return [{"date": point.date, "value": round(point.value, 2)} for point in points]


def recent_account_returns(points: list[Point]) -> list[dict[str, Any]]:
    returns: list[dict[str, Any]] = []
    for previous, current in zip(points, points[1:]):
        change_pct = pct_change(previous.value, current.value)
        returns.append(
            {
                "date": current.date,
                "pnl": round(current.value - previous.value, 2),
                "return_pct": round(change_pct, 4) if change_pct is not None else None,
            }
        )
    return returns


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


def build_operator_summary(
    status: str,
    current_holding: str | None,
    latest_action: str | None,
    loss_streak_sessions: int,
    rolling_7d_pnl: float,
    rolling_7d_drawdown_pct: float,
    momentum_context: dict[str, Any],
) -> str:
    holding = current_holding or "unknown holding"
    action = latest_action or "unknown action"
    momentum_verdict = momentum_context.get("verdict")
    if momentum_verdict == "current_holding_is_momentum_leader":
        next_best = momentum_context.get("next_best_symbol") or "next best asset"
        lead = momentum_context.get("lead_pct_points")
        momentum_text = (
            f"{holding} is still the 12-month momentum leader, ahead of {next_best}"
            f" by {lead} percentage points."
        )
    elif momentum_verdict == "current_holding_is_not_momentum_leader":
        top = momentum_context.get("top_symbol") or "another asset"
        momentum_text = f"{holding} is no longer the top 12-month momentum asset; {top} is ahead."
    else:
        momentum_text = "Momentum context was not available in the latest journal entry."

    if status == "divergence":
        return (
            f"Aurel2 paper PnL divergence: latest action is {action} while holding {holding}. "
            f"The scan saw {loss_streak_sessions} losing sessions in the last 5, "
            f"rolling 7-day PnL {rolling_7d_pnl}, and rolling drawdown "
            f"{round(rolling_7d_drawdown_pct, 4)}%. {momentum_text} "
            "This scan checks account PnL only; it does not by itself prove an execution outage."
        )
    return (
        f"Aurel2 paper PnL scan is OK: latest action is {action} while holding {holding}. "
        f"Rolling 7-day PnL is {rolling_7d_pnl}. {momentum_text}"
    )


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
    latest_entry = latest_journal_entry(raw_entries)
    current_holding = (
        latest_entry.get("current_holding_before")
        or latest_entry.get("current_holding_symbol")
        or latest_entry.get("current_holding")
    )
    current_holding = str(current_holding) if current_holding else None
    latest_action = latest_entry.get("action")
    latest_action = str(latest_action) if latest_action else None
    decision_symbol = latest_entry.get("decision_symbol") or latest_entry.get("symbol")
    decision_symbol = str(decision_symbol) if decision_symbol else None
    momentum_context = build_momentum_context(latest_entry)

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
        "bot": "Aurel2",
        "mode": "paper",
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "divergence" if flags else "ok",
        "alert_kind": "strategy_pnl_loss" if flags else "none",
        "scope": "paper account PnL scan only; not a container or execution health check",
        "last_trading_day": last.date,
        "previous_trading_day": previous.date,
        "last_value": last.value,
        "previous_value": previous.value,
        "last_day_pnl": last_day_pnl,
        "last_day_return_pct": round(last_day_return_pct or 0.0, 4),
        "current_holding": current_holding,
        "latest_action": latest_action,
        "decision_symbol": decision_symbol,
        "loss_streak_sessions": len(negative_recent_returns),
        "recent_account_values": recent_account_values(recent),
        "recent_account_returns": recent_account_returns(recent),
        "rolling_7d_pnl": rolling_7d_pnl,
        "rolling_7d_return_pct": round(rolling_7d_return_pct or 0.0, 4),
        "rolling_7d_drawdown_pct": round(rolling_7d_drawdown_pct, 4),
        "expected_daily_std_pct": expected_daily_std_pct,
        "expected_rolling_7d_dd_p95_pct": expected_rolling_7d_dd_p95_pct,
        "momentum_context": momentum_context,
        "flags": flags,
    }
    record["operator_summary"] = build_operator_summary(
        status=record["status"],
        current_holding=current_holding,
        latest_action=latest_action,
        loss_streak_sessions=len(negative_recent_returns),
        rolling_7d_pnl=rolling_7d_pnl,
        rolling_7d_drawdown_pct=rolling_7d_drawdown_pct,
        momentum_context=momentum_context,
    )
    if os.environ.get("AUREL2_PNL_SCAN_NO_APPEND") != "1":
        append_memory(record)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
