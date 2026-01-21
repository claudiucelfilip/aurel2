"""Tests for agent orchestrator."""

from datetime import time

import pytest

from aurel2.agent.orchestrator import (
    AgentDecision,
    AgentOrchestrator,
    DecisionType,
    Urgency,
)
from aurel2.core.models import SignalAction


class TestDecisionType:
    """Tests for DecisionType enum."""

    def test_has_routine_value(self):
        """DecisionType should have ROUTINE value."""
        assert DecisionType.ROUTINE.value == "routine"

    def test_has_non_routine_value(self):
        """DecisionType should have NON_ROUTINE value."""
        assert DecisionType.NON_ROUTINE.value == "non_routine"

    def test_has_urgent_value(self):
        """DecisionType should have URGENT value."""
        assert DecisionType.URGENT.value == "urgent"


class TestUrgency:
    """Tests for Urgency enum."""

    def test_has_low_value(self):
        """Urgency should have LOW value."""
        assert Urgency.LOW.value == "low"

    def test_has_medium_value(self):
        """Urgency should have MEDIUM value."""
        assert Urgency.MEDIUM.value == "medium"

    def test_has_high_value(self):
        """Urgency should have HIGH value."""
        assert Urgency.HIGH.value == "high"


class TestAgentDecision:
    """Tests for AgentDecision dataclass."""

    def test_agent_decision_dataclass(self):
        """AgentDecision should be properly instantiable."""
        decision = AgentDecision(
            decision_type=DecisionType.ROUTINE,
            action=SignalAction.HOLD,
            asset_symbol=None,
            reasoning="All strategies agree on holding",
            confidence=0.85,
            strategy_signals={
                "dual_momentum": {"action": "hold"},
                "mean_reversion": {"action": "hold"},
                "multi_timeframe": {"action": "hold"},
            },
            requires_approval=False,
            timeout_hours=24.0,
            urgency=Urgency.LOW,
            market_context={"volatility": "normal"},
        )
        assert decision.decision_type == DecisionType.ROUTINE
        assert decision.action == SignalAction.HOLD
        assert decision.requires_approval is False
        assert decision.urgency == Urgency.LOW

    def test_agent_decision_to_dict(self):
        """AgentDecision.to_dict() should serialize correctly."""
        decision = AgentDecision(
            decision_type=DecisionType.NON_ROUTINE,
            action=SignalAction.BUY,
            asset_symbol="SPY",
            reasoning="Strategy disagreement",
            confidence=0.65,
            strategy_signals={"dual_momentum": {"action": "buy"}},
            requires_approval=True,
            timeout_hours=8.0,
            urgency=Urgency.MEDIUM,
            market_context={"drawdown": 0.05},
        )
        result = decision.to_dict()
        assert result["decision_type"] == "non_routine"
        assert result["action"] == "buy"
        assert result["asset_symbol"] == "SPY"
        assert result["requires_approval"] is True
        assert result["timeout_hours"] == 8.0
        assert result["urgency"] == "medium"


class TestAgentOrchestratorInit:
    """Tests for AgentOrchestrator initialization."""

    def test_default_initialization(self):
        """AgentOrchestrator should initialize with default values."""
        orchestrator = AgentOrchestrator()
        assert orchestrator.timezone == "Europe/Bucharest"
        assert orchestrator.sleep_start == time(23, 0)
        assert orchestrator.sleep_end == time(8, 0)

    def test_custom_initialization(self):
        """AgentOrchestrator should accept custom timezone and sleep hours."""
        orchestrator = AgentOrchestrator(
            timezone="America/New_York",
            sleep_start=time(22, 0),
            sleep_end=time(7, 0),
        )
        assert orchestrator.timezone == "America/New_York"
        assert orchestrator.sleep_start == time(22, 0)
        assert orchestrator.sleep_end == time(7, 0)


