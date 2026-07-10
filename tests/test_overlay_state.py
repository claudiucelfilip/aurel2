"""Cap state tests: 21d accelerate_entry, 30d+2/quarter lookback override, 14d defensive contest."""

from datetime import date

from aurel2.overlay.state import (
    OverlayState,
    can_accelerate_entry,
    can_activate_lookback_override,
    can_force_defensive_contest,
    clear_lookback_override,
    get_active_lookback_override,
    load_state,
    record_accelerate_entry,
    record_force_defensive_contest,
    record_lookback_activation,
    save_state,
)


class TestAccelerateEntryCap:
    def test_allowed_when_never_used(self):
        state = OverlayState()
        assert can_accelerate_entry(state, date(2026, 7, 14)) is True

    def test_blocked_within_21_days(self):
        state = OverlayState()
        record_accelerate_entry(state, date(2026, 7, 1))
        assert can_accelerate_entry(state, date(2026, 7, 15)) is False  # 14 days later

    def test_allowed_exactly_at_21_days(self):
        state = OverlayState()
        record_accelerate_entry(state, date(2026, 7, 1))
        assert can_accelerate_entry(state, date(2026, 7, 22)) is True  # exactly 21 days

    def test_allowed_after_21_days(self):
        state = OverlayState()
        record_accelerate_entry(state, date(2026, 7, 1))
        assert can_accelerate_entry(state, date(2026, 7, 23)) is True


class TestForceDefensiveContestCap:
    def test_allowed_when_never_used(self):
        state = OverlayState()
        assert can_force_defensive_contest(state, date(2026, 7, 14)) is True

    def test_blocked_within_14_days(self):
        state = OverlayState()
        record_force_defensive_contest(state, date(2026, 7, 1))
        assert can_force_defensive_contest(state, date(2026, 7, 10)) is False

    def test_allowed_exactly_at_14_days(self):
        state = OverlayState()
        record_force_defensive_contest(state, date(2026, 7, 1))
        assert can_force_defensive_contest(state, date(2026, 7, 15)) is True


class TestLookbackOverrideCap:
    def test_allowed_when_never_used(self):
        state = OverlayState()
        assert can_activate_lookback_override(state, date(2026, 1, 10)) is True

    def test_blocked_while_active(self):
        state = OverlayState()
        record_lookback_activation(state, date(2026, 1, 10), 3)
        assert can_activate_lookback_override(state, date(2026, 1, 15)) is False

    def test_active_override_visible(self):
        state = OverlayState()
        record_lookback_activation(state, date(2026, 1, 10), 6)
        assert get_active_lookback_override(state, date(2026, 1, 20)) == 6

    def test_auto_reverts_after_30_trading_days(self):
        state = OverlayState()
        record_lookback_activation(state, date(2026, 1, 1), 3)
        # 31 calendar days later -> expired
        assert get_active_lookback_override(state, date(2026, 2, 1)) is None
        assert state.active_lookback_override is None

    def test_still_active_at_30_days(self):
        state = OverlayState()
        record_lookback_activation(state, date(2026, 1, 1), 3)
        assert get_active_lookback_override(state, date(2026, 1, 31)) == 3

    def test_clear_lookback_override(self):
        state = OverlayState()
        record_lookback_activation(state, date(2026, 1, 10), 3)
        clear_lookback_override(state)
        assert state.active_lookback_override is None
        assert can_activate_lookback_override(state, date(2026, 1, 11)) is True

    def test_max_2_activations_per_quarter(self):
        state = OverlayState()
        record_lookback_activation(state, date(2026, 1, 1), 3)
        clear_lookback_override(state)
        record_lookback_activation(state, date(2026, 2, 1), 6)
        clear_lookback_override(state)
        # third activation attempt in same quarter (Q1 2026) should be blocked
        assert can_activate_lookback_override(state, date(2026, 3, 1)) is False

    def test_new_quarter_resets_cap(self):
        state = OverlayState()
        record_lookback_activation(state, date(2026, 1, 1), 3)
        clear_lookback_override(state)
        record_lookback_activation(state, date(2026, 2, 1), 6)
        clear_lookback_override(state)
        assert can_activate_lookback_override(state, date(2026, 4, 1)) is True  # Q2


class TestStatePersistence:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "overlay_state.json")
        state = OverlayState()
        record_accelerate_entry(state, date(2026, 7, 1))
        record_lookback_activation(state, date(2026, 7, 1), 3)
        save_state(path, state)

        loaded = load_state(path)
        assert loaded.last_accelerate_entry_date == "2026-07-01"
        assert loaded.active_lookback_override == {"started": "2026-07-01", "months": 3, "quarter": "2026Q3"}

    def test_missing_file_returns_default(self, tmp_path):
        path = str(tmp_path / "does_not_exist.json")
        state = load_state(path)
        assert state.last_accelerate_entry_date is None
        assert state.active_lookback_override is None

    def test_corrupt_file_returns_default(self, tmp_path):
        path = tmp_path / "overlay_state.json"
        path.write_text("{not json")
        state = load_state(str(path))
        assert state.last_accelerate_entry_date is None
