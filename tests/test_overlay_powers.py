"""Tests for the three transition powers + apply_overlay seam.

Covers: accelerate-entry symbol restriction (only the core's own projected
next pick, never a novel symbol), and each power's cap enforcement end-to-end
through apply_overlay.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass
from aurel2.overlay.powers import (
    apply_overlay,
    project_next_pick,
    run_defensive_contest,
)
from aurel2.overlay.schema import validate_tilt
from aurel2.overlay.state import OverlayState


def _price_series(symbol: str, start: date, days: int, start_price: float, daily_growth: float) -> pd.DataFrame:
    rows = []
    price = start_price
    d = start
    for _ in range(days):
        rows.append({"date": d.isoformat(), "symbol": symbol, "close": price})
        price *= 1 + daily_growth
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def build_prices(calc_date: date, symbol_growth: dict[str, float], lookback_days: int = 400) -> pd.DataFrame:
    start = calc_date - timedelta(days=lookback_days)
    frames = [_price_series(sym, start, lookback_days, 100.0, g) for sym, g in symbol_growth.items()]
    return pd.concat(frames, ignore_index=True)


DM_ASSETS = {
    AssetClass.TECH_SECTOR: ASSET_REGISTRY[AssetClass.TECH_SECTOR],  # XLK
    AssetClass.US_STOCKS: ASSET_REGISTRY[AssetClass.US_STOCKS],  # SPY
    AssetClass.CASH: ASSET_REGISTRY[AssetClass.CASH],
}


def make_tilt(**overrides) -> dict:
    base = {
        "as_of": "2026-07-14",
        "expires": "2026-07-21",
        "regime_view": "risk_on",
        "confidence": 0.8,
        "powers": {
            "accelerate_entry": {"symbol": None},
            "lookback_override_months": None,
            "force_defensive_contest": False,
        },
        "reasoning": "test",
        "samples": 5,
        "sample_agreement": 1.0,
    }
    for k, v in overrides.items():
        if k == "powers":
            base["powers"].update(v)
        else:
            base[k] = v
    return base


class TestProjectNextPick:
    def test_projects_highest_short_term_momentum_symbol(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})  # XLK trending up hard
        projected = project_next_pick(prices, calc_date, DM_ASSETS)
        assert projected == "XLK"

    def test_never_returns_symbol_outside_dm_assets(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001, "QQQ": 0.05})
        projected = project_next_pick(prices, calc_date, DM_ASSETS)
        assert projected in {"XLK", "SPY", None}
        assert projected != "QQQ"  # QQQ isn't in dm_assets even though its price data exists


class TestAccelerateEntryRestriction:
    def test_applies_when_symbol_matches_projected_pick(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})
        tilt = validate_tilt(make_tilt(powers={"accelerate_entry": {"symbol": "XLK"}}))
        state = OverlayState()

        result = apply_overlay(
            tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY"
        )

        applied = [e for e in result.journal_entries if e.power == "accelerate_entry"]
        assert applied[0].status == "applied"
        assert result.decision_overrides == {"action": "buy", "asset_symbol": "XLK"}

    def test_ignored_when_symbol_is_not_core_own_pick(self):
        """The AI can never introduce a novel symbol -- requesting QQQ (not in
        DM's universe / not the projected winner) must be a no-op, journaled."""
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.001, "SPY": 0.01, "QQQ": 0.05})
        tilt = validate_tilt(make_tilt(powers={"accelerate_entry": {"symbol": "QQQ"}}))
        state = OverlayState()

        result = apply_overlay(
            tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY"
        )

        entries = [e for e in result.journal_entries if e.power == "accelerate_entry"]
        assert entries[0].status == "ignored"
        assert "not the core's own projected next pick" in entries[0].reason
        assert result.decision_overrides == {}

    def test_ignored_when_already_holding_symbol(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})
        tilt = validate_tilt(make_tilt(powers={"accelerate_entry": {"symbol": "XLK"}}))
        state = OverlayState()

        result = apply_overlay(
            tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="XLK"
        )
        entries = [e for e in result.journal_entries if e.power == "accelerate_entry"]
        assert entries[0].status == "ignored"

    def test_null_symbol_is_not_journaled(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})
        tilt = validate_tilt(make_tilt())  # accelerate_entry.symbol is None
        state = OverlayState()

        result = apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY")
        assert [e for e in result.journal_entries if e.power == "accelerate_entry"] == []


class TestAccelerateEntryCapInSeam:
    def test_over_cap_is_ignored_and_journaled(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})
        tilt = validate_tilt(make_tilt(powers={"accelerate_entry": {"symbol": "XLK"}}))
        state = OverlayState()
        state.last_accelerate_entry_date = (calc_date - timedelta(days=5)).isoformat()  # within 21d cooldown

        result = apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY")
        entries = [e for e in result.journal_entries if e.power == "accelerate_entry"]
        assert entries[0].status == "ignored"
        assert "over cap" in entries[0].reason
        assert result.decision_overrides == {}

    def test_applying_updates_state_for_next_check(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})
        tilt = validate_tilt(make_tilt(powers={"accelerate_entry": {"symbol": "XLK"}}))
        state = OverlayState()

        apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY")
        assert state.last_accelerate_entry_date == calc_date.isoformat()


