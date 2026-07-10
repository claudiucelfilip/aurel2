"""Agent orchestrator for intelligent decision-making.

This module provides the AgentOrchestrator class that analyzes strategy signals,
classifies decisions, handles sleep hours, and manages the approval flow.

The dual-momentum strategy's own signal drives every trade directly (DM is the
sole live strategy). The orchestrator's remaining jobs: regime detection for
display/notifications, sideways-hold suppression, the correlation guard, and
approval/urgency classification.
"""

from collections import Counter
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


class AgentOrchestrator:
    """Orchestrates agent decisions based on strategy signals.

    The orchestrator analyzes the dual-momentum signal, classifies the
    decision type, determines urgency, and manages the approval workflow.

    Key Logic:
        - ROUTINE: dual_momentum decision -> auto-execute
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
        correlation_guard_enabled: bool = True,
        correlation_threshold: float = 0.50,
        sideways_hold_enabled: bool = True,
        sideways_hold_momentum_threshold: float = 0.20,
        routine_agreement_threshold: float = 2 / 3,
    ) -> None:
        """Initialize the orchestrator.

        Args:
            timezone: The timezone for sleep hour calculations.
            sleep_start: Start time of sleep hours (default 23:00).
            sleep_end: End time of sleep hours (default 08:00).
            correlation_guard_enabled: Redirect bond rotations to GLD/CASH when
                SPY-AGG correlation is high (stocks and bonds falling together).
            correlation_threshold: SPY-AGG correlation above which the guard fires.
            sideways_hold_enabled: Suppress switches in sideways markets unless
                the momentum advantage is overwhelming.
            sideways_hold_momentum_threshold: Minimum momentum advantage (fraction)
                required to allow a switch in a sideways market.
            routine_agreement_threshold: Fraction of signals that must share the
                same action for a decision to be ROUTINE (auto-executed without
                approval). With dual_momentum as the only live signal this is
                always met; kept for single-signal-dict compatibility.
        """
        self.timezone = timezone
        self.sleep_start = sleep_start
        self.sleep_end = sleep_end
        self._tz = pytz.timezone(timezone)

        self.correlation_guard_enabled = correlation_guard_enabled
        self.correlation_threshold = correlation_threshold
        self.sideways_hold_enabled = sideways_hold_enabled
        self.sideways_hold_momentum_threshold = sideways_hold_momentum_threshold
        self.routine_agreement_threshold = routine_agreement_threshold

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

    def analyze(
        self,
        signals: dict[str, dict[str, Any]],
        market_context: dict[str, Any] | None = None,
        current_holding: str | None = None,
    ) -> AgentDecision:
        """Analyze the dual-momentum signal and produce a decision.

        This is the main entry point for the orchestrator. It:
        1. Detects the market regime.
        2. Classifies the decision type based on signal agreement.
        3. Determines urgency from market conditions.
        4. Calculates timeout based on type and urgency.
        5. Takes dual_momentum's action/asset directly.
        6. Applies sideways-hold and the correlation guard.
        7. Determines if approval is needed.

        Args:
            signals: Dictionary mapping strategy names to their signals.
                Must contain a "dual_momentum" entry to produce a trade.
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

        dm = signals.get("dual_momentum", {})
        dm_action_str = dm.get("action", "hold")
        dm_action = SignalAction(dm_action_str) if dm_action_str in ("buy", "sell", "hold") else SignalAction.HOLD
        dm_asset = dm.get("asset_symbol")

        # Sideways-hold: suppress switches in choppy markets unless momentum advantage is large
        sideways_hold_applied = False
        if self.sideways_hold_enabled:
            drawdown = market_context.get("drawdown", 0.0)
            sideways_hold_candidate = (
                current_holding
                and current_holding not in ("CASH", None)
                and dm_action == SignalAction.BUY
                and dm_asset != current_holding
                and 0.05 <= drawdown < 0.15
                and decision_type != DecisionType.URGENT
            )
            if sideways_hold_candidate:
                dm_momentum = dm.get("momentum_scores", {})
                target_mom = dm_momentum.get(dm_asset)
                current_mom = dm_momentum.get(current_holding)

                if target_mom is not None and current_mom is not None and current_mom != 0:
                    advantage = (target_mom - current_mom) / abs(current_mom)
                else:
                    advantage = 0.0

                if advantage <= self.sideways_hold_momentum_threshold:
                    action = SignalAction.HOLD
                    asset_symbol = None
                    confidence = dm.get("confidence", 0.8)
                    sideways_hold_applied = True

        if not sideways_hold_applied:
            action = dm_action
            asset_symbol = dm_asset
            confidence = dm.get("confidence", 0.8)

        # Correlation guard: redirect bond rotations when stocks and bonds are correlated
        correlation_guard_applied = False
        if (
            self.correlation_guard_enabled
            and not sideways_hold_applied
            and action == SignalAction.BUY
            and asset_symbol in BOND_SYMBOLS
        ):
            spy_agg_corr = market_context.get("spy_agg_correlation")
            if spy_agg_corr is not None and spy_agg_corr > self.correlation_threshold:
                dm_momentum = dm.get("momentum_scores", {})
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

        if sideways_hold_applied:
            drawdown = market_context.get("drawdown", 0.0)
            reasoning = f"{regime_info}Sideways-hold: keeping {current_holding} (drawdown {drawdown:.1%}, momentum advantage {advantage:.0%} <= {self.sideways_hold_momentum_threshold:.0%} threshold)"
        elif correlation_guard_applied:
            reasoning = f"{regime_info}Correlation guard: redirected bond rotation to {asset_symbol} (SPY-AGG correlation {spy_agg_corr:.2f} > {self.correlation_threshold:.2f})"
        elif decision_type == DecisionType.URGENT:
            reasoning = f"{regime_info}Urgent condition detected. Recommended action: {action.value.upper()} (position: {position_size:.0%})"
        else:
            reasoning = f"{regime_info}dual_momentum: {action.value.upper()}" + (f" {asset_symbol}" if asset_symbol else "")

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
