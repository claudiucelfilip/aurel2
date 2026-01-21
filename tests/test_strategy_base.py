"""Tests for strategy base classes."""

from datetime import date
from typing import Any

import pandas as pd
import pytest

from aurel2.core.models import AssetClass, SignalAction
from aurel2.strategies.base import BaseStrategy, StrategySignal


class TestStrategySignal:
    """Tests for StrategySignal dataclass."""

    def test_strategy_signal_dataclass(self):
        """StrategySignal should be constructable with all required fields."""
        signal = StrategySignal(
            strategy_name="test",
            date=date(2024, 1, 1),
            action=SignalAction.BUY,
            asset_class=AssetClass.US_STOCKS,
            confidence=0.8,
            reasoning="Test reasoning",
            metadata={"rsi": 25},
        )
        assert signal.strategy_name == "test"
        assert signal.confidence == 0.8

    def test_strategy_signal_all_fields(self):
        """StrategySignal should store all fields correctly."""
        signal = StrategySignal(
            strategy_name="momentum",
            date=date(2024, 6, 15),
            action=SignalAction.SELL,
            asset_class=AssetClass.BONDS_AGGREGATE,
            confidence=0.95,
            reasoning="Strong sell signal detected",
            metadata={"momentum_12m": -0.05, "trigger": "absolute_momentum"},
        )
        assert signal.strategy_name == "momentum"
        assert signal.date == date(2024, 6, 15)
        assert signal.action == SignalAction.SELL
        assert signal.asset_class == AssetClass.BONDS_AGGREGATE
        assert signal.confidence == 0.95
        assert signal.reasoning == "Strong sell signal detected"
        assert signal.metadata == {"momentum_12m": -0.05, "trigger": "absolute_momentum"}

    def test_strategy_signal_with_none_asset_class(self):
        """StrategySignal should allow None for asset_class."""
        signal = StrategySignal(
            strategy_name="test",
            date=date(2024, 1, 1),
            action=SignalAction.HOLD,
            asset_class=None,
            confidence=0.5,
            reasoning="No clear signal",
        )
        assert signal.asset_class is None

    def test_strategy_signal_default_metadata(self):
        """StrategySignal should default to empty dict for metadata."""
        signal = StrategySignal(
            strategy_name="test",
            date=date(2024, 1, 1),
            action=SignalAction.HOLD,
            asset_class=None,
            confidence=0.5,
            reasoning="Test",
        )
        assert signal.metadata == {}

    def test_strategy_signal_to_dict(self):
        """StrategySignal should serialize to dict correctly."""
        signal = StrategySignal(
            strategy_name="test_strategy",
            date=date(2024, 3, 15),
            action=SignalAction.BUY,
            asset_class=AssetClass.US_STOCKS,
            confidence=0.85,
            reasoning="Momentum is positive",
            metadata={"rsi": 30, "macd_signal": "bullish"},
        )
        result = signal.to_dict()

        assert isinstance(result, dict)
        assert result["strategy_name"] == "test_strategy"
        assert result["date"] == "2024-03-15"
        assert result["action"] == "buy"
        assert result["asset_class"] == "us_stocks"
        assert result["confidence"] == 0.85
        assert result["reasoning"] == "Momentum is positive"
        assert result["metadata"] == {"rsi": 30, "macd_signal": "bullish"}

    def test_strategy_signal_to_dict_with_none_asset_class(self):
        """StrategySignal.to_dict() should handle None asset_class."""
        signal = StrategySignal(
            strategy_name="test",
            date=date(2024, 1, 1),
            action=SignalAction.HOLD,
            asset_class=None,
            confidence=0.5,
            reasoning="Test",
        )
        result = signal.to_dict()
        assert result["asset_class"] is None

    def test_confidence_bounds(self):
        """Confidence should be between 0.0 and 1.0."""
        # Valid confidence values
        signal_low = StrategySignal(
            strategy_name="test",
            date=date(2024, 1, 1),
            action=SignalAction.HOLD,
            asset_class=None,
            confidence=0.0,
            reasoning="Test",
        )
        assert signal_low.confidence == 0.0

        signal_high = StrategySignal(
            strategy_name="test",
            date=date(2024, 1, 1),
            action=SignalAction.HOLD,
            asset_class=None,
            confidence=1.0,
            reasoning="Test",
        )
        assert signal_high.confidence == 1.0


