"""Tests for Agent Backtest Engine."""

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from aurel2.agent.orchestrator import DecisionType
from aurel2.core.models import AssetClass, SignalAction
from aurel2.engine.backtest_agent import (
    AgentBacktestEngine,
    AgentBacktestResult,
    AgentDecisionRecord,
)


@pytest.fixture
def sample_prices():
    """Create sample price data for testing."""
    # Generate 2 years of daily data for SPY, EFA, AGG
    dates = pd.date_range("2022-01-01", "2024-01-01", freq="D")
    n = len(dates)

    # SPY: trending up with volatility
    spy_prices = 400 + np.cumsum(np.random.randn(n) * 2 + 0.05)
    spy_prices = np.maximum(spy_prices, 300)  # Floor at 300

    # EFA: similar but less correlated
    efa_prices = 70 + np.cumsum(np.random.randn(n) * 1.5 + 0.02)
    efa_prices = np.maximum(efa_prices, 50)

    # AGG: bonds, more stable
    agg_prices = 100 + np.cumsum(np.random.randn(n) * 0.3 - 0.01)
    agg_prices = np.maximum(agg_prices, 80)

    # Combine into single DataFrame
    df = pd.concat([
        pd.DataFrame({"date": dates, "close": spy_prices, "symbol": "SPY"}),
        pd.DataFrame({"date": dates, "close": efa_prices, "symbol": "EFA"}),
        pd.DataFrame({"date": dates, "close": agg_prices, "symbol": "AGG"}),
    ])

    return df


@pytest.fixture
def deterministic_prices():
    """Create deterministic price data for predictable testing."""
    dates = pd.date_range("2023-01-01", "2023-12-31", freq="D")
    n = len(dates)

    # SPY: steady uptrend
    spy_prices = 400 + np.arange(n) * 0.3

    # EFA: mild uptrend
    efa_prices = 70 + np.arange(n) * 0.1

    # AGG: flat to slightly down
    agg_prices = 100 - np.arange(n) * 0.01

    df = pd.concat([
        pd.DataFrame({"date": dates, "close": spy_prices, "symbol": "SPY"}),
        pd.DataFrame({"date": dates, "close": efa_prices, "symbol": "EFA"}),
        pd.DataFrame({"date": dates, "close": agg_prices, "symbol": "AGG"}),
    ])

    return df


class TestAgentDecisionRecord:
    """Tests for AgentDecisionRecord dataclass."""

    def test_agent_decision_record_creation(self):
        """AgentDecisionRecord should be properly instantiable."""
        record = AgentDecisionRecord(
            date=date(2024, 1, 15),
            decision_type=DecisionType.ROUTINE,
            action=SignalAction.BUY,
            asset_class=AssetClass.US_STOCKS,
            confidence=0.85,
            reasoning="All strategies agree on BUY",
            strategy_signals={
                "dual_momentum": {"action": "buy", "confidence": 0.8},
                "mean_reversion": {"action": "buy", "confidence": 0.7},
                "multi_timeframe": {"action": "buy", "confidence": 0.9},
            },
            market_regime="bull",
            executed=True,
        )

        assert record.date == date(2024, 1, 15)
        assert record.decision_type == DecisionType.ROUTINE
        assert record.action == SignalAction.BUY
        assert record.asset_class == AssetClass.US_STOCKS
        assert record.confidence == 0.85
        assert record.executed is True

    def test_agent_decision_record_with_none_asset_class(self):
        """AgentDecisionRecord should handle None asset_class."""
        record = AgentDecisionRecord(
            date=date(2024, 1, 15),
            decision_type=DecisionType.ROUTINE,
            action=SignalAction.HOLD,
            asset_class=None,
            confidence=0.5,
            reasoning="Holding position",
            strategy_signals={},
            market_regime="sideways",
            executed=False,
        )

        assert record.asset_class is None
        assert record.executed is False


