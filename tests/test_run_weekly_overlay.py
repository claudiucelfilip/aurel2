"""Tests for scripts/run_weekly_overlay.py's pure/testable pieces: current
holding lookup from the journal and the event-trigger predicate wrapper.
No network calls, no CLI shell-outs (run_overlay_decision itself is Track
C's and already tested in tests/test_overlay_runner.py).
"""

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import run_weekly_overlay as rwo  # noqa: E402


class TestCurrentHolding:
    def test_no_journal_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        holding, days_held = rwo._current_holding("paper")
        assert holding is None
        assert days_held == 0

    def test_reads_latest_decision_holding(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        journal_dir = tmp_path / "data" / "paper"
        journal_dir.mkdir(parents=True)
        entries = [
            {
                "id": "1", "timestamp": "2026-07-01T16:00:00", "entry_type": "decision",
                "current_holding_after": "QQQ", "current_holding_symbol": "QQQ",
                "executed": True, "current_holding_before": None,
            },
        ]
        (journal_dir / "trade_journal.json").write_text(json.dumps(entries))
        holding, _days_held = rwo._current_holding("paper")
        assert holding == "QQQ"


class TestCheckEventTrigger:
    def test_no_holding_never_triggers(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = rwo.check_event_trigger("paper", date(2026, 7, 9))
        assert result is False

    def test_large_3day_move_triggers(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(rwo, "_current_holding", lambda mode: ("QQQ", 5))

        class FakeProvider:
            def get_prices(self, symbol, start, end):
                dates = pd.date_range(end=end, periods=6, freq="B")
                closes = [100, 100, 100, 108, 108, 108]  # >5% move day 3->4
                return pd.DataFrame({"date": dates, "close": closes, "symbol": symbol})

        monkeypatch.setattr(rwo, "CachedPriceProvider", FakeProvider)
        result = rwo.check_event_trigger("paper", date(2026, 7, 9))
        assert result is True

    def test_small_3day_move_does_not_trigger(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(rwo, "_current_holding", lambda mode: ("QQQ", 5))

        class FakeProvider:
            def get_prices(self, symbol, start, end):
                dates = pd.date_range(end=end, periods=6, freq="B")
                closes = [100, 100, 100, 101, 101, 101]
                return pd.DataFrame({"date": dates, "close": closes, "symbol": symbol})

        monkeypatch.setattr(rwo, "CachedPriceProvider", FakeProvider)
        result = rwo.check_event_trigger("paper", date(2026, 7, 9))
        assert result is False

    def test_empty_price_data_does_not_trigger(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(rwo, "_current_holding", lambda mode: ("QQQ", 5))

        class FakeProvider:
            def get_prices(self, symbol, start, end):
                return pd.DataFrame(columns=["date", "close", "symbol"])

        monkeypatch.setattr(rwo, "CachedPriceProvider", FakeProvider)
        result = rwo.check_event_trigger("paper", date(2026, 7, 9))
        assert result is False
