"""Tests for Mean Reversion Strategy."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from aurel2.core.models import AssetClass, SignalAction
from aurel2.strategies.base import StrategySignal
from aurel2.strategies.mean_reversion import MeanReversionStrategy


@pytest.fixture
def oversold_prices():
    """Create price data that produces oversold RSI."""
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    prices = 100 - np.arange(50) * 1.5  # Strong downtrend
    return pd.DataFrame({"date": dates, "close": prices, "symbol": ["SPY"] * 50})


@pytest.fixture
def overbought_prices():
    """Create price data that produces overbought RSI."""
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    prices = 100 + np.arange(50) * 2.0  # Strong uptrend
    return pd.DataFrame({"date": dates, "close": prices, "symbol": ["SPY"] * 50})


@pytest.fixture
def normal_prices():
    """Create price data with RSI in normal range."""
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    # Alternating up/down to stay in neutral range
    prices = [100 + (i % 5) * 0.5 for i in range(50)]
    return pd.DataFrame({"date": dates, "close": prices, "symbol": ["SPY"] * 50})


class TestMeanReversionStrategy:
    """Tests for MeanReversionStrategy."""

    def test_strategy_name(self):
        """Strategy should have name 'mean_reversion'."""
        strategy = MeanReversionStrategy()
        assert strategy.name == "mean_reversion"

    def test_default_parameters(self):
        """Strategy should have correct default parameters."""
        strategy = MeanReversionStrategy()
        assert strategy.rsi_oversold == 25
        assert strategy.rsi_overbought == 75
        assert strategy.rsi_period == 14
        assert strategy.drawdown_threshold == 0.10
        assert strategy.target_assets == [
            AssetClass.US_STOCKS,
            AssetClass.INTL_DEVELOPED,
            AssetClass.EMERGING_MARKETS,
        ]

    def test_custom_parameters(self):
        """Strategy should accept custom parameters."""
        strategy = MeanReversionStrategy(
            rsi_oversold=25,
            rsi_overbought=75,
            rsi_period=21,
            drawdown_threshold=0.15,
            target_assets=[AssetClass.US_STOCKS],
        )
        assert strategy.rsi_oversold == 25
        assert strategy.rsi_overbought == 75
        assert strategy.rsi_period == 21
        assert strategy.drawdown_threshold == 0.15
        assert strategy.target_assets == [AssetClass.US_STOCKS]

    def test_mean_reversion_signals_buy_when_oversold(self, oversold_prices):
        """Strategy should signal BUY when RSI is oversold."""
        strategy = MeanReversionStrategy(
            rsi_oversold=30, rsi_overbought=70, drawdown_threshold=0.10
        )
        signal = strategy.generate_signal(
            prices=oversold_prices,
            calc_date=date(2024, 2, 19),
            current_holding=None,
        )
        assert signal.strategy_name == "mean_reversion"
        assert signal.action == SignalAction.BUY
        assert signal.confidence > 0.5

    def test_mean_reversion_signals_sell_when_overbought_with_position(
        self, overbought_prices
    ):
        """Strategy should signal SELL when RSI is overbought and has position."""
        strategy = MeanReversionStrategy(
            rsi_oversold=30, rsi_overbought=70, drawdown_threshold=0.10
        )
        signal = strategy.generate_signal(
            prices=overbought_prices,
            calc_date=date(2024, 2, 19),
            current_holding=AssetClass.US_STOCKS,
        )
        assert signal.strategy_name == "mean_reversion"
        assert signal.action == SignalAction.SELL
        assert signal.confidence > 0.5

    def test_mean_reversion_signals_hold_when_overbought_no_position(
        self, overbought_prices
    ):
        """Strategy should signal HOLD when overbought but no position."""
        strategy = MeanReversionStrategy(
            rsi_oversold=30, rsi_overbought=70, drawdown_threshold=0.10
        )
        signal = strategy.generate_signal(
            prices=overbought_prices,
            calc_date=date(2024, 2, 19),
            current_holding=None,
        )
        assert signal.action == SignalAction.HOLD

    def test_mean_reversion_signals_hold_when_normal(self, normal_prices):
        """Strategy should signal HOLD when RSI is in normal range."""
        strategy = MeanReversionStrategy()
        signal = strategy.generate_signal(
            prices=normal_prices,
            calc_date=date(2024, 2, 19),
            current_holding=AssetClass.US_STOCKS,
        )
        assert signal.action == SignalAction.HOLD

    def test_mean_reversion_is_event_driven(self):
        """Strategy should generate daily business day rebalance dates."""
        strategy = MeanReversionStrategy()
        dates = strategy.get_rebalance_dates(date(2024, 1, 1), date(2024, 1, 31))
        # January 2024 has 23 business days (excluding weekends)
        assert len(dates) >= 20  # At least 20 business days
        # Verify all dates are business days (Monday-Friday)
        for d in dates:
            assert d.weekday() < 5  # 0=Mon, 4=Fri

    def test_signal_returns_strategy_signal(self, oversold_prices):
        """generate_signal should return StrategySignal object."""
        strategy = MeanReversionStrategy()
        signal = strategy.generate_signal(
            prices=oversold_prices,
            calc_date=date(2024, 2, 19),
            current_holding=None,
        )
        assert isinstance(signal, StrategySignal)
        assert signal.strategy_name == "mean_reversion"
        assert isinstance(signal.action, SignalAction)
        assert 0 <= signal.confidence <= 1
        assert isinstance(signal.reasoning, str)
        assert len(signal.reasoning) > 0

    def test_signal_includes_rsi_in_metadata(self, oversold_prices):
        """Signal metadata should include RSI value."""
        strategy = MeanReversionStrategy()
        signal = strategy.generate_signal(
            prices=oversold_prices,
            calc_date=date(2024, 2, 19),
            current_holding=None,
        )
        assert "rsi" in signal.metadata
        assert isinstance(signal.metadata["rsi"], float)

    def test_signal_handles_insufficient_data(self):
        """Strategy should handle insufficient data gracefully."""
        # Only 10 days of data, not enough for RSI
        dates = pd.date_range("2024-01-01", periods=10, freq="D")
        prices = pd.DataFrame(
            {"date": dates, "close": [100 + i for i in range(10)], "symbol": ["SPY"] * 10}
        )
        strategy = MeanReversionStrategy()
        signal = strategy.generate_signal(
            prices=prices,
            calc_date=date(2024, 1, 10),
            current_holding=None,
        )
        # Should return HOLD when insufficient data
        assert signal.action == SignalAction.HOLD
        assert "insufficient" in signal.reasoning.lower() or signal.metadata.get("rsi") is None

    def test_extends_base_strategy(self):
        """MeanReversionStrategy should extend BaseStrategy."""
        from aurel2.strategies.base import BaseStrategy

        strategy = MeanReversionStrategy()
        assert isinstance(strategy, BaseStrategy)
