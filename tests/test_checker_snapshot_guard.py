from types import SimpleNamespace

from aurel2.live.checker import _broker_snapshot_is_inconsistent


def journal_with_state(value: float = 1083.28, holding: str = "XLK"):
    entry = SimpleNamespace(
        account_value_after=None,
        account_value_before=value,
        current_holding_after=None,
        current_holding_before=holding,
    )
    return SimpleNamespace(entries=[entry])


def test_empty_positions_and_collapsed_equity_are_inconsistent():
    assert _broker_snapshot_is_inconsistent([], 19.84, journal_with_state()) is True


def test_empty_positions_with_stable_equity_are_allowed():
    assert _broker_snapshot_is_inconsistent([], 1080.0, journal_with_state()) is False


def test_present_position_is_allowed_even_when_equity_moved():
    position = SimpleNamespace(symbol="XLK")
    assert _broker_snapshot_is_inconsistent([position], 19.84, journal_with_state()) is False
