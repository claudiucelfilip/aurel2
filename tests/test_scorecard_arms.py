"""Tests for scorecard arm-series construction: parsing live-trader's
trades.jsonl, the A2 trade journal, and overlay accounting from journal rows.
All I/O against temp files -- no real live-trader or paper data touched.
"""

import json
from datetime import date

from aurel2.scorecard.arms import (
    live_trader_daily_series,
    overlay_accounting,
    paper_journal_daily_series,
)


def write_jsonl(path, records):
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TestLiveTraderDailySeries:
    def test_takes_last_value_per_day(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        write_jsonl(p, [
            {"timestamp": "2026-02-17T10:00:00Z", "portfolio_value": 100.0},
            {"timestamp": "2026-02-17T15:00:00Z", "portfolio_value": 105.0},
            {"timestamp": "2026-02-18T10:00:00Z", "portfolio_value": 110.0},
        ])
        series, gap_flag = live_trader_daily_series(p, date(2026, 2, 17), date(2026, 2, 18))
        assert series == [(date(2026, 2, 17), 105.0), (date(2026, 2, 18), 110.0)]
        assert gap_flag is False

    def test_skips_entries_without_portfolio_value(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        write_jsonl(p, [
            {"timestamp": "2026-02-17T10:00:00Z", "action": "HOLD"},
            {"timestamp": "2026-02-18T10:00:00Z", "portfolio_value": 110.0},
        ])
        series, _ = live_trader_daily_series(p, date(2026, 2, 17), date(2026, 2, 18))
        assert series == [(date(2026, 2, 18), 110.0)]

    def test_skips_unparseable_lines(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        p.write_text(
            '{"timestamp": "2026-02-17T10:00:00Z", "portfolio_value": 100.0}\n'
            "not json at all\n"
            '{"timestamp": "2026-02-18T10:00:00Z", "portfolio_value": 110.0}\n'
        )
        series, _ = live_trader_daily_series(p, date(2026, 2, 17), date(2026, 2, 18))
        assert len(series) == 2

    def test_handles_out_of_order_lines(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        write_jsonl(p, [
            {"timestamp": "2026-02-19T10:00:00Z", "portfolio_value": 120.0},
            {"timestamp": "2026-02-17T10:00:00Z", "portfolio_value": 100.0},
            {"timestamp": "2026-02-18T10:00:00Z", "portfolio_value": 110.0},
        ])
        series, _ = live_trader_daily_series(p, date(2026, 2, 17), date(2026, 2, 19))
        assert [d for d, _ in series] == [date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19)]

    def test_forward_fills_gap_and_flags_small_gap_as_ok(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        write_jsonl(p, [
            {"timestamp": "2026-02-16T10:00:00Z", "portfolio_value": 100.0},
            {"timestamp": "2026-02-20T10:00:00Z", "portfolio_value": 108.0},
        ])
        series, gap_flag = live_trader_daily_series(p, date(2026, 2, 16), date(2026, 2, 20))
        by_date = dict(series)
        assert by_date[date(2026, 2, 17)] == 100.0
        assert by_date[date(2026, 2, 18)] == 100.0
        assert by_date[date(2026, 2, 20)] == 108.0

    def test_flags_large_gap(self, tmp_path):
        p = tmp_path / "trades.jsonl"
        write_jsonl(p, [
            {"timestamp": "2026-02-01T10:00:00Z", "portfolio_value": 100.0},
            {"timestamp": "2026-02-20T10:00:00Z", "portfolio_value": 108.0},
        ])
        series, gap_flag = live_trader_daily_series(p, date(2026, 2, 1), date(2026, 2, 20))
        assert gap_flag is True

    def test_missing_file_returns_empty_and_gap_flag(self, tmp_path):
        p = tmp_path / "does_not_exist.jsonl"
        series, gap_flag = live_trader_daily_series(p, date(2026, 2, 1), date(2026, 2, 20))
        assert series == []
        assert gap_flag is True


class TestPaperJournalDailySeries:
    def test_uses_account_value_after_then_before(self, tmp_path):
        entries = [
            {
                "id": "1", "timestamp": "2026-02-17T16:00:00", "entry_type": "decision",
                "account_value_before": 100.0, "account_value_after": 101.0,
            },
            {
                "id": "2", "timestamp": "2026-02-18T16:00:00", "entry_type": "decision",
                "account_value_before": 102.0, "account_value_after": None,
            },
        ]
        p = tmp_path / "trade_journal.json"
        p.write_text(json.dumps(entries))
        series = paper_journal_daily_series(p, date(2026, 2, 17), date(2026, 2, 18))
        by_date = dict(series)
        assert by_date[date(2026, 2, 17)] == 101.0
        assert by_date[date(2026, 2, 18)] == 102.0

    def test_forward_fills_missing_days(self, tmp_path):
        entries = [
            {
                "id": "1", "timestamp": "2026-02-17T16:00:00", "entry_type": "decision",
                "account_value_before": 100.0, "account_value_after": 100.0,
            },
        ]
        p = tmp_path / "trade_journal.json"
        p.write_text(json.dumps(entries))
        series = paper_journal_daily_series(p, date(2026, 2, 17), date(2026, 2, 19))
        by_date = dict(series)
        assert by_date[date(2026, 2, 18)] == 100.0
        assert by_date[date(2026, 2, 19)] == 100.0

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "does_not_exist.json"
        series = paper_journal_daily_series(p, date(2026, 2, 17), date(2026, 2, 19))
        assert series == []


class TestOverlayAccounting:
    def test_counts_applied_and_ignored(self, tmp_path):
        entries = [
            {
                "id": "1", "timestamp": "2026-02-17T16:00:00", "entry_type": "decision",
                "overlay_activity": [
                    {"power": "accelerate_entry", "status": "applied", "reason": "ok", "detail": {}},
                    {"power": "force_defensive_contest", "status": "ignored", "reason": "cooldown", "detail": {}},
                ],
            },
            {
                "id": "2", "timestamp": "2026-02-18T16:00:00", "entry_type": "decision",
                "overlay_activity": [],
            },
        ]
        p = tmp_path / "trade_journal.json"
        p.write_text(json.dumps(entries))
        result = overlay_accounting(p, date(2026, 2, 17), date(2026, 2, 18))
        assert result["applied_count"] == 1
        assert result["ignored_count"] == 1
        assert len(result["activity"]) == 2

    def test_no_overlay_activity_gives_zero_counts(self, tmp_path):
        entries = [
            {"id": "1", "timestamp": "2026-02-17T16:00:00", "entry_type": "decision", "overlay_activity": []},
        ]
        p = tmp_path / "trade_journal.json"
        p.write_text(json.dumps(entries))
        result = overlay_accounting(p, date(2026, 2, 17), date(2026, 2, 17))
        assert result["applied_count"] == 0
        assert result["ignored_count"] == 0

    def test_missing_file_degrades_gracefully(self, tmp_path):
        p = tmp_path / "does_not_exist.json"
        result = overlay_accounting(p, date(2026, 2, 17), date(2026, 2, 17))
        assert result["applied_count"] == 0
        assert result["activity"] == []
