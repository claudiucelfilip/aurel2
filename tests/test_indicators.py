"""Tests for technical indicators."""

import pytest
import pandas as pd
import numpy as np
from aurel2.data.indicators import (
    calculate_rsi,
    calculate_drawdown,
    calculate_moving_average,
    get_current_price,
)


class TestCalculateRSI:
    """Tests for RSI calculation."""

    def test_calculate_rsi_basic(self):
        """RSI should be between 0 and 100."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=50, freq="D"),
            "close": [100 + i * 0.5 for i in range(50)],  # Uptrend
            "symbol": ["SPY"] * 50,
        })
        rsi = calculate_rsi(prices, "SPY", period=14)
        assert rsi is not None
        assert 0 <= rsi <= 100

    def test_calculate_rsi_oversold(self):
        """RSI should be low after price drops."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=50, freq="D"),
            "close": [100 - i * 1.5 for i in range(50)],  # Strong downtrend
            "symbol": ["SPY"] * 50,
        })
        rsi = calculate_rsi(prices, "SPY", period=14)
        assert rsi is not None
        assert rsi < 30  # Oversold

    def test_calculate_rsi_overbought(self):
        """RSI should be high after strong price gains."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=50, freq="D"),
            "close": [100 + i * 2.0 for i in range(50)],  # Strong uptrend
            "symbol": ["SPY"] * 50,
        })
        rsi = calculate_rsi(prices, "SPY", period=14)
        assert rsi is not None
        assert rsi > 70  # Overbought

    def test_calculate_rsi_insufficient_data(self):
        """RSI should return None when insufficient data."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100 + i for i in range(10)],
            "symbol": ["SPY"] * 10,
        })
        rsi = calculate_rsi(prices, "SPY", period=14)
        assert rsi is None

    def test_calculate_rsi_unknown_symbol(self):
        """RSI should return None for unknown symbol."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=50, freq="D"),
            "close": [100 + i for i in range(50)],
            "symbol": ["SPY"] * 50,
        })
        rsi = calculate_rsi(prices, "UNKNOWN", period=14)
        assert rsi is None

    def test_calculate_rsi_flat_prices(self):
        """RSI should handle flat prices (no gains or losses)."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=50, freq="D"),
            "close": [100.0] * 50,  # Flat
            "symbol": ["SPY"] * 50,
        })
        rsi = calculate_rsi(prices, "SPY", period=14)
        # With no movement, RSI is technically undefined but should handle gracefully
        # Returning 50 (neutral) or None are both acceptable
        assert rsi is None or rsi == 50.0


class TestCalculateDrawdown:
    """Tests for drawdown calculation."""

    def test_calculate_drawdown(self):
        """Drawdown should measure decline from peak."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100, 105, 110, 100, 95, 90, 92, 94, 96, 98],
            "symbol": ["SPY"] * 10,
        })
        drawdown = calculate_drawdown(prices, "SPY")
        # Peak was 110, current is 98, drawdown = (110-98)/110 = 10.9%
        assert drawdown is not None
        assert abs(drawdown - 0.109) < 0.01

    def test_calculate_drawdown_at_peak(self):
        """Drawdown should be zero when at peak."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100, 102, 104, 106, 108, 110, 112, 114, 116, 118],  # Always rising
            "symbol": ["SPY"] * 10,
        })
        drawdown = calculate_drawdown(prices, "SPY")
        assert drawdown is not None
        assert drawdown == 0.0

    def test_calculate_drawdown_unknown_symbol(self):
        """Drawdown should return None for unknown symbol."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100 + i for i in range(10)],
            "symbol": ["SPY"] * 10,
        })
        drawdown = calculate_drawdown(prices, "UNKNOWN")
        assert drawdown is None

    def test_calculate_drawdown_with_lookback(self):
        """Drawdown should respect lookback_days parameter."""
        # Create data with an old peak that's higher than recent peak
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=300, freq="D"),
            "close": (
                [200] * 50  # Old high
                + [150] * 200  # Long flat period
                + [160] * 49  # Recent peak
                + [155]  # Current
            ),
            "symbol": ["SPY"] * 300,
        })
        # With 252 day lookback, should see peak of 160, current 155
        drawdown = calculate_drawdown(prices, "SPY", lookback_days=60)
        assert drawdown is not None
        # Peak 160, current 155, drawdown = (160-155)/160 = 0.03125
        assert abs(drawdown - 0.03125) < 0.01


class TestCalculateMovingAverage:
    """Tests for moving average calculation."""

    def test_calculate_moving_average_basic(self):
        """Moving average should be calculated correctly."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100, 102, 104, 106, 108, 110, 112, 114, 116, 118],
            "symbol": ["SPY"] * 10,
        })
        ma = calculate_moving_average(prices, "SPY", period=5)
        assert ma is not None
        # MA of last 5 prices: (110+112+114+116+118)/5 = 114
        assert abs(ma - 114.0) < 0.01

    def test_calculate_moving_average_insufficient_data(self):
        """Moving average should return None when insufficient data."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100 + i for i in range(10)],
            "symbol": ["SPY"] * 10,
        })
        ma = calculate_moving_average(prices, "SPY", period=200)
        assert ma is None

    def test_calculate_moving_average_unknown_symbol(self):
        """Moving average should return None for unknown symbol."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=250, freq="D"),
            "close": [100 + i for i in range(250)],
            "symbol": ["SPY"] * 250,
        })
        ma = calculate_moving_average(prices, "UNKNOWN", period=200)
        assert ma is None


class TestGetCurrentPrice:
    """Tests for getting current price."""

    def test_get_current_price_basic(self):
        """Should return most recent price."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100, 102, 104, 106, 108, 110, 112, 114, 116, 118],
            "symbol": ["SPY"] * 10,
        })
        price = get_current_price(prices, "SPY")
        assert price is not None
        assert price == 118.0

    def test_get_current_price_unknown_symbol(self):
        """Should return None for unknown symbol."""
        prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100 + i for i in range(10)],
            "symbol": ["SPY"] * 10,
        })
        price = get_current_price(prices, "UNKNOWN")
        assert price is None

    def test_get_current_price_multiple_symbols(self):
        """Should return correct price for each symbol."""
        prices = pd.DataFrame({
            "date": list(pd.date_range("2024-01-01", periods=5, freq="D")) * 2,
            "close": [100, 101, 102, 103, 104, 200, 201, 202, 203, 204],
            "symbol": ["SPY"] * 5 + ["QQQ"] * 5,
        })
        spy_price = get_current_price(prices, "SPY")
        qqq_price = get_current_price(prices, "QQQ")
        assert spy_price == 104.0
        assert qqq_price == 204.0
