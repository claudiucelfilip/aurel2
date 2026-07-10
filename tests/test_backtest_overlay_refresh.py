"""Tests for BacktestEngine's optional overlay_refresh hook.

Mirrors live's external weekly cron that regenerates data/{mode}/overlay_tilt.json
before the checker reads it (docs/plans/2026-07-10-ai-overlay-design.md, "Decision
protocol" cadence). Backtest has no cron, so scripts/overlay_replay.py (Track D)
passes a callable that does the same regeneration inline, once per rebalance date.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from aurel2.engine.backtest import BacktestEngine


def _flat_prices(symbols, start, days=500, price=100.0):
    rows = []
    d = start
    for _ in range(days):
        for sym in symbols:
            rows.append({"date": d.isoformat(), "symbol": sym, "close": price})
        d += timedelta(days=1)
    return pd.DataFrame(rows)


UNIVERSE = ["SPY", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "AGG", "SHY", "IEF", "TIP", "VNQ", "IJS", "GLD", "DBC"]


class TestOverlayRefreshHook:
    def test_not_called_when_overlay_disabled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        start = date(2026, 1, 1)
        prices = _flat_prices(UNIVERSE, start - timedelta(days=400))

        calls = []
        engine = BacktestEngine(overlay_enabled=False, overlay_refresh=lambda d, p: calls.append(d))
        engine.run(prices=prices, start_date=start, end_date=start + timedelta(days=60), frequency="monthly")

        assert calls == []

    def test_called_once_per_rebalance_date_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        start = date(2026, 1, 1)
        end = start + timedelta(days=90)
        prices = _flat_prices(UNIVERSE, start - timedelta(days=400))

        calls = []
        engine = BacktestEngine(
            overlay_enabled=True,
            overlay_mode="replay_test",
            overlay_refresh=lambda d, p: calls.append(d),
        )
        engine.run(prices=prices, start_date=start, end_date=end, frequency="monthly")

        rebalance_dates = engine.dual_momentum.get_rebalance_dates(start, end, "monthly")
        assert calls == rebalance_dates

    def test_refresh_receives_prices_frame(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        start = date(2026, 1, 1)
        prices = _flat_prices(UNIVERSE, start - timedelta(days=400))

        seen_frames = []
        engine = BacktestEngine(
            overlay_enabled=True,
            overlay_mode="replay_test",
            overlay_refresh=lambda d, p: seen_frames.append(len(p)),
        )
        engine.run(prices=prices, start_date=start, end_date=start + timedelta(days=40), frequency="monthly")

        assert len(seen_frames) >= 1
        assert all(n == len(prices) for n in seen_frames)

    def test_refresh_exception_does_not_break_backtest(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        start = date(2026, 1, 1)
        prices = _flat_prices(UNIVERSE, start - timedelta(days=400))

        def boom(d, p):
            raise RuntimeError("simulated refresh failure")

        engine = BacktestEngine(overlay_enabled=True, overlay_mode="replay_test", overlay_refresh=boom)
        result = engine.run(prices=prices, start_date=start, end_date=start + timedelta(days=60), frequency="monthly")

        assert result.snapshots  # backtest completed despite the refresh hook raising

    def test_default_is_none_and_backtest_runs_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        start = date(2026, 1, 1)
        prices = _flat_prices(UNIVERSE, start - timedelta(days=400))

        engine = BacktestEngine()
        assert engine.overlay_refresh is None
        result = engine.run(prices=prices, start_date=start, end_date=start + timedelta(days=60), frequency="monthly")
        assert result.snapshots