class TestIsSleepHours:
    """Tests for _is_sleep_hours method."""

    def test_is_sleep_hours_at_2am(self):
        """2am should be sleep hours."""
        orchestrator = AgentOrchestrator(
            timezone="Europe/Bucharest",
            sleep_start=time(23, 0),
            sleep_end=time(8, 0),
        )
        assert orchestrator._is_sleep_hours(time(2, 0)) is True

    def test_is_sleep_hours_at_10am(self):
        """10am should be awake hours."""
        orchestrator = AgentOrchestrator(
            timezone="Europe/Bucharest",
            sleep_start=time(23, 0),
            sleep_end=time(8, 0),
        )
        assert orchestrator._is_sleep_hours(time(10, 0)) is False

    def test_is_sleep_hours_at_2330(self):
        """11:30pm should be sleep hours."""
        orchestrator = AgentOrchestrator(
            timezone="Europe/Bucharest",
            sleep_start=time(23, 0),
            sleep_end=time(8, 0),
        )
        assert orchestrator._is_sleep_hours(time(23, 30)) is True

    def test_is_sleep_hours_at_midnight(self):
        """Midnight should be sleep hours."""
        orchestrator = AgentOrchestrator(
            timezone="Europe/Bucharest",
            sleep_start=time(23, 0),
            sleep_end=time(8, 0),
        )
        assert orchestrator._is_sleep_hours(time(0, 0)) is True

    def test_is_sleep_hours_at_8am(self):
        """8am (sleep end) should be awake hours."""
        orchestrator = AgentOrchestrator(
            timezone="Europe/Bucharest",
            sleep_start=time(23, 0),
            sleep_end=time(8, 0),
        )
        assert orchestrator._is_sleep_hours(time(8, 0)) is False

    def test_is_sleep_hours_at_23_00(self):
        """23:00 (sleep start) should be sleep hours."""
        orchestrator = AgentOrchestrator(
            timezone="Europe/Bucharest",
            sleep_start=time(23, 0),
            sleep_end=time(8, 0),
        )
        assert orchestrator._is_sleep_hours(time(23, 0)) is True


class TestClassifyDecision:
    """Tests for _classify_decision method."""

    def test_all_agree_is_routine(self):
        """When all strategies agree, decision should be ROUTINE."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "hold"},
            "mean_reversion": {"action": "hold"},
            "multi_timeframe": {"action": "hold"},
        }
        assert orchestrator._classify_decision(signals) == DecisionType.ROUTINE

    def test_disagreement_is_non_routine(self):
        """When strategies disagree, decision should be NON_ROUTINE."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "hold"},
            "mean_reversion": {"action": "buy"},
            "multi_timeframe": {"action": "hold"},
        }
        assert orchestrator._classify_decision(signals) == DecisionType.NON_ROUTINE

    def test_all_agree_buy_is_routine(self):
        """When all strategies agree on BUY, decision should be ROUTINE."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "buy"},
            "mean_reversion": {"action": "buy"},
            "multi_timeframe": {"action": "buy"},
        }
        assert orchestrator._classify_decision(signals) == DecisionType.ROUTINE

    def test_all_agree_sell_is_routine(self):
        """When all strategies agree on SELL, decision should be ROUTINE."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "sell"},
            "mean_reversion": {"action": "sell"},
            "multi_timeframe": {"action": "sell"},
        }
        assert orchestrator._classify_decision(signals) == DecisionType.ROUTINE

    def test_two_out_of_three_disagree(self):
        """When 2 out of 3 strategies disagree from the third, should be NON_ROUTINE."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "buy"},
            "mean_reversion": {"action": "buy"},
            "multi_timeframe": {"action": "sell"},
        }
        assert orchestrator._classify_decision(signals) == DecisionType.NON_ROUTINE


class TestDetermineUrgency:
    """Tests for _determine_urgency method."""

    def test_high_drawdown_is_urgent(self):
        """High drawdown (>15%) should result in HIGH urgency."""
        orchestrator = AgentOrchestrator()
        signals = {"dual_momentum": {"action": "sell"}}
        market_context = {"drawdown": 0.18}
        assert orchestrator._determine_urgency(signals, market_context) == Urgency.HIGH

    def test_medium_drawdown_is_medium_urgency(self):
        """Medium drawdown (5-15%) should result in MEDIUM urgency."""
        orchestrator = AgentOrchestrator()
        signals = {"dual_momentum": {"action": "hold"}}
        market_context = {"drawdown": 0.10}
        assert orchestrator._determine_urgency(signals, market_context) == Urgency.MEDIUM

    def test_low_drawdown_is_low_urgency(self):
        """Low drawdown (<5%) should result in LOW urgency."""
        orchestrator = AgentOrchestrator()
        signals = {"dual_momentum": {"action": "hold"}}
        market_context = {"drawdown": 0.02}
        assert orchestrator._determine_urgency(signals, market_context) == Urgency.LOW

    def test_no_drawdown_info_is_low_urgency(self):
        """No drawdown info should default to LOW urgency."""
        orchestrator = AgentOrchestrator()
        signals = {"dual_momentum": {"action": "hold"}}
        market_context = {}
        assert orchestrator._determine_urgency(signals, market_context) == Urgency.LOW

    def test_extreme_volatility_increases_urgency(self):
        """Extreme volatility should increase urgency."""
        orchestrator = AgentOrchestrator()
        signals = {"dual_momentum": {"action": "hold"}}
        market_context = {"volatility": "extreme", "drawdown": 0.03}
        # Even with low drawdown, extreme volatility bumps to MEDIUM
        assert orchestrator._determine_urgency(signals, market_context) == Urgency.MEDIUM


class TestCalculateTimeout:
    """Tests for _calculate_timeout method."""

    def test_routine_low_urgency_timeout(self):
        """ROUTINE + LOW urgency should have 24-48h timeout."""
        orchestrator = AgentOrchestrator()
        timeout = orchestrator._calculate_timeout(DecisionType.ROUTINE, Urgency.LOW)
        assert 24 <= timeout <= 48

    def test_non_routine_medium_urgency_timeout(self):
        """NON_ROUTINE + MEDIUM urgency should have 4-8h timeout."""
        orchestrator = AgentOrchestrator()
        timeout = orchestrator._calculate_timeout(DecisionType.NON_ROUTINE, Urgency.MEDIUM)
        assert 4 <= timeout <= 8

    def test_urgent_high_urgency_timeout(self):
        """URGENT + HIGH urgency should have 1-2h timeout."""
        orchestrator = AgentOrchestrator()
        timeout = orchestrator._calculate_timeout(DecisionType.URGENT, Urgency.HIGH)
        assert 1 <= timeout <= 2


class TestSelectBestAction:
    """Tests for _select_best_action method."""

    def test_unanimous_hold_selects_hold(self):
        """Unanimous HOLD should select HOLD."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "hold", "confidence": 0.8},
            "mean_reversion": {"action": "hold", "confidence": 0.7},
            "multi_timeframe": {"action": "hold", "confidence": 0.9},
        }
        action, asset, confidence = orchestrator._select_best_action(signals, {})
        assert action == SignalAction.HOLD
        assert confidence > 0.7

    def test_majority_buy_selects_buy(self):
        """Majority BUY should select BUY."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "buy", "confidence": 0.8, "asset_symbol": "SPY"},
            "mean_reversion": {"action": "buy", "confidence": 0.7, "asset_symbol": "SPY"},
            "multi_timeframe": {"action": "hold", "confidence": 0.6},
        }
        action, asset, confidence = orchestrator._select_best_action(signals, {})
        assert action == SignalAction.BUY
        assert asset == "SPY"

    def test_mixed_signals_uses_highest_confidence(self):
        """Mixed signals should weight by confidence."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "buy", "confidence": 0.9, "asset_symbol": "SPY"},
            "mean_reversion": {"action": "sell", "confidence": 0.3},
            "multi_timeframe": {"action": "hold", "confidence": 0.5},
        }
        action, asset, confidence = orchestrator._select_best_action(signals, {})
        # BUY has highest confidence
        assert action == SignalAction.BUY


