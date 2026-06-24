import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pnl_divergence_scan.py"
SPEC = importlib.util.spec_from_file_location("pnl_divergence_scan", MODULE_PATH)
assert SPEC is not None
scan = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = scan
SPEC.loader.exec_module(scan)


def leader_context() -> dict[str, str]:
    return {"verdict": "current_holding_is_momentum_leader"}


def stale_holding_context() -> dict[str, str]:
    return {"verdict": "current_holding_is_not_momentum_leader"}


def test_isolated_daily_loss_is_observation_not_alert_flag():
    flags, observations = scan.build_divergence_flags(
        last_day_return_pct=-3.2796,
        expected_daily_std_pct=1.665,
        rolling_7d_drawdown_pct=-3.2796,
        expected_rolling_7d_dd_p95_pct=-8.0937,
        loss_streak_sessions=3,
        rolling_7d_return_pct=4.164,
        momentum_context=leader_context(),
    )

    assert flags == []
    assert observations == [
        {
            "rule": "daily_loss_gt_1_5x_expected_std",
            "observed_pct": -3.2796,
            "threshold_pct": 2.4975,
            "suppressed": True,
            "reason": "isolated_daily_loss_with_positive_context",
        }
    ]


def test_large_positive_daily_move_does_not_alert():
    flags, observations = scan.build_divergence_flags(
        last_day_return_pct=3.4238,
        expected_daily_std_pct=1.665,
        rolling_7d_drawdown_pct=-3.0,
        expected_rolling_7d_dd_p95_pct=-8.0937,
        loss_streak_sessions=2,
        rolling_7d_return_pct=5.0,
        momentum_context=leader_context(),
    )

    assert flags == []
    assert observations == []


def test_daily_loss_alerts_when_rolling_window_is_negative():
    flags, observations = scan.build_divergence_flags(
        last_day_return_pct=-3.2796,
        expected_daily_std_pct=1.665,
        rolling_7d_drawdown_pct=-3.2796,
        expected_rolling_7d_dd_p95_pct=-8.0937,
        loss_streak_sessions=3,
        rolling_7d_return_pct=-0.5,
        momentum_context=leader_context(),
    )

    assert observations == []
    assert {
        "rule": "daily_loss_gt_1_5x_expected_std",
        "observed_pct": -3.2796,
        "threshold_pct": 2.4975,
    } in flags


def test_daily_loss_alerts_when_holding_is_no_longer_momentum_leader():
    flags, observations = scan.build_divergence_flags(
        last_day_return_pct=-3.2796,
        expected_daily_std_pct=1.665,
        rolling_7d_drawdown_pct=-3.2796,
        expected_rolling_7d_dd_p95_pct=-8.0937,
        loss_streak_sessions=3,
        rolling_7d_return_pct=4.164,
        momentum_context=stale_holding_context(),
    )

    assert observations == []
    assert {
        "rule": "daily_loss_gt_1_5x_expected_std",
        "observed_pct": -3.2796,
        "threshold_pct": 2.4975,
    } in flags


def test_sustained_loss_rules_still_alert():
    flags, observations = scan.build_divergence_flags(
        last_day_return_pct=-0.5,
        expected_daily_std_pct=1.665,
        rolling_7d_drawdown_pct=-9.0,
        expected_rolling_7d_dd_p95_pct=-8.0937,
        loss_streak_sessions=4,
        rolling_7d_return_pct=-4.0,
        momentum_context=leader_context(),
    )

    assert observations == []
    assert {
        "rule": "losses_at_least_4_of_last_5",
        "observed": 4,
        "threshold": 4,
    } in flags
    assert {
        "rule": "rolling_7d_drawdown_below_p95",
        "observed_pct": -9.0,
        "threshold_pct": -8.0937,
    } in flags