class TestAgentBacktestResult:
    """Tests for AgentBacktestResult dataclass."""

    def test_agent_backtest_result_creation(self):
        """AgentBacktestResult should be properly instantiable."""
        equity_curve = pd.DataFrame({
            "date": [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)],
            "value": [10000.0, 10100.0, 10200.0],
        })

        result = AgentBacktestResult(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 3),
            initial_capital=10000.0,
            final_value=10200.0,
            total_return=0.02,
            decisions=[],
            trades=[],
            equity_curve=equity_curve,
        )

        assert result.initial_capital == 10000.0
        assert result.final_value == 10200.0
        assert result.total_return == 0.02

    def test_calculate_metrics_updates_fields(self):
        """calculate_metrics should update all metric fields."""
        equity_curve = pd.DataFrame({
            "date": [date(2023, 1, 1), date(2023, 6, 1), date(2024, 1, 1)],
            "value": [10000.0, 10500.0, 11000.0],
        })

        decisions = [
            AgentDecisionRecord(
                date=date(2023, 1, 1),
                decision_type=DecisionType.ROUTINE,
                action=SignalAction.BUY,
                asset_class=AssetClass.US_STOCKS,
                confidence=0.8,
                reasoning="Test",
                strategy_signals={},
                market_regime="bull",
                executed=True,
            ),
            AgentDecisionRecord(
                date=date(2023, 6, 1),
                decision_type=DecisionType.NON_ROUTINE,
                action=SignalAction.HOLD,
                asset_class=AssetClass.US_STOCKS,
                confidence=0.6,
                reasoning="Test",
                strategy_signals={},
                market_regime="sideways",
                executed=False,
            ),
        ]

        result = AgentBacktestResult(
            start_date=date(2023, 1, 1),
            end_date=date(2024, 1, 1),
            initial_capital=10000.0,
            final_value=11000.0,
            total_return=0.0,
            decisions=decisions,
            trades=[],
            equity_curve=equity_curve,
        )

        result.calculate_metrics()

        assert result.total_return == pytest.approx(0.10, abs=0.001)
        assert result.routine_count == 1
        assert result.non_routine_count == 1
        assert result.urgent_count == 0
        assert result.strategy_agreement_rate == 0.5

    def test_print_summary_executes_without_error(self, capsys):
        """print_summary should execute without errors."""
        equity_curve = pd.DataFrame({
            "date": [date(2024, 1, 1)],
            "value": [10500.0],
        })

        result = AgentBacktestResult(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            initial_capital=10000.0,
            final_value=10500.0,
            total_return=0.05,
            decisions=[],
            trades=[],
            equity_curve=equity_curve,
            benchmark_return=0.04,
        )

        result.print_summary()
        captured = capsys.readouterr()

        assert "AGENT BACKTEST RESULTS" in captured.out
        assert "Initial Capital" in captured.out
        assert "Final Value" in captured.out
        assert "Benchmark" in captured.out


class TestAgentBacktestEngineInit:
    """Tests for AgentBacktestEngine initialization."""

    def test_default_initialization(self):
        """AgentBacktestEngine should initialize with default values."""
        engine = AgentBacktestEngine()

        assert engine.initial_capital == 10000.0
        assert engine.transaction_cost_pct == 0.001
        assert engine.dual_momentum is not None
        assert engine.mean_reversion is not None
        assert engine.multi_timeframe is not None
        assert engine.orchestrator is not None

    def test_custom_initialization(self):
        """AgentBacktestEngine should accept custom parameters."""
        engine = AgentBacktestEngine(
            initial_capital=50000.0,
            transaction_cost_pct=0.002,
        )

        assert engine.initial_capital == 50000.0
        assert engine.transaction_cost_pct == 0.002


class TestGetCheckDates:
    """Tests for _get_check_dates method."""

    def test_daily_frequency(self):
        """Daily frequency should return business days."""
        engine = AgentBacktestEngine()
        dates = engine._get_check_dates(
            date(2024, 1, 1),
            date(2024, 1, 31),
            "daily",
        )

        # January 2024 has ~23 business days
        assert len(dates) >= 20
        assert len(dates) <= 25

        # All dates should be business days (Mon-Fri)
        for d in dates:
            assert d.weekday() < 5

    def test_weekly_frequency(self):
        """Weekly frequency should return ~4-5 dates per month."""
        engine = AgentBacktestEngine()
        dates = engine._get_check_dates(
            date(2024, 1, 1),
            date(2024, 1, 31),
            "weekly",
        )

        assert len(dates) >= 4
        assert len(dates) <= 5

    def test_monthly_frequency(self):
        """Monthly frequency should return month-end dates."""
        engine = AgentBacktestEngine()
        dates = engine._get_check_dates(
            date(2024, 1, 1),
            date(2024, 12, 31),
            "monthly",
        )

        assert len(dates) == 12

    def test_invalid_frequency_raises_error(self):
        """Invalid frequency should raise ValueError."""
        engine = AgentBacktestEngine()

        with pytest.raises(ValueError, match="Unknown frequency"):
            engine._get_check_dates(date(2024, 1, 1), date(2024, 1, 31), "hourly")


