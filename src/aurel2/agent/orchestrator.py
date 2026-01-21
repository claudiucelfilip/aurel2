"""Agent orchestrator for intelligent decision-making.

This module provides the AgentOrchestrator class that analyzes strategy signals,
classifies decisions, handles sleep hours, and manages the approval flow.
"""

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Any

import pytz

from aurel2.core.models import SignalAction


class DecisionType(str, Enum):
    """Type of decision based on strategy agreement."""

    ROUTINE = "routine"
    NON_ROUTINE = "non_routine"
    URGENT = "urgent"


class Urgency(str, Enum):
    """Urgency level for decisions.

    - LOW: 24-48 hours to act
    - MEDIUM: 4-8 hours to act
    - HIGH: 1-2 hours to act
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class AgentDecision:
    """A decision made by the agent orchestrator.

    Attributes:
        decision_type: Classification of the decision (ROUTINE, NON_ROUTINE, URGENT).
        action: The recommended trading action.
        asset_symbol: The asset to trade, or None if no specific asset.
        reasoning: Human-readable explanation for the decision.
        confidence: Confidence level from 0.0 to 1.0.
        strategy_signals: Dictionary of signals from each strategy.
        requires_approval: Whether human approval is needed.
        timeout_hours: How long before the decision times out.
        urgency: How urgent the decision is.
        market_context: Additional market context information.
    """

    decision_type: DecisionType
    action: SignalAction
    asset_symbol: str | None
    reasoning: str
    confidence: float
    strategy_signals: dict[str, Any]
    requires_approval: bool
    timeout_hours: float
    urgency: Urgency
    market_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the decision to a dictionary.

        Returns:
            Dictionary representation suitable for JSON serialization.
        """
        return {
            "decision_type": self.decision_type.value,
            "action": self.action.value,
            "asset_symbol": self.asset_symbol,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "strategy_signals": self.strategy_signals,
            "requires_approval": self.requires_approval,
            "timeout_hours": self.timeout_hours,
            "urgency": self.urgency.value,
            "market_context": self.market_context,
        }