class TestAnalyze:
    """Tests for analyze method."""

    def test_analyze_returns_agent_decision(self):
        """analyze() should return an AgentDecision."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "hold", "confidence": 0.8},
            "mean_reversion": {"action": "hold", "confidence": 0.7},
            "multi_timeframe": {"action": "hold", "confidence": 0.9},
        }
        market_context = {"drawdown": 0.02, "volatility": "normal"}
        decision = orchestrator.analyze(signals, market_context)
        assert isinstance(decision, AgentDecision)
        assert decision.decision_type == DecisionType.ROUTINE
        assert decision.action == SignalAction.HOLD
        assert decision.requires_approval is False

    def test_analyze_non_routine_requires_approval(self):
        """Non-routine decisions should require approval."""
        orchestrator = AgentOrchestrator()
        signals = {
            "dual_momentum": {"action": "buy", "confidence": 0.8},
            "mean_reversion": {"action": "sell", "confidence": 0.7},
            "multi_timeframe": {"action": "hold", "confidence": 0.6},
        }
        market_context = {"drawdown": 0.05}
        decision = orchestrator.analyze(signals, market_context)
        assert decision.decision_type == DecisionType.NON_ROUTINE
        assert decision.requires_approval is True


class TestExecute:
    """Tests for execute method."""

    def test_execute_routine_decision(self):
        """execute() should handle routine decisions."""
        orchestrator = AgentOrchestrator()
        decision = AgentDecision(
            decision_type=DecisionType.ROUTINE,
            action=SignalAction.HOLD,
            asset_symbol=None,
            reasoning="All strategies agree",
            confidence=0.85,
            strategy_signals={},
            requires_approval=False,
            timeout_hours=24.0,
            urgency=Urgency.LOW,
            market_context={},
        )
        result = orchestrator.execute(decision)
        assert result["status"] == "executed"
        assert result["action"] == "hold"

    def test_execute_non_routine_without_approval(self):
        """execute() should not execute non-routine without approval."""
        orchestrator = AgentOrchestrator()
        decision = AgentDecision(
            decision_type=DecisionType.NON_ROUTINE,
            action=SignalAction.BUY,
            asset_symbol="SPY",
            reasoning="Strategy disagreement",
            confidence=0.65,
            strategy_signals={},
            requires_approval=True,
            timeout_hours=8.0,
            urgency=Urgency.MEDIUM,
            market_context={},
        )
        result = orchestrator.execute(decision)
        assert result["status"] == "pending_approval"
