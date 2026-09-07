"""Research knobs: top-N accelerate gate and mixed-regime mapping (defaults = launch shape)."""

from dataclasses import replace
from datetime import date

import pytest

import aurel2.overlay.powers as powers_mod
from aurel2.overlay.schema import Tilt
from aurel2.overlay.state import OverlayState


def tilt(**over) -> Tilt:
    base = dict(
        as_of=date(2026, 7, 14), expires=date(2026, 7, 21), regime_view="risk_on", confidence=0.8,
        accelerate_entry_symbol=None, lookback_override_months=None, force_defensive_contest=False,
        reasoning="t", samples=5, sample_agreement=1.0,
    )
    base.update(over)
    return Tilt(**base)


def set_overlay(monkeypatch, **knobs):
    cfg = replace(powers_mod.CANONICAL_CONFIG, overlay=replace(powers_mod.CANONICAL_CONFIG.overlay, **knobs))
    monkeypatch.setattr(powers_mod, "CANONICAL_CONFIG", cfg)


def test_defaults_reproduce_launch_shape():
    o = powers_mod.CANONICAL_CONFIG.overlay
    assert o.accelerate_entry_enabled is False
    assert o.accelerate_entry_candidates == 1
    assert o.mixed_regime_action == "none"


class TestTopNGate:
    @pytest.fixture(autouse=True)
    def ranking(self, monkeypatch):
        monkeypatch.setattr(powers_mod, "project_next_picks", lambda *a, **k: ["XLE", "GLD", "EEM", "SPY"])

    def test_strict_rejects_second_ranked(self, monkeypatch):
        set_overlay(monkeypatch, accelerate_entry_enabled=True, accelerate_entry_candidates=1)
        e = powers_mod.apply_accelerate_entry(tilt(accelerate_entry_symbol="GLD"), OverlayState(), date(2026, 3, 2), None, {}, None)
        assert e.status == "ignored"
        assert "projected next pick" in e.reason

    def test_top3_accepts_second_ranked(self, monkeypatch):
        set_overlay(monkeypatch, accelerate_entry_enabled=True, accelerate_entry_candidates=3)
        e = powers_mod.apply_accelerate_entry(tilt(accelerate_entry_symbol="GLD"), OverlayState(), date(2026, 3, 2), None, {}, None)
        assert e.status == "applied"
        assert e.detail == {"symbol": "GLD", "projected_rank": 2}

    def test_top3_still_rejects_fourth(self, monkeypatch):
        set_overlay(monkeypatch, accelerate_entry_enabled=True, accelerate_entry_candidates=3)
        e = powers_mod.apply_accelerate_entry(tilt(accelerate_entry_symbol="SPY"), OverlayState(), date(2026, 3, 2), None, {}, None)
        assert e.status == "ignored"
        assert "top-3" in e.reason

    def test_disabled_shadow_logs_regardless_of_n(self, monkeypatch):
        set_overlay(monkeypatch, accelerate_entry_enabled=False, accelerate_entry_candidates=3)
        e = powers_mod.apply_accelerate_entry(tilt(accelerate_entry_symbol="GLD"), OverlayState(), date(2026, 3, 2), None, {}, None)
        assert e.status == "ignored" and "shadow" in e.reason


class TestMixedRegimeAction:
    def test_none_is_noop(self, monkeypatch):
        set_overlay(monkeypatch, mixed_regime_action="none")
        t = tilt(regime_view="mixed")
        assert powers_mod.apply_mixed_regime_action(t) is t

    def test_lookback_6m_maps_mixed(self, monkeypatch):
        set_overlay(monkeypatch, mixed_regime_action="lookback_6m")
        assert powers_mod.apply_mixed_regime_action(tilt(regime_view="mixed")).lookback_override_months == 6

    def test_lookback_3m_maps_mixed(self, monkeypatch):
        set_overlay(monkeypatch, mixed_regime_action="lookback_3m")
        assert powers_mod.apply_mixed_regime_action(tilt(regime_view="mixed")).lookback_override_months == 3

    def test_does_not_override_explicit_request(self, monkeypatch):
        set_overlay(monkeypatch, mixed_regime_action="lookback_6m")
        assert powers_mod.apply_mixed_regime_action(tilt(regime_view="mixed", lookback_override_months=3)).lookback_override_months == 3

    def test_ignores_non_mixed_views(self, monkeypatch):
        set_overlay(monkeypatch, mixed_regime_action="lookback_6m")
        for view in ("risk_on", "risk_off"):
            assert powers_mod.apply_mixed_regime_action(tilt(regime_view=view)).lookback_override_months is None

    def test_defensive_contest_maps_mixed(self, monkeypatch):
        set_overlay(monkeypatch, mixed_regime_action="defensive_contest")
        assert powers_mod.apply_mixed_regime_action(tilt(regime_view="mixed")).force_defensive_contest is True