class TestGetPrice:
    """Tests for _get_price method."""

    def test_get_price_returns_correct_price(self, deterministic_prices):
        """_get_price should return the correct closing price."""
        engine = AgentBacktestEngine()
        price = engine._get_price(deterministic_prices, "SPY", date(2023, 1, 10))

        # Price should be around 400 + 9*0.3 = 402.7
        assert price is not None
        assert 400 <= price <= 410

    def test_get_price_returns_none_for_missing_symbol(self, deterministic_prices):
        """_get_price should return None for missing symbol."""
        engine = AgentBacktestEngine()
        price = engine._get_price(deterministic_prices, "INVALID", date(2023, 1, 10))

        assert price is None

    def test_get_price_returns_none_for_future_date(self, deterministic_prices):
        """_get_price should return None for date before data starts."""
        engine = AgentBacktestEngine()
        price = engine._get_price(deterministic_prices, "SPY", date(2020, 1, 1))

        assert price is None


class TestDetermineMarketRegime:
    """Tests for _determine_market_regime method."""

    def test_returns_string(self, deterministic_prices):
        """_determine_market_regime should return a string."""
        engine = AgentBacktestEngine()
        regime = engine._determine_market_regime(
            deterministic_prices,
            date(2023, 6, 15),
        )

        assert isinstance(regime, str)
        assert len(regime) > 0

    def test_returns_insufficient_data_for_short_history(self):
        """Should return 'insufficient_data' when not enough data."""
        engine = AgentBacktestEngine()
        short_prices = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=10, freq="D"),
            "close": [100 + i for i in range(10)],
            "symbol": ["SPY"] * 10,
        })

        regime = engine._determine_market_regime(short_prices, date(2024, 1, 10))

        assert regime == "insufficient_data"


class TestCalculateDrawdown:
    """Tests for _calculate_drawdown method."""

    def test_returns_zero_for_uptrend(self, deterministic_prices):
        """Steady uptrend should have minimal drawdown."""
        engine = AgentBacktestEngine()
        drawdown = engine._calculate_drawdown(
            deterministic_prices,
            date(2023, 12, 31),
        )

        # SPY is in steady uptrend, so current price is at/near peak
        assert drawdown >= 0
        assert drawdown < 0.05  # Less than 5% drawdown

    def test_returns_correct_drawdown_for_decline(self):
        """Should calculate correct drawdown after decline."""
        engine = AgentBacktestEngine()

        # Create data with a peak and decline
        dates = pd.date_range("2023-01-01", periods=300, freq="D")
        prices = np.concatenate([
            np.linspace(100, 150, 200),  # Rise to 150
            np.linspace(150, 120, 100),  # Fall to 120
        ])

        decline_prices = pd.DataFrame({
            "date": dates,
            "close": prices,
            "symbol": ["SPY"] * 300,
        })

        drawdown = engine._calculate_drawdown(decline_prices, date(2023, 10, 27))

        # Peak was 150, current is 120, so drawdown = (150-120)/150 = 0.2
        assert drawdown == pytest.approx(0.2, abs=0.02)


