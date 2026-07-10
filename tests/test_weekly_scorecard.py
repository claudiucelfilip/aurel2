"""Tests for scripts/weekly_scorecard.py's orchestration: build_scorecard
wiring, ntfy summary formatting, graceful degradation when arms have no
data. Network/price-provider and BacktestEngine calls are monkeypatched --
these tests never touch the network or the real live-trader file.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import weekly_scorecard as ws  # noqa: E402


def daily_series(values, start=date(2026, 6, 1)):
    from datetime import timedelta
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


class TestArmMetrics:
    def test_empty_series_reports_gap(self):
        result = ws.arm_metrics([], daily_series([100.0, 101.0]))
        assert result["gap"] is True
        assert result["cumulative_return"] is None

    def test_populated_series_computes_metrics(self):
        arm = daily_series([100.0] * 12 + [110.0] * 5)
        bench = daily_series([100.0] * 17)
        result = ws.arm_metrics(arm, bench)
        assert result["gap"] is False
        assert result["cumulative_return"] == pytest.approx(0.10)
        assert result["alpha_vs_qqq"] == pytest.approx(0.10)


class TestFormatNtfySummary:
    def test_reports_gap_arms_distinctly(self):
        card = {
            "mode": "paper",
            "as_of": "2026-07-10",
            "trading_days_elapsed": 10,
            "arms": {
                "a2_overlay": {"gap": True},
                "live_trader": {"gap": True},
                "a2_bare": {"cumulative_return": 0.05, "max_drawdown": 0.02, "sharpe": 1.2, "gap": False},
                "qqq": {"cumulative_return": 0.03, "max_drawdown": 0.01, "sharpe": 0.9, "gap": False},
            },
            "overlay": {"applied_count": 1, "ignored_count": 2},
        }
        summary = ws.format_ntfy_summary(card)
        assert "A2+overlay: DATA GAP" in summary
        assert "live-trader: DATA GAP" in summary
        assert "A2-bare: ret +5.0%" in summary
        assert "1 applied, 2 ignored" in summary


class TestBuildScorecardDegradesGracefully:
    def test_missing_journal_and_live_trader_file_never_crashes(self, tmp_path, monkeypatch):
        # No trade_journal.json, no live-trader file, price provider fails --
        # every arm should degrade to a gap, not raise.
        monkeypatch.setattr(ws, "LIVE_TRADER_TRADES_PATH", tmp_path / "nonexistent.jsonl")
        monkeypatch.chdir(tmp_path)  # journal_path_for_mode is relative to cwd

        def fail_bare(*a, **k):
            return [], "no price data"

        def fail_qqq(*a, **k):
            return [], "no price data"

        monkeypatch.setattr(ws, "a2_bare_shadow_series", fail_bare)
        monkeypatch.setattr(ws, "qqq_series", fail_qqq)

        card = ws.build_scorecard("paper", date(2026, 6, 1), today=date(2026, 6, 5))

        assert card["arms"]["a2_overlay"]["gap"] is True
        assert card["arms"]["live_trader"]["gap"] is True
        assert card["arms"]["a2_bare"]["gap"] is True
        assert card["arms"]["qqq"]["gap"] is True
        assert card["overlay"]["applied_count"] == 0

    def test_appends_history_and_writes_latest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ws, "LIVE_TRADER_TRADES_PATH", tmp_path / "nonexistent.jsonl")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(ws, "a2_bare_shadow_series", lambda *a, **k: ([], "n/a"))
        monkeypatch.setattr(ws, "qqq_series", lambda *a, **k: ([], "n/a"))

        card = ws.build_scorecard("paper", date(2026, 6, 1), today=date(2026, 6, 5))
        ws.append_history("paper", card)
        ws.write_latest("paper", card)

        history_path = tmp_path / "data" / "paper" / "scorecard_history.jsonl"
        latest_path = tmp_path / "data" / "paper" / "scorecard_latest.json"
        assert history_path.exists()
        assert latest_path.exists()
        assert json.loads(history_path.read_text().strip().splitlines()[-1])["mode"] == "paper"
        assert json.loads(latest_path.read_text())["mode"] == "paper"
