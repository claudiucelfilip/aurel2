"""Agent orchestrator for intelligent decision-making.

This module provides the AgentOrchestrator class that analyzes strategy signals,
classifies decisions, handles sleep hours, and manages the approval flow.

Enhanced with:
- Dynamic strategy weighting based on rolling accuracy
- Confidence-based position sizing
- Regime-aware strategy selection/disabling
"""

from collections import deque
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


class MarketRegime(str, Enum):
    """Market regime for strategy selection."""

    BULL = "bull"
    BEAR = "bear"
    VOLATILE_BULL = "volatile_bull"
    VOLATILE_BEAR = "volatile_bear"
    SIDEWAYS = "sideways"


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
        position_size_pct: Recommended position size as percentage of portfolio (0.0 to 1.0).
        regime: The detected market regime.
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
    position_size_pct: float = 1.0
    regime: MarketRegime | None = None

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
            "position_size_pct": self.position_size_pct,
            "regime": self.regime.value if self.regime else None,
        }


@dataclass
class StrategyAccuracyRecord:
    """Tracks a strategy's prediction and whether it was correct."""

    strategy_name: str
    action: str
    was_correct: bool


class AgentOrchestrator:
    """Orchestrates agent decisions based on strategy signals.

    The orchestrator analyzes signals from multiple strategies, classifies
    the decision type, determines urgency, and manages the approval workflow.

    Enhanced Features:
        - Dynamic strategy weighting based on rolling accuracy
        - Confidence-based position sizing
        - Regime-aware strategy selection/disabling

    Key Logic:
        - ROUTINE: All 3 strategies agree -> auto-execute
        - NON_ROUTINE: Strategies disagree -> needs approval
        - URGENT: High drawdown (>15%) or extreme conditions -> short timeout
        - Sleep hours (23:00-08:00 Romania): If urgent, auto-execute; else wait

    Attributes:
        timezone: The timezone for sleep hour calculations.
        sleep_start: Start time of sleep hours.
        sleep_end: End time of sleep hours.
        accuracy_window: Number of recent decisions to track for accuracy.
        use_dynamic_weights: Whether to use dynamic strategy weighting.
        use_position_sizing: Whether to use confidence-based position sizing.
        use_regime_selection: Whether to disable strategies based on regime.
    """

    # Base weights for each strategy
    BASE_WEIGHTS = {
        "dual_momentum": 0.45,
        "mean_reversion": 0.25,
        "multi_timeframe": 0.30,
    }

    # Regime-specific weight adjustments
    REGIME_WEIGHTS = {
        MarketRegime.BULL: {
            "dual_momentum": 0.50,
            "mean_reversion": 0.15,  # Disable mean reversion in bull (catches false dips)
            "multi_timeframe": 0.35,
        },
        MarketRegime.BEAR: {
            "dual_momentum": 0.35,
            "mean_reversion": 0.40,  # Mean reversion valuable in bear (oversold bounces)
            "multi_timeframe": 0.25,
        },
        MarketRegime.VOLATILE_BULL: {
            "dual_momentum": 0.40,
            "mean_reversion": 0.30,
            "multi_timeframe": 0.30,
        },
        MarketRegime.VOLATILE_BEAR: {
            "dual_momentum": 0.30,
            "mean_reversion": 0.50,  # Mean reversion most valuable in volatile bear
            "multi_timeframe": 0.20,
        },
        MarketRegime.SIDEWAYS: {
            "dual_momentum": 0.30,
            "mean_reversion": 0.40,  # Mean reversion good in sideways
            "multi_timeframe": 0.30,
        },
    }

    # Strategies to disable in certain regimes (weight < threshold = disabled)
    DISABLE_THRESHOLD = 0.10

    def __init__(
        self,
        timezone: str = "Europe/Bucharest",
        sleep_start: time = time(23, 0),
        sleep_end: time = time(8, 0),
        accuracy_window: int = 20,
        use_dynamic_weights: bool = True,
        use_position_sizing: bool = True,
        use_regime_selection: bool = True,
    ) -> None:
        """Initialize the orchestrator.

        Args:
            timezone: The timezone for sleep hour calculations.
            sleep_start: Start time of sleep hours (default 23:00).
            sleep_end: End time of sleep hours (default 08:00).
            accuracy_window: Number of recent decisions to track for accuracy.
            use_dynamic_weights: Whether to use dynamic strategy weighting.
            use_position_sizing: Whether to use confidence-based position sizing.
            use_regime_selection: Whether to adjust weights based on regime.
        """
        self.timezone = timezone
        self.sleep_start = sleep_start
        self.sleep_end = sleep_end
        self._tz = pytz.timezone(timezone)

        # Enhanced features
        self.accuracy_window = accuracy_window
        self.use_dynamic_weights = use_dynamic_weights
        self.use_position_sizing = use_position_sizing
        self.use_regime_selection = use_regime_selection

        # Rolling accuracy tracking per strategy
        self._accuracy_history: dict[str, deque[bool]] = {
            "dual_momentum": deque(maxlen=accuracy_window),
            "mean_reversion": deque(maxlen=accuracy_window),
            "multi_timeframe": deque(maxlen=accuracy_window),
        }

        # Track previous predictions for accuracy calculation
        self._pending_predictions: dict[str, tuple[str, str | None]] = {}  # strategy -> (action, asset)
        self._last_prices: dict[str, float] = {}  # asset -> price at prediction time

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

    def _detect_regime(self, market_context: dict[str, Any]) -> MarketRegime:
        """Detect market regime from context.

        Args:
            market_context: Dictionary with market conditions.
                Expected keys: "drawdown", "volatility", "return_50d" or regime string.

        Returns:
            The detected MarketRegime.
        """
        # Check if regime is directly provided
        regime_str = market_context.get("regime", "")

        if "volatile" in regime_str and "bear" in regime_str:
            return MarketRegime.VOLATILE_BEAR
        elif "volatile" in regime_str and "bull" in regime_str:
            return MarketRegime.VOLATILE_BULL
        elif "strong_bear" in regime_str or regime_str == "bear":
            return MarketRegime.BEAR
        elif "strong_bull" in regime_str or regime_str == "bull":
            return MarketRegime.BULL
        elif "sideways" in regime_str:
            return MarketRegime.SIDEWAYS

        # Infer from drawdown and volatility
        drawdown = market_context.get("drawdown", 0.0)
        volatility = market_context.get("volatility", "normal")
        is_volatile = volatility == "extreme"

        if drawdown > 0.10:
            return MarketRegime.VOLATILE_BEAR if is_volatile else MarketRegime.BEAR
        elif drawdown > 0.05:
            return MarketRegime.SIDEWAYS
        else:
            return MarketRegime.VOLATILE_BULL if is_volatile else MarketRegime.BULL

    def _get_dynamic_weights(
        self, regime: MarketRegime
    ) -> dict[str, float]:
        """Get strategy weights adjusted for accuracy and regime.

        Args:
            regime: The current market regime.

        Returns:
            Dictionary mapping strategy names to weights (sum to 1.0).
        """
        # Start with regime-based weights if enabled
        if self.use_regime_selection:
            weights = dict(self.REGIME_WEIGHTS.get(regime, self.BASE_WEIGHTS))
        else:
            weights = dict(self.BASE_WEIGHTS)

        # Adjust based on rolling accuracy if enabled
        if self.use_dynamic_weights:
            for strategy_name, history in self._accuracy_history.items():
                if len(history) >= 5:  # Need at least 5 data points
                    accuracy = sum(history) / len(history)

                    # Adjust weight: good accuracy (>60%) gets boost, poor (<40%) gets penalty
                    if accuracy > 0.60:
                        weights[strategy_name] *= 1.2  # 20% boost
                    elif accuracy < 0.40:
                        weights[strategy_name] *= 0.7  # 30% penalty

        # Normalize weights to sum to 1.0
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}

        return weights

    def _calculate_position_size(
        self, confidence: float, strategy_agreement: int, total_strategies: int = 3
    ) -> float:
        """Calculate position size based on confidence and agreement.

        Less conservative than before - only reduce position on very low confidence.

        Args:
            confidence: Overall confidence level (0.0 to 1.0).
            strategy_agreement: Number of strategies that agree on the action.
            total_strategies: Total number of strategies.

        Returns:
            Position size as percentage (0.7 to 1.0).
        """
        if not self.use_position_sizing:
            return 1.0

        # Base position size from agreement
        agreement_ratio = strategy_agreement / total_strategies

        if agreement_ratio >= 1.0:
            # All agree: 100% position
            agreement_factor = 1.0
        elif agreement_ratio >= 0.67:
            # 2/3 agree: 90-95% position
            agreement_factor = 0.92
        else:
            # Disagreement: 80-85% position (less conservative)
            agreement_factor = 0.82

        # Adjust by confidence (0.85 to 1.0 multiplier) - narrower range
        confidence_factor = 0.85 + (confidence * 0.15)

        # Final position size (min 70%, max 100%)
        position_size = agreement_factor * confidence_factor
        return max(0.70, min(1.0, position_size))

    def update_accuracy(
        self,
        strategy_name: str,
        was_correct: bool,
    ) -> None:
        """Update the rolling accuracy for a strategy.

        Call this after observing whether a strategy's prediction was correct.

        Args:
            strategy_name: Name of the strategy.
            was_correct: Whether the prediction led to positive outcome.
        """
        if strategy_name in self._accuracy_history:
            self._accuracy_history[strategy_name].append(was_correct)

    def get_strategy_accuracies(self) -> dict[str, float]:
        """Get current rolling accuracy for each strategy.

        Returns:
            Dictionary mapping strategy names to accuracy (0.0 to 1.0).
        """
        accuracies = {}
        for name, history in self._accuracy_history.items():
            if history:
                accuracies[name] = sum(history) / len(history)
            else:
                accuracies[name] = 0.5  # Default to 50% if no history
        return accuracies

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
        self,
        signals: dict[str, dict[str, Any]],
        market_context: dict[str, Any],
        regime: MarketRegime,
    ) -> tuple[SignalAction, str | None, float, int]:
        """Select the best action from the signals using dynamic weighting.

        Uses a regime-aware, accuracy-adjusted weighted voting system:
        1. Get weights based on regime and rolling accuracy.
        2. Multiply each strategy's vote by its weight and confidence.
        3. Select the action with highest weighted score.

        Args:
            signals: Strategy signals with "action", "confidence", and optionally
                "asset_symbol" keys.
            market_context: Market context.
            regime: The detected market regime.

        Returns:
            Tuple of (action, asset_symbol, confidence, agreement_count).
        """
        if not signals:
            return SignalAction.HOLD, None, 0.5, 0

        # Get dynamic weights based on regime and accuracy
        strategy_weights = self._get_dynamic_weights(regime)

        # Collect actions with their weighted scores
        action_weights: dict[str, float] = {
            "hold": 0.0,
            "buy": 0.0,
            "sell": 0.0,
        }
        asset_symbols: dict[str, str] = {}
        action_votes: dict[str, int] = {"hold": 0, "buy": 0, "sell": 0}

        for strategy_name, signal in signals.items():
            action = signal.get("action", "hold")
            confidence = signal.get("confidence", 0.5)
            asset_symbol = signal.get("asset_symbol")
            strategy_weight = strategy_weights.get(strategy_name, 0.33)

            # Skip strategies with very low weight (effectively disabled)
            if strategy_weight < self.DISABLE_THRESHOLD:
                continue

            if action in action_weights:
                # Weight = strategy_weight * confidence
                weighted_vote = strategy_weight * confidence
                action_weights[action] += weighted_vote
                action_votes[action] += 1

                if asset_symbol and action != "hold":
                    # Prefer asset from higher-weighted strategy
                    if action not in asset_symbols:
                        asset_symbols[action] = asset_symbol

        # Find the action with highest weighted score
        best_action = max(action_weights, key=lambda k: action_weights[k])
        total_weight = sum(action_weights.values())

        # Calculate normalized confidence for the best action
        if total_weight > 0:
            confidence = action_weights[best_action] / total_weight
        else:
            confidence = 0.5

        # Count how many strategies agreed on the best action
        agreement_count = action_votes[best_action]

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
            agreement_count,
        )

    def analyze(
        self,
        signals: dict[str, dict[str, Any]],
        market_context: dict[str, Any] | None = None,
    ) -> AgentDecision:
        """Analyze strategy signals and produce a decision.

        This is the main entry point for the orchestrator. It:
        1. Detects the market regime.
        2. Classifies the decision type based on strategy agreement.
        3. Determines urgency from market conditions.
        4. Calculates timeout based on type and urgency.
        5. Selects the best action using dynamic regime-aware weighting.
        6. Calculates position size based on confidence and agreement.
        7. Determines if approval is needed.

        Args:
            signals: Dictionary mapping strategy names to their signals.
            market_context: Optional market context with conditions.

        Returns:
            An AgentDecision with all relevant information.
        """
        if market_context is None:
            market_context = {}

        # Detect market regime
        regime = self._detect_regime(market_context)

        # Classify and determine urgency
        decision_type = self._classify_decision(signals)
        urgency = self._determine_urgency(signals, market_context)

        # High urgency conditions can upgrade to URGENT decision type
        if urgency == Urgency.HIGH:
            decision_type = DecisionType.URGENT

        # Calculate timeout
        timeout_hours = self._calculate_timeout(decision_type, urgency)

        # Select the best action using dynamic weights
        action, asset_symbol, confidence, agreement_count = self._select_best_action(
            signals, market_context, regime
        )

        # Calculate position size based on confidence and agreement
        position_size = self._calculate_position_size(
            confidence, agreement_count, total_strategies=len(signals)
        )

        # Determine if approval is needed
        # ROUTINE decisions don't need approval
        # NON_ROUTINE and URGENT decisions need approval (unless auto-execute conditions)
        requires_approval = decision_type != DecisionType.ROUTINE

        # Build reasoning with regime info
        regime_info = f"[{regime.value.upper()}] "
        weights = self._get_dynamic_weights(regime)
        weights_str = ", ".join(f"{k}:{v:.0%}" for k, v in weights.items())

        if decision_type == DecisionType.ROUTINE:
            reasoning = f"{regime_info}All strategies agree on {action.value.upper()} action (weights: {weights_str})"
        elif decision_type == DecisionType.URGENT:
            reasoning = f"{regime_info}Urgent condition detected. Recommended action: {action.value.upper()} (position: {position_size:.0%})"
        else:
            actions = [s.get("action", "unknown") for s in signals.values()]
            reasoning = f"{regime_info}Strategy disagreement: {actions}. Weighted vote: {action.value.upper()} ({agreement_count}/3 agree, position: {position_size:.0%})"

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
            position_size_pct=position_size,
            regime=regime,
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
