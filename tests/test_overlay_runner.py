"""Majority-of-5 runner tests: 3/5 threshold, tie handling. All CLI calls mocked."""

from datetime import date
from unittest.mock import patch

from aurel2.overlay.runner import (
    tilt_health_alert,
    aggregate_samples,
    collect_samples,
    run_overlay_decision,
    should_event_trigger,
)


def sample(regime="risk_on", accel=None, lookback=None, defensive=False, confidence=0.7, reasoning="ok"):
    return {
        "regime_view": regime,
        "confidence": confidence,
        "powers": {
            "accelerate_entry": {"symbol": accel},
            "lookback_override_months": lookback,
            "force_defensive_contest": defensive,
        },
        "reasoning": reasoning,
    }


class TestAggregateRegimeMajority:
    def test_clear_majority_wins(self):
        samples = [sample("risk_on")] * 3 + [sample("risk_off")] * 2
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["regime_view"] == "risk_on"
        assert result["sample_agreement"] == 0.6
        assert result["samples"] == 5

    def test_tie_falls_back_to_conservative(self):
        # 2 risk_on vs 2 risk_off vs 1 mixed -> tie between risk_on/risk_off at 2 each? Use clean tie.
        samples = [sample("risk_on"), sample("risk_on"), sample("risk_off"), sample("risk_off")]
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["regime_view"] == "risk_off"  # more conservative of the tied pair

    def test_unanimous(self):
        samples = [sample("mixed")] * 5
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["regime_view"] == "mixed"
        assert result["sample_agreement"] == 1.0

    def test_empty_samples_produces_safe_fallback(self):
        result = aggregate_samples([], as_of=date(2026, 7, 14))
        assert result["regime_view"] == "mixed"
        assert result["samples"] == 0
        assert result["sample_agreement"] == 0.0
        assert result["powers"]["accelerate_entry"]["symbol"] is None
        assert result["powers"]["force_defensive_contest"] is False


class TestAggregatePowerThreshold:
    def test_accelerate_entry_needs_3_of_5(self):
        samples = [sample(accel="QQQ")] * 3 + [sample(accel=None)] * 2
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["powers"]["accelerate_entry"]["symbol"] == "QQQ"

    def test_accelerate_entry_below_threshold_is_null(self):
        samples = [sample(accel="QQQ")] * 2 + [sample(accel=None)] * 3
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["powers"]["accelerate_entry"]["symbol"] is None

    def test_accelerate_entry_split_votes_dont_sum(self):
        """2 vote QQQ, 2 vote XLK, 1 declines -- neither symbol hits 3/5."""
        samples = [sample(accel="QQQ")] * 2 + [sample(accel="XLK")] * 2 + [sample(accel=None)]
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["powers"]["accelerate_entry"]["symbol"] is None

    def test_lookback_override_needs_3_of_5(self):
        samples = [sample(lookback=3)] * 3 + [sample(lookback=None)] * 2
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["powers"]["lookback_override_months"] == 3

    def test_lookback_override_below_threshold_is_null(self):
        samples = [sample(lookback=3)] * 2 + [sample(lookback=6)] * 2 + [sample(lookback=None)]
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["powers"]["lookback_override_months"] is None

    def test_force_defensive_needs_3_of_5(self):
        samples = [sample(defensive=True)] * 3 + [sample(defensive=False)] * 2
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["powers"]["force_defensive_contest"] is True

    def test_force_defensive_below_threshold_is_false(self):
        samples = [sample(defensive=True)] * 2 + [sample(defensive=False)] * 3
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["powers"]["force_defensive_contest"] is False

    def test_confidence_is_averaged(self):
        samples = [sample(confidence=0.5), sample(confidence=1.0)]
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["confidence"] == 0.75

    def test_fewer_than_5_valid_samples_uses_actual_denominator(self):
        """If only 4 samples survived (one dropped/failed), agreement is out of 4, not 5."""
        samples = [sample("risk_on")] * 3 + [sample("risk_off")] * 1
        result = aggregate_samples(samples, as_of=date(2026, 7, 14))
        assert result["samples"] == 4
        assert result["sample_agreement"] == 0.75


class TestCollectSamplesMocked:
    @patch("aurel2.overlay.runner._sample_one")
    def test_drops_invalid_samples(self, mock_sample):
        mock_sample.side_effect = [
            sample("risk_on"),
            None,  # simulate CLI failure
            {"garbage": True},  # simulate malformed response
            sample("risk_off"),
            sample("mixed"),
        ]
        samples = collect_samples({"as_of": "2026-07-14"}, n=5)
        assert len(samples) == 3

    @patch("aurel2.overlay.runner._sample_one")
    def test_all_valid(self, mock_sample):
        mock_sample.side_effect = [sample("risk_on")] * 5
        samples = collect_samples({"as_of": "2026-07-14"}, n=5)
        assert len(samples) == 5


class TestRunOverlayDecisionMocked:
    @patch("aurel2.overlay.runner.git_commit_tilt")
    @patch("aurel2.overlay.runner._sample_one")
    def test_writes_tilt_file_and_commits(self, mock_sample, mock_commit, tmp_path, monkeypatch):
        mock_sample.side_effect = [sample("risk_on")] * 5
        monkeypatch.chdir(tmp_path)

        tilt = run_overlay_decision({"as_of": "2026-07-14"}, mode="paper", auto_commit=True)

        assert tilt["regime_view"] == "risk_on"
        assert (tmp_path / "data" / "paper" / "overlay_tilt.json").exists()
        mock_commit.assert_called_once()

    @patch("aurel2.overlay.runner.git_commit_tilt")
    @patch("aurel2.overlay.runner._sample_one")
    def test_no_commit_flag_skips_commit(self, mock_sample, mock_commit, tmp_path, monkeypatch):
        mock_sample.side_effect = [sample("risk_on")] * 5
        monkeypatch.chdir(tmp_path)

        run_overlay_decision({"as_of": "2026-07-14"}, mode="paper", auto_commit=False)
        mock_commit.assert_not_called()


class TestEventTrigger:
    def test_triggers_above_5pct(self):
        assert should_event_trigger(5.1) is True
        assert should_event_trigger(-5.1) is True

    def test_does_not_trigger_at_or_below_5pct(self):
        assert should_event_trigger(5.0) is False
        assert should_event_trigger(-5.0) is False
        assert should_event_trigger(2.0) is False

    def test_none_does_not_trigger(self):
        assert should_event_trigger(None) is False


class TestTiltHealthAlert:
    """A dead/degraded panel must be loudly reportable (silent Jul-Aug 2026 outage)."""

    def test_zero_samples_is_critical(self):
        tilt = aggregate_samples([], as_of=date(2026, 8, 5))
        severity, message = tilt_health_alert(tilt)
        assert severity == "critical"
        assert "0" in message

    def test_below_majority_is_warning(self):
        tilt = aggregate_samples([sample(), sample()], as_of=date(2026, 8, 5))
        severity, message = tilt_health_alert(tilt)
        assert severity == "warning"

    def test_healthy_pool_is_none(self):
        tilt = aggregate_samples([sample()] * 5, as_of=date(2026, 8, 5))
        assert tilt_health_alert(tilt) is None

    def test_majority_threshold_pool_is_none(self):
        tilt = aggregate_samples([sample()] * 3, as_of=date(2026, 8, 5))
        assert tilt_health_alert(tilt) is None