class AgentOrchestrator:
    """Orchestrates agent decisions based on strategy signals.

    The orchestrator analyzes signals from multiple strategies, classifies
    the decision type, determines urgency, and manages the approval workflow.

    Key Logic:
        - ROUTINE: All 3 strategies agree -> auto-execute
        - NON_ROUTINE: Strategies disagree -> needs approval
        - URGENT: High drawdown (>15%) or extreme conditions -> short timeout
        - Sleep hours (23:00-08:00 Romania): If urgent, auto-execute; else wait

    Attributes:
        timezone: The timezone for sleep hour calculations.
        sleep_start: Start time of sleep hours.
        sleep_end: End time of sleep hours.
    """

    def __init__(
        self,
        timezone: str = "Europe/Bucharest",
        sleep_start: time = time(23, 0),
        sleep_end: time = time(8, 0),
    ) -> None:
        """Initialize the orchestrator.

        Args:
            timezone: The timezone for sleep hour calculations.
            sleep_start: Start time of sleep hours (default 23:00).
            sleep_end: End time of sleep hours (default 08:00).
        """
        self.timezone = timezone
        self.sleep_start = sleep_start
        self.sleep_end = sleep_end
        self._tz = pytz.timezone(timezone)

    def _is_sleep_hours(self, current_time: time) -> bool:
        """Check if the given time is within sleep hours.

        Sleep hours span midnight (e.g., 23:00 to 08:00), so we need to
        handle the wrap-around case.

        Args:
            current_time: The time to check.

        Returns:
            True if within sleep hours, False otherwise.
        """
        # Sleep spans midnight: 23:00 -> 08:00
        # Time is in sleep hours if:
        # - After or equal to sleep_start (e.g., 23:00 or later), OR
        # - Before sleep_end (e.g., before 08:00)
        if self.sleep_start > self.sleep_end:
            # Sleep spans midnight
            return current_time >= self.sleep_start or current_time < self.sleep_end
        else:
            # Sleep doesn't span midnight (unusual but handle it)
            return self.sleep_start <= current_time < self.sleep_end

    def _classify_decision(self, signals: dict[str, dict[str, Any]]) -> DecisionType:
        """Classify the decision based on strategy agreement.

        Args:
            signals: Dictionary mapping strategy names to their signals.
                Each signal should have an "action" key.

        Returns:
            ROUTINE if all strategies agree, NON_ROUTINE otherwise.
        """
        if not signals:
            return DecisionType.NON_ROUTINE

        # Extract actions from all signals
        actions = [
            signal.get("action") for signal in signals.values() if signal.get("action")
        ]

        if not actions:
            return DecisionType.NON_ROUTINE

        # All strategies must agree for ROUTINE
        first_action = actions[0]
        if all(action == first_action for action in actions):
            return DecisionType.ROUTINE

        return DecisionType.NON_ROUTINE

    def _determine_urgency(
        self, signals: dict[str, dict[str, Any]], market_context: dict[str, Any]
    ) -> Urgency:
        """Determine the urgency level based on market conditions.

        Urgency levels:
            - HIGH: Drawdown > 15% or extreme conditions
            - MEDIUM: Drawdown 5-15% or extreme volatility
            - LOW: Normal conditions (drawdown < 5%)

        Args:
            signals: Strategy signals (currently unused but available for future use).
            market_context: Dictionary with market conditions.
                Expected keys: "drawdown" (float), "volatility" (str).

        Returns:
            The urgency level.
        """
        drawdown = market_context.get("drawdown", 0.0)
        volatility = market_context.get("volatility", "normal")

        # High drawdown (>15%) = HIGH urgency
        if drawdown > 0.15:
            return Urgency.HIGH

        # Medium drawdown (5-15%) = MEDIUM urgency
        if drawdown > 0.05:
            return Urgency.MEDIUM

        # Extreme volatility bumps urgency to MEDIUM
        if volatility == "extreme":
            return Urgency.MEDIUM

        return Urgency.LOW

    def _calculate_timeout(self, decision_type: DecisionType, urgency: Urgency) -> float:
        """Calculate the timeout in hours for a decision.

        Timeout ranges:
            - LOW urgency: 24-48 hours (use 24)
            - MEDIUM urgency: 4-8 hours (use 8)
            - HIGH urgency: 1-2 hours (use 2)

        The timeout is also influenced by decision type.

        Args:
            decision_type: The type of decision.
            urgency: The urgency level.

        Returns:
            Timeout in hours.
        """
        # Base timeouts by urgency
        base_timeouts = {
            Urgency.LOW: 24.0,
            Urgency.MEDIUM: 8.0,
            Urgency.HIGH: 2.0,
        }

        timeout = base_timeouts.get(urgency, 24.0)

        # Urgent decisions get minimum timeout
        if decision_type == DecisionType.URGENT:
            timeout = min(timeout, 2.0)

        return timeout

    def _select_best_action(
        self, signals: dict[str, dict[str, Any]], market_context: dict[str, Any]
    ) -> tuple[SignalAction, str | None, float]:
        """Select the best action from the signals.

        Uses a confidence-weighted voting system:
        1. If all signals agree, return that action with average confidence.
        2. Otherwise, weight by confidence and select the majority.

        Args:
            signals: Strategy signals with "action", "confidence", and optionally
                "asset_symbol" keys.
            market_context: Market context (for future use).

        Returns:
            Tuple of (action, asset_symbol, confidence).
        """
        if not signals:
            return SignalAction.HOLD, None, 0.5

        # Collect actions with their confidences
        action_weights: dict[str, float] = {
            "hold": 0.0,
            "buy": 0.0,
            "sell": 0.0,
        }
        asset_symbols: dict[str, str] = {}

        for strategy_name, signal in signals.items():
            action = signal.get("action", "hold")
            confidence = signal.get("confidence", 0.5)
            asset_symbol = signal.get("asset_symbol")

            if action in action_weights:
                action_weights[action] += confidence
                if asset_symbol and action != "hold":
                    asset_symbols[action] = asset_symbol

        # Find the action with highest weighted score
        best_action = max(action_weights, key=lambda k: action_weights[k])
        total_confidence = sum(action_weights.values())

        # Calculate normalized confidence for the best action
        if total_confidence > 0:
            confidence = action_weights[best_action] / total_confidence
        else:
            confidence = 0.5

        # Map to SignalAction enum
        action_map = {
            "hold": SignalAction.HOLD,
            "buy": SignalAction.BUY,
            "sell": SignalAction.SELL,
        }

        return (
            action_map.get(best_action, SignalAction.HOLD),
            asset_symbols.get(best_action),
            confidence,
        )

    def analyze(
        self,
        signals: dict[str, dict[str, Any]],
        market_context: dict[str, Any] | None = None,
    ) -> AgentDecision:
        """Analyze strategy signals and produce a decision.

        This is the main entry point for the orchestrator. It:
        1. Classifies the decision type based on strategy agreement.
        2. Determines urgency from market conditions.
        3. Calculates timeout based on type and urgency.
        4. Selects the best action and confidence.
        5. Determines if approval is needed.

        Args:
            signals: Dictionary mapping strategy names to their signals.
            market_context: Optional market context with conditions.

        Returns:
            An AgentDecision with all relevant information.
        """
        if market_context is None:
            market_context = {}

        # Classify and determine urgency
        decision_type = self._classify_decision(signals)
        urgency = self._determine_urgency(signals, market_context)

        # High urgency conditions can upgrade to URGENT decision type
        if urgency == Urgency.HIGH:
            decision_type = DecisionType.URGENT

        # Calculate timeout
        timeout_hours = self._calculate_timeout(decision_type, urgency)

        # Select the best action
        action, asset_symbol, confidence = self._select_best_action(signals, market_context)

        # Determine if approval is needed
        # ROUTINE decisions don't need approval
        # NON_ROUTINE and URGENT decisions need approval (unless auto-execute conditions)
        requires_approval = decision_type != DecisionType.ROUTINE

        # Build reasoning
        if decision_type == DecisionType.ROUTINE:
            reasoning = f"All strategies agree on {action.value.upper()} action"
        elif decision_type == DecisionType.URGENT:
            reasoning = f"Urgent condition detected. Recommended action: {action.value.upper()}"
        else:
            actions = [s.get("action", "unknown") for s in signals.values()]
            reasoning = f"Strategy disagreement detected: {actions}"

        return AgentDecision(
            decision_type=decision_type,
            action=action,
            asset_symbol=asset_symbol,
            reasoning=reasoning,
            confidence=confidence,
            strategy_signals=signals,
            requires_approval=requires_approval,
            timeout_hours=timeout_hours,
            urgency=urgency,
            market_context=market_context,
        )

    def execute(self, decision: AgentDecision) -> dict[str, Any]:
        """Execute a decision if appropriate.

        For routine decisions, executes immediately.
        For non-routine decisions, returns pending status.
        For urgent decisions during sleep hours, may auto-execute.

        Args:
            decision: The decision to execute.

        Returns:
            Dictionary with execution status and details.
        """
        # Check if we're in sleep hours
        now = datetime.now(self._tz)
        in_sleep_hours = self._is_sleep_hours(now.time())

        # Handle based on decision type and approval requirements
        if not decision.requires_approval:
            # Routine decisions - auto-execute
            return {
                "status": "executed",
                "action": decision.action.value,
                "asset_symbol": decision.asset_symbol,
                "timestamp": now.isoformat(),
                "decision_type": decision.decision_type.value,
            }

        # Non-routine decisions need approval
        if decision.decision_type == DecisionType.URGENT and in_sleep_hours:
            # Urgent during sleep - could auto-execute with notification
            return {
                "status": "auto_executed_urgent",
                "action": decision.action.value,
                "asset_symbol": decision.asset_symbol,
                "timestamp": now.isoformat(),
                "decision_type": decision.decision_type.value,
                "note": "Auto-executed due to urgency during sleep hours",
            }

        # Normal non-routine - pending approval
        return {
            "status": "pending_approval",
            "action": decision.action.value,
            "asset_symbol": decision.asset_symbol,
            "timeout_hours": decision.timeout_hours,
            "decision_type": decision.decision_type.value,
            "urgency": decision.urgency.value,
        }