class TestBaseStrategy:
    """Tests for BaseStrategy abstract class."""

    def test_base_strategy_is_abstract(self):
        """BaseStrategy should not be instantiable directly."""
        with pytest.raises(TypeError):
            BaseStrategy()

    def test_base_strategy_requires_name(self):
        """Concrete strategies must define a name."""
        # Should fail at class definition time because name is not defined
        with pytest.raises(TypeError):

            class IncompleteStrategy(BaseStrategy):
                def generate_signal(
                    self,
                    prices: pd.DataFrame,
                    calc_date: date,
                    current_holding: AssetClass | None,
                ) -> StrategySignal:
                    pass

                def get_rebalance_dates(
                    self,
                    start_date: date,
                    end_date: date,
                ) -> list[date]:
                    return []

    def test_concrete_strategy_implementation(self):
        """A properly implemented concrete strategy should be instantiable."""

        class ConcreteStrategy(BaseStrategy):
            name = "concrete_test_strategy"

            def generate_signal(
                self,
                prices: pd.DataFrame,
                calc_date: date,
                current_holding: AssetClass | None,
            ) -> StrategySignal:
                return StrategySignal(
                    strategy_name=self.name,
                    date=calc_date,
                    action=SignalAction.HOLD,
                    asset_class=current_holding,
                    confidence=0.5,
                    reasoning="Test signal",
                )

            def get_rebalance_dates(
                self,
                start_date: date,
                end_date: date,
            ) -> list[date]:
                return [start_date, end_date]

        strategy = ConcreteStrategy()
        assert strategy.name == "concrete_test_strategy"

    def test_concrete_strategy_generate_signal(self):
        """Concrete strategy should generate valid signals."""

        class TestStrategy(BaseStrategy):
            name = "test_strategy"

            def generate_signal(
                self,
                prices: pd.DataFrame,
                calc_date: date,
                current_holding: AssetClass | None,
            ) -> StrategySignal:
                return StrategySignal(
                    strategy_name=self.name,
                    date=calc_date,
                    action=SignalAction.BUY,
                    asset_class=AssetClass.US_STOCKS,
                    confidence=0.9,
                    reasoning="Strong momentum",
                    metadata={"test": True},
                )

            def get_rebalance_dates(
                self,
                start_date: date,
                end_date: date,
            ) -> list[date]:
                return []

        strategy = TestStrategy()
        prices = pd.DataFrame()  # Empty DataFrame for test
        signal = strategy.generate_signal(
            prices=prices,
            calc_date=date(2024, 1, 1),
            current_holding=None,
        )

        assert isinstance(signal, StrategySignal)
        assert signal.strategy_name == "test_strategy"
        assert signal.action == SignalAction.BUY
        assert signal.asset_class == AssetClass.US_STOCKS

    def test_concrete_strategy_get_rebalance_dates(self):
        """Concrete strategy should return rebalance dates."""

        class QuarterlyStrategy(BaseStrategy):
            name = "quarterly"

            def generate_signal(
                self,
                prices: pd.DataFrame,
                calc_date: date,
                current_holding: AssetClass | None,
            ) -> StrategySignal:
                return StrategySignal(
                    strategy_name=self.name,
                    date=calc_date,
                    action=SignalAction.HOLD,
                    asset_class=None,
                    confidence=0.5,
                    reasoning="Test",
                )

            def get_rebalance_dates(
                self,
                start_date: date,
                end_date: date,
            ) -> list[date]:
                # Simple quarterly implementation
                dates = []
                current = start_date
                while current <= end_date:
                    if current.month in [3, 6, 9, 12]:
                        dates.append(current)
                    # Move to next month (simplified)
                    if current.month == 12:
                        current = date(current.year + 1, 1, current.day)
                    else:
                        current = date(current.year, current.month + 1, current.day)
                return dates

        strategy = QuarterlyStrategy()
        dates = strategy.get_rebalance_dates(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )

        assert isinstance(dates, list)
        assert all(isinstance(d, date) for d in dates)
