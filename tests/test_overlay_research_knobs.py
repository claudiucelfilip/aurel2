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
