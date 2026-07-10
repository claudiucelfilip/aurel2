"""End-to-end integration seam test: tilt file on disk -> apply_overlay -> outcome.

Exercises run_overlay_for_decision (the function checker.py/backtest.py both
call), covering the hard-no-op path and an applied accelerate_entry path.
"""

import json
from datetime import date, timedelta

import pandas as pd
import pytest

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, SignalAction
from aurel2.overlay.integration import run_overlay_for_decision
from aurel2.overlay.schema import tilt_path_for_mode
from aurel2.overlay.state import state_path_for_mode


@pytest.fixture
def accelerate_enabled(monkeypatch):
    """Enable power 1 for tests of its logic; it ships disabled for the
    parallel run (shadow-logged) per the 2026-07-10 replay finding."""
    from dataclasses import replace

    import aurel2.overlay.powers as powers_mod

    cfg = replace(
        powers_mod.CANONICAL_CONFIG,
        overlay=replace(powers_mod.CANONICAL_CONFIG.overlay, accelerate_entry_enabled=True),
    )
    monkeypatch.setattr(powers_mod, "CANONICAL_CONFIG", cfg)


DM_ASSETS = {
    AssetClass.TECH_SECTOR: ASSET_REGISTRY[AssetClass.TECH_SECTOR],
    AssetClass.US_STOCKS: ASSET_REGISTRY[AssetClass.US_STOCKS],
    AssetClass.CASH: ASSET_REGISTRY[AssetClass.CASH],
}


def _price_series(symbol, start, days, start_price, daily_growth):
    rows = []
    price = start_price
    d = start
    for _ in range(days):
        rows.append({"date": d.isoformat(), "symbol": symbol, "close": price})
        price *= 1 + daily_growth
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def build_prices(calc_date, symbol_growth, lookback_days=400):
    start = calc_date - timedelta(days=lookback_days)
    frames = [_price_series(sym, start, lookback_days, 100.0, g) for sym, g in symbol_growth.items()]
    return pd.concat(frames, ignore_index=True)


class TestIntegrationSeam:
    def test_no_tilt_file_is_hard_noop(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})

        outcome = run_overlay_for_decision(
            mode="paper", today=calc_date, prices=prices, dm_assets=DM_ASSETS, current_holding_symbol="SPY"
        )
        assert outcome.action is None
        assert outcome.journal_rows == []
        # state file created (empty) but tilt file was never written by this test
        assert not (tmp_path / "data" / "paper" / "overlay_tilt.json").exists()

    def test_malformed_tilt_file_is_hard_noop(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})

        tilt_path = tmp_path / tilt_path_for_mode("paper")
        tilt_path.parent.mkdir(parents=True, exist_ok=True)
        tilt_path.write_text("{not valid json")

        outcome = run_overlay_for_decision(
            mode="paper", today=calc_date, prices=prices, dm_assets=DM_ASSETS, current_holding_symbol="SPY"
        )
        assert outcome.action is None
        assert outcome.journal_rows == []

    def test_expired_tilt_file_is_hard_noop(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})

        tilt_path = tmp_path / tilt_path_for_mode("paper")
        tilt_path.parent.mkdir(parents=True, exist_ok=True)
        tilt_path.write_text(json.dumps({
            "as_of": "2026-06-01", "expires": "2026-06-08", "regime_view": "risk_on",
            "confidence": 0.8,
            "powers": {"accelerate_entry": {"symbol": "XLK"}, "lookback_override_months": None, "force_defensive_contest": False},
            "reasoning": "stale", "samples": 5, "sample_agreement": 1.0,
        }))

        outcome = run_overlay_for_decision(
            mode="paper", today=calc_date, prices=prices, dm_assets=DM_ASSETS, current_holding_symbol="SPY"
        )
        assert outcome.action is None
        assert outcome.journal_rows == []

    def test_valid_accelerate_entry_tilt_overrides_decision(self, tmp_path, monkeypatch, accelerate_enabled):
        monkeypatch.chdir(tmp_path)
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})

        tilt_path = tmp_path / tilt_path_for_mode("paper")
        tilt_path.parent.mkdir(parents=True, exist_ok=True)
        tilt_path.write_text(json.dumps({
            "as_of": "2026-07-14", "expires": "2026-07-21", "regime_view": "risk_on",
            "confidence": 0.8,
            "powers": {"accelerate_entry": {"symbol": "XLK"}, "lookback_override_months": None, "force_defensive_contest": False},
            "reasoning": "strong trend", "samples": 5, "sample_agreement": 1.0,
        }))

        outcome = run_overlay_for_decision(
            mode="paper", today=calc_date, prices=prices, dm_assets=DM_ASSETS, current_holding_symbol="SPY"
        )
        assert outcome.action == SignalAction.BUY
        assert outcome.asset_symbol == "XLK"
        assert len(outcome.journal_rows) == 1
        assert outcome.journal_rows[0]["power"] == "accelerate_entry"
        assert outcome.journal_rows[0]["status"] == "applied"

        # State persisted for next call's cap check
        state_file = tmp_path / state_path_for_mode("paper")
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["last_accelerate_entry_date"] == "2026-07-14"

    def test_second_call_within_cooldown_is_ignored(self, tmp_path, monkeypatch, accelerate_enabled):
        monkeypatch.chdir(tmp_path)
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})

        tilt_path = tmp_path / tilt_path_for_mode("paper")
        tilt_path.parent.mkdir(parents=True, exist_ok=True)
        tilt_content = {
            "as_of": "2026-07-14", "expires": "2026-07-28", "regime_view": "risk_on",
            "confidence": 0.8,
            "powers": {"accelerate_entry": {"symbol": "XLK"}, "lookback_override_months": None, "force_defensive_contest": False},
            "reasoning": "strong trend", "samples": 5, "sample_agreement": 1.0,
        }
        tilt_path.write_text(json.dumps(tilt_content))

        run_overlay_for_decision(
            mode="paper", today=calc_date, prices=prices, dm_assets=DM_ASSETS, current_holding_symbol="SPY"
        )

        # Second decision 5 days later, still holding SPY (accel didn't "execute" in this test,
        # but cap tracking only cares about the overlay having fired once already).
        second_date = calc_date + timedelta(days=5)
        prices2 = build_prices(second_date, {"XLK": 0.01, "SPY": 0.001})
        outcome2 = run_overlay_for_decision(
            mode="paper", today=second_date, prices=prices2, dm_assets=DM_ASSETS, current_holding_symbol="SPY"
        )
        assert outcome2.action is None
        assert outcome2.journal_rows[0]["status"] == "ignored"
        assert "over cap" in outcome2.journal_rows[0]["reason"]
