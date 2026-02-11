"""Tests for Multi-Timeframe Trend Strategy."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from aurel2.core.models import AssetClass, SignalAction
from aurel2.strategies.base import BaseStrategy, StrategySignal
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy


@pytest.fixture
def uptrend_prices():
    """Create price data with consistent uptrend (~20% annual growth)."""
    dates = pd.date_range("2023-01-01", periods=400, freq="D")
    prices = 100 * (1.0005 ** np.arange(400))  # ~20% annual growth
    return pd.DataFrame({"date": dates, "close": prices, "symbol": ["SPY"] * 400})


@pytest.fixture
def downtrend_prices():
    """Create price data with consistent downtrend."""
    dates = pd.date_range("2023-01-01", periods=400, freq="D")
    prices = 100 * (0.9995 ** np.arange(400))  # ~17% annual decline
    return pd.DataFrame({"date": dates, "close": prices, "symbol": ["SPY"] * 400})


@pytest.fixture
def multi_asset_prices():
    """Create price data for multiple assets with different trends."""
    dates = pd.date_range("2023-01-01", periods=400, freq="D")

    # SPY: strong uptrend
    spy_prices = 100 * (1.0006 ** np.arange(400))
    spy_df = pd.DataFrame({"date": dates, "close": spy_prices, "symbol": ["SPY"] * 400})

    # EFA: moderate uptrend
    efa_prices = 100 * (1.0003 ** np.arange(400))
    efa_df = pd.DataFrame({"date": dates, "close": efa_prices, "symbol": ["EFA"] * 400})

    # AGG: slight downtrend
    agg_prices = 100 * (0.9998 ** np.arange(400))
    agg_df = pd.DataFrame({"date": dates, "close": agg_prices, "symbol": ["AGG"] * 400})

    return pd.concat([spy_df, efa_df, agg_df], ignore_index=True)


class TestMultiTimeframeTrendStrategy:
    """Tests for MultiTimeframeTrendStrategy."""

    def test_strategy_name(self):
        """Strategy should have name 'multi_timeframe_trend'."""
        strategy = MultiTimeframeTrendStrategy()
        assert strategy.name == "multi_timeframe_trend"

    def test_default_parameters(self):
        """Strategy should have correct default parameters."""
        strategy = MultiTimeframeTrendStrategy()
        assert strategy.lookback_months == [1, 3, 6, 12]
        assert strategy.weights == [0.30, 0.30, 0.25, 0.15]
        assert strategy.switch_threshold == 0.03
        assert strategy.target_assets == [
            AssetClass.US_STOCKS,
            AssetClass.INTL_DEVELOPED,
            AssetClass.BONDS_AGGREGATE,
        ]

    def test_custom_parameters(self):
        """Strategy should accept custom parameters."""
        strategy = MultiTimeframeTrendStrategy(
            lookback_months=[2, 4, 8],
            weights=[0.5, 0.3, 0.2],
            switch_threshold=0.10,
            target_assets=[AssetClass.US_STOCKS, AssetClass.GOLD],
        )
        assert strategy.lookback_months == [2, 4, 8]
        assert strategy.weights == [0.5, 0.3, 0.2]
        assert strategy.switch_threshold == 0.10
        assert strategy.target_assets == [AssetClass.US_STOCKS, AssetClass.GOLD]

    def test_multi_timeframe_calculates_blended_momentum(self, uptrend_prices):
        """Strategy should calculate and return blended momentum in metadata."""
        strategy = MultiTimeframeTrendStrategy(
            lookback_months=[3, 6, 12], weights=[0.4, 0.35, 0.25]
        )
        signal = strategy.generate_signal(
            prices=uptrend_prices,
            calc_date=date(2024, 2, 1),
            current_holding=None,
        )
        assert signal.strategy_name == "multi_timeframe_trend"
        assert "momentum_3m" in signal.metadata
        assert "momentum_6m" in signal.metadata
        assert "momentum_12m" in signal.metadata
        assert "blended_momentum" in signal.metadata

    def test_multi_timeframe_signals_buy_in_uptrend(self, uptrend_prices):
        """Strategy should signal BUY in uptrend."""
        strategy = MultiTimeframeTrendStrategy()
        signal = strategy.generate_signal(
            prices=uptrend_prices,
            calc_date=date(2024, 2, 1),
            current_holding=None,
        )
        assert signal.action == SignalAction.BUY
        assert signal.confidence > 0.6

    def test_multi_timeframe_signals_hold_in_downtrend_with_position(
        self, downtrend_prices
    ):
        """Strategy should signal appropriately in downtrend."""
        strategy = MultiTimeframeTrendStrategy()
        signal = strategy.generate_signal(
            prices=downtrend_prices,
            calc_date=date(2024, 2, 1),
            current_holding=AssetClass.US_STOCKS,
        )
        # In downtrend with negative momentum, may switch to cash or hold
        assert signal.action in [SignalAction.HOLD, SignalAction.BUY]

    def test_multi_timeframe_rebalances_monthly(self):
        """Strategy should generate monthly rebalance dates."""
        strategy = MultiTimeframeTrendStrategy()
        dates = strategy.get_rebalance_dates(date(2024, 1, 1), date(2024, 6, 30))
        # Should have ~6 monthly dates (Jan through Jun month-ends)
        assert 5 <= len(dates) <= 7

    def test_rebalance_dates_are_month_ends(self):
        """Rebalance dates should be at month-end."""
        strategy = MultiTimeframeTrendStrategy()
        dates = strategy.get_rebalance_dates(date(2024, 1, 1), date(2024, 3, 31))
        # Check that dates are at month-end (day 28-31)
        for d in dates:
            assert d.day >= 28 or (d.month == 2 and d.day >= 28)

    def test_signal_returns_strategy_signal(self, uptrend_prices):
        """generate_signal should return StrategySignal object."""
        strategy = MultiTimeframeTrendStrategy()
        signal = strategy.generate_signal(
            prices=uptrend_prices,
            calc_date=date(2024, 2, 1),
            current_holding=None,
        )
        assert isinstance(signal, StrategySignal)
        assert signal.strategy_name == "multi_timeframe_trend"
        assert isinstance(signal.action, SignalAction)
        assert 0 <= signal.confidence <= 1
        assert isinstance(signal.reasoning, str)
        assert len(signal.reasoning) > 0

    def test_extends_base_strategy(self):
        """MultiTimeframeTrendStrategy should extend BaseStrategy."""
        strategy = MultiTimeframeTrendStrategy()
        assert isinstance(strategy, BaseStrategy)

    def test_weights_must_match_lookbacks(self):
        """Should raise error if weights length doesn't match lookbacks."""
        with pytest.raises(ValueError):
            MultiTimeframeTrendStrategy(
                lookback_months=[3, 6, 12],
                weights=[0.5, 0.5],  # Only 2 weights for 3 lookbacks
            )

    def test_weights_should_sum_to_approximately_one(self):
        """Weights should sum to approximately 1.0."""
        # This should work (weights sum to 1.0)
        strategy = MultiTimeframeTrendStrategy(
            lookback_months=[3, 6], weights=[0.6, 0.4]
        )
        assert abs(sum(strategy.weights) - 1.0) < 0.01

    def test_blended_momentum_calculation(self, uptrend_prices):
        """Blended momentum should be weighted average of individual momenta."""
        strategy = MultiTimeframeTrendStrategy(
            lookback_months=[3, 6, 12], weights=[0.4, 0.35, 0.25]
        )
        signal = strategy.generate_signal(
            prices=uptrend_prices,
            calc_date=date(2024, 2, 1),
            current_holding=None,
        )

        # Verify blended momentum is weighted average
        m3 = signal.metadata["momentum_3m"]
        m6 = signal.metadata["momentum_6m"]
        m12 = signal.metadata["momentum_12m"]
        expected_blended = 0.4 * m3 + 0.35 * m6 + 0.25 * m12

        assert abs(signal.metadata["blended_momentum"] - expected_blended) < 0.0001

    def test_handles_insufficient_data(self):
        """Strategy should handle insufficient data gracefully."""
        # Only 3 data points - not enough for any reliable momentum calculation
        dates = pd.date_range("2024-01-01", periods=3, freq="D")
        prices = pd.DataFrame({
            "date": dates,
            "close": [100, 101, 102],
            "symbol": ["SPY"] * 3,
        })
        strategy = MultiTimeframeTrendStrategy()
        signal = strategy.generate_signal(
            prices=prices,
            calc_date=date(2024, 1, 3),
            current_holding=None,
        )
        # Should return HOLD when insufficient data
        assert signal.action == SignalAction.HOLD

    def test_selects_highest_blended_momentum_asset(self, multi_asset_prices):
        """Strategy should select asset with highest blended momentum."""
        strategy = MultiTimeframeTrendStrategy(
            target_assets=[
                AssetClass.US_STOCKS,
                AssetClass.INTL_DEVELOPED,
                AssetClass.BONDS_AGGREGATE,
            ]
        )
        signal = strategy.generate_signal(
            prices=multi_asset_prices,
            calc_date=date(2024, 2, 1),
            current_holding=None,
        )
        # SPY has strongest uptrend, should be selected
        assert signal.asset_class == AssetClass.US_STOCKS
