"""Agent orchestrator for intelligent decision-making.

This module provides the AgentOrchestrator class that analyzes strategy signals,
classifies decisions, handles sleep hours, and manages the approval flow.

Enhanced with:
- Dynamic strategy weighting based on rolling accuracy
- Confidence-based position sizing
- Regime-aware strategy selection/disabling
"""

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Any

import pytz

from aurel2.core.models import SignalAction

# Bond ETFs — used by correlation guard to detect stock-bond co-movement
BOND_SYMBOLS = {"AGG", "TLT", "IEF", "SHY", "TIP"}


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
        calm_market_hold_threshold: float = 0.05,
        correlation_guard_enabled: bool = True,
        correlation_threshold: float = 0.50,
        sideways_hold_enabled: bool = True,
        sideways_hold_momentum_threshold: float = 0.20,
        dm_primary_enabled: bool = True,
        routine_agreement_threshold: float = 2 / 3,
        min_hold_enabled: bool = True,
        min_hold_days: int = 21,
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
            correlation_guard_enabled: Redirect bond rotations to GLD/CASH when
                SPY-AGG correlation is high (stocks and bonds falling together).
            correlation_threshold: SPY-AGG correlation above which the guard fires.
            sideways_hold_enabled: Suppress switches in sideways markets unless
                the momentum advantage is overwhelming.
            sideways_hold_momentum_threshold: Minimum momentum advantage (fraction)
                required to allow a switch in a sideways market.
            dm_primary_enabled: If True, dual momentum directly drives trade action
                when available. If False, use weighted multi-strategy voting.
            routine_agreement_threshold: Fraction of strategies that must share the
                same action for a decision to be ROUTINE (auto-executed without
                approval). Default 2/3 — i.e. 2 of 3 strategies agreeing is enough.
            min_hold_enabled: If True, suppress momentum switches until at least
                ``min_hold_days`` trading days have passed since the last switch
                (daily monitoring with monthly execution cadence — Lesson 17).
            min_hold_days: Minimum trading days to hold a position before a new
                switch is allowed (default 21 ≈ one month). Requires the caller to
                pass ``days_since_last_switch`` in ``market_context``.
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
        self.calm_market_hold_threshold = calm_market_hold_threshold
        self.correlation_guard_enabled = correlation_guard_enabled
        self.correlation_threshold = correlation_threshold
        self.sideways_hold_enabled = sideways_hold_enabled
        self.sideways_hold_momentum_threshold = sideways_hold_momentum_threshold
        self.dm_primary_enabled = dm_primary_enabled
        self.routine_agreement_threshold = routine_agreement_threshold
        self.min_hold_enabled = min_hold_enabled
        self.min_hold_days = min_hold_days

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
            ROUTINE if at least ``routine_agreement_threshold`` of strategies share
            the same action (default 2/3), NON_ROUTINE otherwise.
        """
        if not signals:
            return DecisionType.NON_ROUTINE

        # Extract actions from all signals
        actions = [
            signal.get("action") for signal in signals.values() if signal.get("action")
        ]

        if not actions:
            return DecisionType.NON_ROUTINE

        # ROUTINE when a strong-enough majority shares the same action. With 3
        # strategies and a 2/3 threshold, 2 agreeing is enough to auto-execute;
        # only a true 3-way split stays NON_ROUTINE (needs approval).
        top_count = Counter(actions).most_common(1)[0][1]
        if top_count / len(actions) >= self.routine_agreement_threshold:
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
        current_holding: str | None = None,
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

        # Use weighted vote only to detect if any trade signal exists
        voted_action, voted_asset, voted_confidence, agreement_count = self._select_best_action(
            signals, market_context, regime
        )

        # Calm-market hold: don't switch assets when drawdown is low
        # Escape hatch: if held asset has negative 12m momentum, let DM switch through
        calm_hold_applied = False
        calm_hold_candidate = (
            current_holding
            and current_holding not in ("CASH", None)
            and voted_action == SignalAction.BUY
            and voted_asset != current_holding
            and market_context.get("drawdown", 0.0) < self.calm_market_hold_threshold
            and decision_type != DecisionType.URGENT
        )
        if calm_hold_candidate:
            # Check held asset's 12-month momentum from DM signal
            dm_momentum = signals.get("dual_momentum", {}).get("momentum_scores", {})
            held_momentum = dm_momentum.get(current_holding)

            if held_momentum is not None and held_momentum < 0:
                # Negative momentum escape: held asset is losing, let DM switch
                calm_hold_candidate = False
            else:
                action = SignalAction.HOLD
                asset_symbol = None
                confidence = voted_confidence
                calm_hold_applied = True

        # Sideways-hold: suppress switches in choppy markets unless momentum advantage is large
        sideways_hold_applied = False
        if not calm_hold_applied and self.sideways_hold_enabled:
            drawdown = market_context.get("drawdown", 0.0)
            sideways_hold_candidate = (
                current_holding
                and current_holding not in ("CASH", None)
                and voted_action == SignalAction.BUY
                and "dual_momentum" in signals
                and signals["dual_momentum"].get("asset_symbol") != current_holding
                and 0.05 <= drawdown < 0.15
                and decision_type != DecisionType.URGENT
            )
            if sideways_hold_candidate:
                dm_momentum = signals["dual_momentum"].get("momentum_scores", {})
                target_sym = signals["dual_momentum"].get("asset_symbol")
                target_mom = dm_momentum.get(target_sym)
                current_mom = dm_momentum.get(current_holding)

                if target_mom is not None and current_mom is not None and current_mom != 0:
                    advantage = (target_mom - current_mom) / abs(current_mom)
                else:
                    advantage = 0.0

                if advantage <= self.sideways_hold_momentum_threshold:
                    action = SignalAction.HOLD
                    asset_symbol = None
                    confidence = voted_confidence
                    sideways_hold_applied = True

        # Min-hold throttle: daily monitoring, monthly execution (Lesson 17).
        # Suppress a momentum switch until min_hold_days trading days have passed
        # since the last switch, so we don't churn on daily noise. URGENT (crash)
        # decisions bypass the throttle so we can still rotate to safety fast.
        min_hold_applied = False
        if (
            self.min_hold_enabled
            and not calm_hold_applied
            and not sideways_hold_applied
            and decision_type != DecisionType.URGENT
        ):
            days_since_switch = market_context.get("days_since_last_switch")
            dm = signals.get("dual_momentum", {})
            dm_target = dm.get("asset_symbol")
            proposes_switch = (
                current_holding not in ("CASH", None)
                and dm.get("action") == "buy"
                and dm_target not in (None, current_holding)
            )
            if (
                proposes_switch
                and days_since_switch is not None
                and days_since_switch < self.min_hold_days
            ):
                action = SignalAction.HOLD
                asset_symbol = None
                confidence = voted_confidence
                min_hold_applied = True

        if not calm_hold_applied and not sideways_hold_applied and not min_hold_applied:
            if self.dm_primary_enabled and "dual_momentum" in signals:
                # DM-primary mode: use dual momentum signal directly for trade decisions.
                # Other strategies still contribute to decision classification and urgency.
                dm = signals["dual_momentum"]
                dm_action_str = dm.get("action", "hold")
                action = SignalAction(dm_action_str) if dm_action_str in ("buy", "sell", "hold") else SignalAction.HOLD
                asset_symbol = dm.get("asset_symbol")
                confidence = dm.get("confidence", 0.8)
            else:
                # Multi-strategy mode: use weighted vote across enabled strategies.
                action = voted_action
                asset_symbol = voted_asset
                confidence = voted_confidence

        # Correlation guard: redirect bond rotations when stocks and bonds are correlated
        correlation_guard_applied = False
        if (
            self.correlation_guard_enabled
            and not calm_hold_applied
            and not sideways_hold_applied
            and not min_hold_applied
            and action == SignalAction.BUY
            and asset_symbol in BOND_SYMBOLS
        ):
            spy_agg_corr = market_context.get("spy_agg_correlation")
            if spy_agg_corr is not None and spy_agg_corr > self.correlation_threshold:
                dm_momentum = signals.get("dual_momentum", {}).get("momentum_scores", {})
                gld_momentum = dm_momentum.get("GLD")
                if gld_momentum is not None and gld_momentum > 0:
                    asset_symbol = "GLD"
                else:
                    asset_symbol = "CASH"
                correlation_guard_applied = True

        # Full position sizing — DM-primary trades with full conviction
        position_size = 1.0

        # Determine if approval is needed
        # ROUTINE decisions don't need approval
        # NON_ROUTINE and URGENT decisions need approval (unless auto-execute conditions)
        requires_approval = decision_type != DecisionType.ROUTINE

        # Build reasoning with regime info
        regime_info = f"[{regime.value.upper()}] "
        weights = self._get_dynamic_weights(regime)
        weights_str = ", ".join(f"{k}:{v:.0%}" for k, v in weights.items())

        if calm_hold_applied:
            drawdown = market_context.get("drawdown", 0.0)
            reasoning = f"{regime_info}Calm-market hold: keeping {current_holding} (drawdown {drawdown:.1%} < {self.calm_market_hold_threshold:.0%} threshold)"
        elif sideways_hold_applied:
            drawdown = market_context.get("drawdown", 0.0)
            reasoning = f"{regime_info}Sideways-hold: keeping {current_holding} (drawdown {drawdown:.1%}, momentum advantage {advantage:.0%} <= {self.sideways_hold_momentum_threshold:.0%} threshold)"
        elif min_hold_applied:
            days_since_switch = market_context.get("days_since_last_switch")
            reasoning = f"{regime_info}Min-hold throttle: keeping {current_holding} ({days_since_switch}d since last switch < {self.min_hold_days}d cadence)"
        elif correlation_guard_applied:
            reasoning = f"{regime_info}Correlation guard: redirected bond rotation to {asset_symbol} (SPY-AGG correlation {spy_agg_corr:.2f} > {self.correlation_threshold:.2f})"
        elif decision_type == DecisionType.ROUTINE:
            reasoning = f"{regime_info}Majority agree on {action.value.upper()} action ({agreement_count}/{len(signals)} strategies, weights: {weights_str})"
        elif decision_type == DecisionType.URGENT:
            reasoning = f"{regime_info}Urgent condition detected. Recommended action: {action.value.upper()} (position: {position_size:.0%})"
        else:
            dm_note = " (DM-primary)" if self.dm_primary_enabled and "dual_momentum" in signals else ""
            actions = [s.get("action", "unknown") for s in signals.values()]
            total_signals = len(signals)
            reasoning = f"{regime_info}Strategy signals: {actions}. Action: {action.value.upper()}{dm_note} ({agreement_count}/{total_signals} agree)"

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
