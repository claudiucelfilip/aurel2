"""Per-arm daily equity series construction for the weekly scorecard.

Exact rules: docs/plans/2026-07-10-graduation-rule.md section 1.

- live_trader_daily_series: reads live-trader's trades.jsonl (READ-ONLY,
  /Users/claudiu/.openclaw/workspace/live-trader/trades.jsonl in production;
  path is always an argument here so tests never touch the real file).
- paper_journal_daily_series: reads A2's data/{mode}/trade_journal.json for
  the A2+overlay arm's account-value history.
- overlay_accounting: pulls overlay_activity rows out of the same journal
  for the scorecard's overlay-intervention reporting.

A2-bare (shadow replay via BacktestEngine) and QQQ (CachedPriceProvider) are
NOT here -- they don't parse an on-disk journal, they call existing engine/
provider code directly from scripts/weekly_scorecard.py.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

LARGE_GAP_TRADING_DAYS = 5
MIN_COVERAGE_FRACTION = 0.80


def _daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _parse_ts_date(ts: str) -> Optional[date]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def live_trader_daily_series(
    path: Path, start: date, end: date
) -> tuple[list[tuple[date, float]], bool]:
    """Parse live-trader's trades.jsonl into a forward-filled daily series.

    Returns (series, data_gap_flag). data_gap_flag is True if the file is
    missing, has zero usable rows in range, has any gap > LARGE_GAP_TRADING_DAYS
    consecutive days, or covers < MIN_COVERAGE_FRACTION of the window.
    """
    if not path.exists():
        return [], True

    by_day_ts: dict[date, tuple[float, str]] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = record.get("portfolio_value")
            if value is None:
                continue
            day = _parse_ts_date(record.get("timestamp", ""))
            if day is None or day < start or day > end:
                continue
            # File is not chronologically ordered (broker_reconcile backfills
            # interleave); track the latest intraday timestamp seen per day.
            ts = record.get("timestamp", "")
            existing_ts = by_day_ts.get(day, (None, None))[1]
            if existing_ts is None or ts > existing_ts:
                by_day_ts[day] = (float(value), ts)

    if not by_day_ts:
        return [], True

    by_day = {day: value for day, (value, _ts) in by_day_ts.items()}

    all_days = _daterange(start, end)
    series: list[tuple[date, float]] = []
    last_value: Optional[float] = None
    max_gap = 0
    current_gap = 0
    covered_days = 0

    for day in all_days:
        if day in by_day:
            last_value = by_day[day]
            covered_days += 1
            current_gap = 0
        else:
            current_gap += 1
            max_gap = max(max_gap, current_gap)

        if last_value is not None:
            series.append((day, last_value))

    coverage = covered_days / len(all_days) if all_days else 0.0
    gap_flag = max_gap > LARGE_GAP_TRADING_DAYS or coverage < MIN_COVERAGE_FRACTION

    return series, gap_flag


def paper_journal_daily_series(path: Path, start: date, end: date) -> list[tuple[date, float]]:
    """Build A2+overlay's daily equity series from data/{mode}/trade_journal.json.

    account_value_after preferred, falling back to account_value_before.
    Gaps forward-fill from the prior day's value.
    """
    if not path.exists():
        return []

    try:
        entries = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    by_day: dict[date, float] = {}
    for entry in entries:
        if entry.get("entry_type") != "decision":
            continue
        day = _parse_ts_date(entry.get("timestamp", ""))
        if day is None or day < start or day > end:
            continue
        value = entry.get("account_value_after")
        if value is None:
            value = entry.get("account_value_before")
        if value is None:
            continue
        by_day[day] = float(value)

    if not by_day:
        return []

    series: list[tuple[date, float]] = []
    last_value: Optional[float] = None
    for day in _daterange(start, end):
        if day in by_day:
            last_value = by_day[day]
        if last_value is not None:
            series.append((day, last_value))

    return series


def overlay_accounting(path: Path, start: date, end: date) -> dict:
    """Pull overlay_activity rows from the A2 journal for the run window.

    Returns {"applied_count", "ignored_count", "activity"}. Degrades to
    all-zero on a missing/unreadable file -- never raises.
    """
    empty = {"applied_count": 0, "ignored_count": 0, "activity": []}
    if not path.exists():
        return empty

    try:
        entries = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return empty

    activity: list[dict] = []
    applied = 0
    ignored = 0
    for entry in entries:
        if entry.get("entry_type") != "decision":
            continue
        day = _parse_ts_date(entry.get("timestamp", ""))
        if day is None or day < start or day > end:
            continue
        for row in entry.get("overlay_activity", []) or []:
            activity.append({**row, "as_of": day.isoformat()})
            if row.get("status") == "applied":
                applied += 1
            elif row.get("status") == "ignored":
                ignored += 1

    return {"applied_count": applied, "ignored_count": ignored, "activity": activity}