class TestLookbackOverrideInSeam:
    def test_activation_applied_and_journaled(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.001, "SPY": 0.001})
        tilt = validate_tilt(
            make_tilt(regime_view="risk_off", powers={"lookback_override_months": 3})
        )
        state = OverlayState()

        result = apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY")
        entries = [e for e in result.journal_entries if e.power == "lookback_override_months"]
        assert entries[0].status == "applied"
        assert result.active_lookback_months == 3
        assert state.active_lookback_override["months"] == 3

    def test_over_cap_two_per_quarter(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.001, "SPY": 0.001})
        state = OverlayState()
        # Pre-seed 2 activations already used this quarter (Q3 2026), no override currently active.
        state.lookback_activations_by_quarter["2026Q3"] = 2

        tilt = validate_tilt(make_tilt(regime_view="risk_off", powers={"lookback_override_months": 6}))
        result = apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY")
        entries = [e for e in result.journal_entries if e.power == "lookback_override_months"]
        assert entries[0].status == "ignored"
        assert "over cap" in entries[0].reason

    def test_auto_reverts_on_risk_on(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.001, "SPY": 0.001})
        state = OverlayState()
        state.active_lookback_override = {"started": "2026-07-01", "months": 3, "quarter": "2026Q3"}

        tilt = validate_tilt(make_tilt(regime_view="risk_on"))
        result = apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY")
        entries = [e for e in result.journal_entries if e.power == "lookback_override_months"]
        assert entries[0].status == "ignored"
        assert "auto-reverted" in entries[0].reason
        assert state.active_lookback_override is None

    def test_auto_reverts_after_30_days_expiry(self):
        calc_date = date(2026, 8, 5)  # >30 days after 2026-07-01
        prices = build_prices(calc_date, {"XLK": 0.001, "SPY": 0.001})
        state = OverlayState()
        state.active_lookback_override = {"started": "2026-07-01", "months": 3, "quarter": "2026Q3"}

        tilt = validate_tilt(make_tilt(as_of="2026-08-05", expires="2026-08-12", regime_view="mixed"))
        result = apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY")
        assert state.active_lookback_override is None


class TestDefensiveContest:
    def test_defensive_wins_and_rotates(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": -0.01, "GLD": 0.01, "AGG": 0.0, "SHY": 0.0, "IEF": 0.0, "TIP": 0.0})
        winner = run_defensive_contest(prices, calc_date, current_holding_symbol="XLK")
        assert winner == "GLD"

    def test_held_asset_wins_no_rotation(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.05, "GLD": 0.0, "AGG": 0.0, "SHY": 0.0, "IEF": 0.0, "TIP": 0.0})
        winner = run_defensive_contest(prices, calc_date, current_holding_symbol="XLK")
        assert winner is None

    def test_full_seam_applies_and_journals(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": -0.01, "GLD": 0.01, "AGG": 0.0, "SHY": 0.0, "IEF": 0.0, "TIP": 0.0})
        tilt = validate_tilt(make_tilt(powers={"force_defensive_contest": True}))
        state = OverlayState()

        result = apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="XLK")
        entries = [e for e in result.journal_entries if e.power == "force_defensive_contest"]
        assert entries[0].status == "applied"
        assert result.decision_overrides == {"action": "buy", "asset_symbol": "GLD"}
        assert state.last_force_defensive_date == calc_date.isoformat()

    def test_over_cap_ignored(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": -0.01, "GLD": 0.01, "AGG": 0.0, "SHY": 0.0, "IEF": 0.0, "TIP": 0.0})
        tilt = validate_tilt(make_tilt(powers={"force_defensive_contest": True}))
        state = OverlayState()
        state.last_force_defensive_date = (calc_date - timedelta(days=3)).isoformat()  # within 14d cooldown

        result = apply_overlay(tilt, state, calc_date, prices, DM_ASSETS, current_holding_symbol="XLK")
        entries = [e for e in result.journal_entries if e.power == "force_defensive_contest"]
        assert entries[0].status == "ignored"
        assert result.decision_overrides == {}


class TestMalformedTiltIsHardNoOp:
    def test_none_tilt_produces_empty_result(self):
        calc_date = date(2026, 7, 14)
        prices = build_prices(calc_date, {"XLK": 0.01, "SPY": 0.001})
        state = OverlayState()
        original_last_accel = state.last_accelerate_entry_date

        result = apply_overlay(None, state, calc_date, prices, DM_ASSETS, current_holding_symbol="SPY")

        assert result.decision_overrides == {}
        assert result.journal_entries == []
        assert state.last_accelerate_entry_date == original_last_accel  # untouched