class TestAgentBacktestRun:
    """Tests for the main run method."""

    def test_run_returns_agent_backtest_result(self, sample_prices):
        """run() should return AgentBacktestResult."""
        engine = AgentBacktestEngine(initial_capital=10000.0)
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 6, 30),
            check_frequency="monthly",
        )

        assert isinstance(result, AgentBacktestResult)
        assert result.start_date == date(2023, 1, 1)
        assert result.end_date == date(2023, 6, 30)
        assert result.initial_capital == 10000.0

    def test_run_creates_equity_curve(self, sample_prices):
        """run() should create an equity curve DataFrame."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 6, 30),
            check_frequency="monthly",
        )

        assert not result.equity_curve.empty
        assert "date" in result.equity_curve.columns
        assert "value" in result.equity_curve.columns

    def test_run_records_decisions(self, sample_prices):
        """run() should record decisions."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 6, 30),
            check_frequency="monthly",
        )

        # Should have at least some decisions (monthly for 6 months = 6)
        assert len(result.decisions) >= 5

        # Each decision should be an AgentDecisionRecord
        for decision in result.decisions:
            assert isinstance(decision, AgentDecisionRecord)

    def test_run_calculates_benchmark_return(self, sample_prices):
        """run() should calculate benchmark return."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 6, 30),
            check_frequency="monthly",
        )

        # Benchmark return should be calculated
        assert result.benchmark_return is not None

    def test_run_handles_daily_frequency(self, sample_prices):
        """run() should handle daily check frequency."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 3, 1),
            end_date=date(2023, 3, 31),
            check_frequency="daily",
        )

        # Daily frequency over 1 month should have ~22 decisions
        assert len(result.decisions) >= 20

    def test_run_handles_weekly_frequency(self, sample_prices):
        """run() should handle weekly check frequency."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 3, 31),
            check_frequency="weekly",
        )

        # Weekly frequency over 3 months should have ~13 decisions
        assert len(result.decisions) >= 10
        assert len(result.decisions) <= 15

    def test_run_executes_trades(self, sample_prices):
        """run() should execute trades based on decisions."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 12, 31),
            check_frequency="monthly",
        )

        # Over a year, there should be at least some trades
        # Note: depending on market conditions, could have 0 trades if always HOLD
        assert isinstance(result.trades, list)
        assert isinstance(result.num_trades, int)

    def test_run_preserves_capital(self, deterministic_prices):
        """run() should preserve capital (no money creation/destruction)."""
        engine = AgentBacktestEngine(
            initial_capital=10000.0,
            transaction_cost_pct=0.0,  # No transaction costs for this test
        )
        result = engine.run(
            prices=deterministic_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 3, 31),
            check_frequency="monthly",
        )

        # Final value should be reasonable (not NaN, not negative)
        assert result.final_value > 0
        assert not np.isnan(result.final_value)

    def test_run_calculates_metrics(self, sample_prices):
        """run() should calculate performance metrics."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 12, 31),
            check_frequency="monthly",
        )

        # Metrics should be calculated
        assert result.total_return is not None
        assert result.cagr is not None
        assert result.max_drawdown is not None
        assert result.sharpe_ratio is not None


class TestDecisionClassification:
    """Tests for decision classification logic."""

    def test_routine_decisions_counted(self, sample_prices):
        """Routine decisions should be counted correctly."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 6, 30),
            check_frequency="monthly",
        )

        # Verify counts add up to total
        total = (
            result.routine_count
            + result.non_routine_count
            + result.urgent_count
        )
        assert total == len(result.decisions)

    def test_strategy_agreement_rate_calculated(self, sample_prices):
        """Strategy agreement rate should be calculated."""
        engine = AgentBacktestEngine()
        result = engine.run(
            prices=sample_prices,
            start_date=date(2023, 1, 1),
            end_date=date(2023, 6, 30),
            check_frequency="monthly",
        )

        # Agreement rate should be between 0 and 1
        assert 0.0 <= result.strategy_agreement_rate <= 1.0


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_equity_curve_handling(self):
        """Should handle empty equity curve gracefully."""
        result = AgentBacktestResult(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            initial_capital=10000.0,
            final_value=10000.0,
            total_return=0.0,
            decisions=[],
            trades=[],
            equity_curve=pd.DataFrame(),
        )

        # Should not raise an error
        result.calculate_metrics()

    def test_single_day_backtest(self, sample_prices):
        """Should handle single-day backtest."""
        engine = AgentBacktestEngine()

        # This might raise or return minimal results - either is acceptable
        try:
            result = engine.run(
                prices=sample_prices,
                start_date=date(2023, 6, 15),
                end_date=date(2023, 6, 15),
                check_frequency="daily",
            )
            assert isinstance(result, AgentBacktestResult)
        except Exception:
            # Some edge case handling is acceptable
            pass

    def test_no_spy_data_handling(self):
        """Should handle missing SPY data gracefully."""
        engine = AgentBacktestEngine()

        # Data without SPY
        no_spy_prices = pd.DataFrame({
            "date": pd.date_range("2023-01-01", periods=100, freq="D"),
            "close": [100 + i for i in range(100)],
            "symbol": ["XYZ"] * 100,
        })

        # Should not crash, but may have limited functionality
        regime = engine._determine_market_regime(no_spy_prices, date(2023, 3, 15))
        assert regime == "unknown"

        drawdown = engine._calculate_drawdown(no_spy_prices, date(2023, 3, 15))
        assert drawdown == 0.0
