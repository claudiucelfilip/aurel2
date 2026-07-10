"""Pure metric computations for the weekly scorecard: Sharpe, max DD, alpha,
cumulative return. All operate on plain (date, value) daily equity series so
they're testable with synthetic data, no I/O.
"""

import math
from datetime import date

import pytest

from aurel2.scorecard.metrics import (
    alpha_vs_benchmark,
    cumulative_return,
    max_drawdown,
    sharpe_ratio,
)


def series(values, start=date(2026, 1, 1)):
    """Build a list of (date, value) pairs, one per calendar day starting at start."""
    from datetime import timedelta
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


class TestCumulativeReturn:
    def test_flat_series_is_zero(self):
        assert cumulative_return(series([100.0, 100.0, 100.0])) == 0.0

    def test_ten_percent_gain(self):
        result = cumulative_return(series([100.0, 105.0, 110.0]))
        assert result == pytest.approx(0.10)

    def test_empty_series_is_none(self):
        assert cumulative_return([]) is None

    def test_single_point_is_zero(self):
        assert cumulative_return(series([100.0])) == 0.0


class TestMaxDrawdown:
    def test_monotonic_up_has_zero_drawdown(self):
        assert max_drawdown(series([100.0, 110.0, 120.0])) == 0.0

    def test_peak_then_trough(self):
        # peak 120, trough 90 -> dd = (120-90)/120 = 0.25
        result = max_drawdown(series([100.0, 120.0, 90.0, 115.0]))
        assert result == pytest.approx(0.25)

    def test_recovers_then_worse_drawdown_wins(self):
        # peak 100 -> 50 (dd .5), recover to 200 -> 100 (dd .5) -- max stays .5
        result = max_drawdown(series([100.0, 50.0, 200.0, 100.0]))
        assert result == pytest.approx(0.5)

    def test_empty_series_is_none(self):
        assert max_drawdown([]) is None


class TestSharpeRatio:
    def test_zero_volatility_is_zero(self):
        # constant daily return of 0 (flat series) -> std is 0 -> sharpe 0
        assert sharpe_ratio(series([100.0] * 15)) == 0.0

    def test_insufficient_history_returns_none(self):
        # fewer than 10 daily returns (11 points -> 10 returns is the minimum)
        result = sharpe_ratio(series([100.0, 101.0, 102.0]))
        assert result is None

    def test_ten_returns_is_minimum_reportable(self):
        # exactly 11 points -> 10 daily returns, should compute (not None)
        values = [100.0 + i for i in range(11)]
        result = sharpe_ratio(series(values))
        assert result is not None

    def test_positive_trend_gives_positive_sharpe(self):
        values = [100.0 * (1.001 ** i) for i in range(30)]
        result = sharpe_ratio(series(values))
        assert result > 0

    def test_annualization_uses_sqrt_252(self):
        # Hand-computed: constant daily return r on every step gives std=0 via
        # pct_change (all returns identical) -- use a two-value alternating
        # series instead so std > 0, and check against a manual formula.
        values = [100.0, 101.0] * 8  # 16 points, alternating +1%/-0.99%
        result = sharpe_ratio(series(values))

        returns = []
        vals = [v for _, v in series(values)]
        for i in range(1, len(vals)):
            returns.append(vals[i] / vals[i - 1] - 1)
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(variance)
        expected = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0

        assert result == pytest.approx(expected)


class TestAlphaVsBenchmark:
    def test_arm_beats_benchmark(self):
        arm = series([100.0, 110.0])
        bench = series([100.0, 105.0])
        result = alpha_vs_benchmark(arm, bench)
        assert result == pytest.approx(0.05)

    def test_arm_trails_benchmark_negative_alpha(self):
        arm = series([100.0, 102.0])
        bench = series([100.0, 110.0])
        result = alpha_vs_benchmark(arm, bench)
        assert result == pytest.approx(0.02 - 0.10)

    def test_missing_arm_data_returns_none(self):
        assert alpha_vs_benchmark([], series([100.0, 105.0])) is None

    def test_missing_benchmark_data_returns_none(self):
        assert alpha_vs_benchmark(series([100.0, 105.0]), []) is None