class TestDeriskHeld:
    @pytest.fixture(autouse=True)
    def ranking(self, monkeypatch):
        from types import SimpleNamespace as NS
        from aurel2.core.models import AssetClass as AC
        self.assets = {AC.GOLD: NS(yahoo_symbol="GLD"), AC.ENERGY_SECTOR: NS(yahoo_symbol="XLE"), AC.TECH_SECTOR: NS(yahoo_symbol="XLK"), AC.CASH: NS(yahoo_symbol=None)}
        scores = {AC.GOLD: NS(momentum_12m=0.7), AC.XLE if hasattr(AC, "XLE") else AC.ENERGY_SECTOR: NS(momentum_12m=0.2), AC.TECH_SECTOR: NS(momentum_12m=0.1), AC.CASH: NS(momentum_12m=0.0)}
        monkeypatch.setattr(powers_mod, "calculate_momentum_scores", lambda **k: scores)

    def test_disabled_is_noop(self, monkeypatch):
        set_overlay(monkeypatch, derisk_enabled=False)
        t = tilt(); t.raw = {"derisk_symbols": ["GLD"]}
        assert powers_mod.apply_derisk_held(t, OverlayState(), date(2026, 3, 2), None, self.assets, "GLD") is None

    def test_no_veto_is_noop(self, monkeypatch):
        set_overlay(monkeypatch, derisk_enabled=True)
        assert powers_mod.apply_derisk_held(tilt(), OverlayState(), date(2026, 3, 2), None, self.assets, "GLD") is None

    def test_vetoed_holding_rotates_to_best_non_vetoed(self, monkeypatch):
        set_overlay(monkeypatch, derisk_enabled=True)
        t = tilt(); t.raw = {"derisk_symbols": ["GLD"]}
        e = powers_mod.apply_derisk_held(t, OverlayState(), date(2026, 3, 2), None, self.assets, "GLD")
        assert e.status == "applied" and e.detail["target"] == "XLE" and e.detail["rotation"] is True

    def test_all_vetoed_goes_to_cash(self, monkeypatch):
        set_overlay(monkeypatch, derisk_enabled=True)
        t = tilt(); t.raw = {"derisk_symbols": ["GLD", "XLE", "XLK"]}
        e = powers_mod.apply_derisk_held(t, OverlayState(), date(2026, 3, 2), None, self.assets, "GLD")
        assert e.detail["target"] == "CASH"

    def test_veto_blocks_reentry_while_holding_other(self, monkeypatch):
        set_overlay(monkeypatch, derisk_enabled=True)
        t = tilt(); t.raw = {"derisk_symbols": ["GLD"]}
        e = powers_mod.apply_derisk_held(t, OverlayState(), date(2026, 3, 2), None, self.assets, "XLE")
        assert e.detail["target"] == "XLE" and e.detail["rotation"] is False


    def test_rotate_mode_keeps_holding_within_switch_threshold(self, monkeypatch):
        # XLE (0.2) vs XLK (0.1): gap 0.10 > 0.05 threshold -> would switch; make them close.
        from types import SimpleNamespace as NS
        from aurel2.core.models import AssetClass as AC
        scores = {AC.GOLD: NS(momentum_12m=0.7), AC.ENERGY_SECTOR: NS(momentum_12m=0.12), AC.TECH_SECTOR: NS(momentum_12m=0.10), AC.CASH: NS(momentum_12m=0.0)}
        monkeypatch.setattr(powers_mod, "calculate_momentum_scores", lambda **k: scores)
        set_overlay(monkeypatch, derisk_enabled=True, derisk_mode="rotate")
        t = tilt(); t.raw = {"derisk_symbols": ["GLD"]}
        e = powers_mod.apply_derisk_held(t, OverlayState(), date(2026, 3, 2), None, self.assets, "XLK")
        assert e.detail["target"] == "XLK"  # 0.02 gap < 0.05: stay put

    def test_cash_mode_sells_vetoed_and_stays_out(self, monkeypatch):
        set_overlay(monkeypatch, derisk_enabled=True, derisk_mode="cash")
        t = tilt(); t.raw = {"derisk_symbols": ["GLD"]}
        assert powers_mod.apply_derisk_held(t, OverlayState(), date(2026, 3, 2), None, self.assets, "GLD").detail["target"] == "CASH"
        assert powers_mod.apply_derisk_held(t, OverlayState(), date(2026, 3, 2), None, self.assets, "CASH").detail["target"] == "CASH"
        assert powers_mod.apply_derisk_held(t, OverlayState(), date(2026, 3, 2), None, self.assets, "XLE").detail["target"] == "XLE"
