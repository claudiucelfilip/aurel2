"""AI Advisor with failure learning integration.

This module provides the AIAdvisor class that reviews deterministic decisions
and may override them based on historical failure patterns.
"""

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import structlog

from aurel2.agent.ai_evaluator import ClaudeCodeExpertEvaluator
from aurel2.agent.failure_analyzer import FailureAnalysis
from aurel2.core.assets import ASSET_REGISTRY

logger = structlog.get_logger()

# Default path for failure learnings
DEFAULT_FAILURE_FILE = "data/failure_learnings.json"

# AI evaluator guardrails
MAX_CONSECUTIVE_AI_FAILURES = 3  # Skip AI after this many consecutive failures
AI_RETRY_COOLDOWN_HOURS = 1  # Wait this long before retrying after max failures


@dataclass
class AIAdvice:
    """Result of AI advisor review."""

    agrees_with_deterministic: bool
    recommended_action: str  # buy, sell, hold
    recommended_asset: str | None
    confidence: float
    reasoning: str
    failure_patterns_detected: list[str]
    # Original deterministic decision for comparison
    deterministic_action: str
    deterministic_asset: str | None


class AIAdvisor:
    """AI advisor that reviews deterministic decisions with failure learning.

    The advisor:
    1. Loads historical failure patterns
    2. Reviews the deterministic decision against market context
    3. Either agrees or suggests an override based on failure patterns
    4. Only overrides in rare, high-conviction cases

    This is the "conservative risk manager" that trusts the momentum system
    but intervenes when it recognizes dangerous patterns from the past.
    """

    def __init__(
        self,
        failure_file: str = DEFAULT_FAILURE_FILE,
        model: str = "sonnet",
        lookback_years: int = 5,
    ):
        """Initialize the AI advisor.

        Args:
            failure_file: Path to failure learnings JSON file.
            model: Claude model to use via CLI (sonnet, opus, haiku).
            lookback_years: Only use failures from the last N years. Default 5.
                           Set to None to use all historical failures.
        """
        self.failure_file = failure_file
        self.model = model
        self.lookback_years = lookback_years

        # Load failure analysis
        self.failure_analysis: FailureAnalysis | None = None
        self._load_failure_analysis()

        # Initialize AI evaluator (always uses Claude Code CLI)
        self.ai_evaluator: ClaudeCodeExpertEvaluator | None = None

        # AI failure tracking for guardrails
        self._consecutive_ai_failures = 0
        self._last_ai_failure_time: datetime | None = None
        self._ai_disabled_until: datetime | None = None

    def _load_failure_analysis(self) -> None:
        """Load failure analysis from file if available."""
        if os.path.exists(self.failure_file):
            try:
                self.failure_analysis = FailureAnalysis.load(self.failure_file)
                logger.info(
                    "loaded_failure_learnings",
                    filepath=self.failure_file,
                    num_failures=len(self.failure_analysis.failure_events),
                )
            except Exception as e:
                logger.warning("failed_to_load_failure_analysis", error=str(e))
        else:
            logger.warning(
                "no_failure_learnings_found",
                filepath=self.failure_file,
                message="Run `aurel2 analyze-failures` to generate learnings",
            )

    def reload_failure_analysis(self) -> bool:
        """Reload failure analysis from file.

        Returns:
            True if successfully loaded, False otherwise
        """
        previous_count = len(self.failure_analysis.failure_events) if self.failure_analysis else 0

        self._load_failure_analysis()

        current_count = len(self.failure_analysis.failure_events) if self.failure_analysis else 0

        if current_count != previous_count:
            logger.info(
                "failure_analysis_reloaded",
                previous_count=previous_count,
                current_count=current_count,
            )

        return self.failure_analysis is not None

    def is_failure_data_stale(self, max_age_hours: int = 24) -> bool:
        """Check if failure data is stale.

        Args:
            max_age_hours: Maximum age in hours before considered stale

        Returns:
            True if data is stale or missing
        """
        from datetime import datetime, timedelta

        if not os.path.exists(self.failure_file):
            return True

        file_mtime = datetime.fromtimestamp(os.path.getmtime(self.failure_file))
        age = datetime.now() - file_mtime

        return age > timedelta(hours=max_age_hours)

    def _should_skip_ai(self) -> tuple[bool, str]:
        """Check if AI evaluation should be skipped due to repeated failures.

        Returns:
            Tuple of (should_skip, reason)
        """
        now = datetime.now()

        # Check if we're in cooldown period after max failures
        if self._ai_disabled_until and now < self._ai_disabled_until:
            remaining = (self._ai_disabled_until - now).total_seconds() / 60
            return True, f"AI disabled for {remaining:.0f} more minutes after {MAX_CONSECUTIVE_AI_FAILURES} failures"

        # If cooldown expired, reset the failure counter
        if self._ai_disabled_until and now >= self._ai_disabled_until:
            logger.info("ai_cooldown_expired_retrying")
            self._ai_disabled_until = None
            self._consecutive_ai_failures = 0

        return False, ""

    def _record_ai_success(self) -> None:
        """Record successful AI evaluation, reset failure counter."""
        if self._consecutive_ai_failures > 0:
            logger.info(
                "ai_recovered",
                previous_failures=self._consecutive_ai_failures,
            )
        self._consecutive_ai_failures = 0
        self._last_ai_failure_time = None
        self._ai_disabled_until = None

    def _record_ai_failure(self, error: str) -> None:
        """Record AI evaluation failure and potentially disable AI temporarily."""
        self._consecutive_ai_failures += 1
        self._last_ai_failure_time = datetime.now()

        logger.warning(
            "ai_evaluation_failure_recorded",
            consecutive_failures=self._consecutive_ai_failures,
            max_before_disable=MAX_CONSECUTIVE_AI_FAILURES,
            error=error[:100],
        )

        # Disable AI after too many consecutive failures
        if self._consecutive_ai_failures >= MAX_CONSECUTIVE_AI_FAILURES:
            self._ai_disabled_until = datetime.now() + timedelta(hours=AI_RETRY_COOLDOWN_HOURS)
            logger.warning(
                "ai_temporarily_disabled",
                consecutive_failures=self._consecutive_ai_failures,
                disabled_until=self._ai_disabled_until.isoformat(),
                cooldown_hours=AI_RETRY_COOLDOWN_HOURS,
            )

    def _init_evaluator(self) -> None:
        """Lazy initialization of AI evaluator. Always uses Claude Code CLI."""
        if self.ai_evaluator is None:
            try:
                self.ai_evaluator = ClaudeCodeExpertEvaluator(model=self.model)
                logger.info("using_claude_code_cli_evaluator", model=self.model)
            except Exception as e:
                logger.error("failed_to_init_ai_evaluator", error=str(e))
                raise

    def _build_context(
        self,
        prices: pd.DataFrame,
        market_context: dict[str, Any],
        target_date: date,
    ) -> str:
        """Build context string for AI evaluation.

        Args:
            prices: Price data DataFrame.
            market_context: Market context from MCP server.
            target_date: The decision date.

        Returns:
            Formatted context string.
        """
        lines = []

        # Add failure history (point-in-time safe, with lookback window)
        if self.failure_analysis:
            failure_context = self.failure_analysis.to_prompt_text(
                as_of_date=target_date,
                lookback_years=self.lookback_years,
            )
            lines.append(failure_context)
            lines.append("\n---\n")

        # Add market context
        lines.append("CURRENT MARKET CONTEXT:")
        lines.append(f"Date: {target_date}")
        lines.append(f"Regime: {market_context.get('regime', 'unknown').upper()}")

        spy_price = market_context.get("spy_price")
        if spy_price:
            lines.append(f"SPY Price: ${spy_price:.2f}")

        ma_200 = market_context.get("ma_200")
        if ma_200:
            lines.append(f"200-day MA: ${ma_200:.2f}")

        pct_from_ma = market_context.get("pct_from_ma")
        if pct_from_ma is not None:
            lines.append(f"SPY vs MA: {pct_from_ma:.1%}")

        drawdown = market_context.get("drawdown")
        if drawdown is not None:
            lines.append(f"Drawdown from 52w high: {drawdown:.1%}")

        rsi = market_context.get("rsi")
        if rsi is not None:
            rsi_interp = market_context.get("rsi_interpretation", "neutral")
            lines.append(f"RSI: {rsi:.1f} ({rsi_interp})")

        return "\n".join(lines)

    def _detect_failure_patterns(
        self,
        deterministic_action: str,
        deterministic_asset: str | None,
        market_context: dict[str, Any],
        target_date: date,
    ) -> list[str]:
        """Detect if current situation matches known failure patterns.

        Args:
            deterministic_action: The deterministic signal action.
            deterministic_asset: The deterministic signal asset.
            market_context: Market context.
            target_date: Decision date.

        Returns:
            List of detected pattern descriptions.
        """
        patterns = []

        if not self.failure_analysis:
            return patterns

        # Get recent failures (last 2 years) for pattern matching
        recent_failures = [
            f for f in self.failure_analysis.failure_events
            if f.date < target_date and f.date >= target_date - timedelta(days=730)
        ]

        drawdown = market_context.get("drawdown", 0.0)
        rsi = market_context.get("rsi", 50)
        regime = market_context.get("regime", "neutral")

        for failure in recent_failures:
            # Check for similar market conditions
            failure_drawdown = failure.market_context.get("spy_drawdown", 0)
            failure_vix = failure.market_context.get("vix", 20)

            # High drawdown + defensive position pattern
            if (
                drawdown > 0.10 and
                deterministic_action == "hold" and
                deterministic_asset in ["AGG", "CASH", "bonds", "cash"] and
                failure.failure_type == "missed_opportunity" and
                failure_drawdown > 0.10
            ):
                patterns.append(
                    f"Pattern: Holding defensive in high drawdown. "
                    f"Similar failure on {failure.date}: held {failure.asset_held} "
                    f"while {failure.optimal_asset} gained {failure.optimal_return:.1%}"
                )

            # Bear regime hold pattern
            if (
                regime == "bear" and
                deterministic_action in ["hold", "buy"] and
                failure.failure_type == "bad_hold" and
                "bear" in str(failure.market_context.get("regime", "")).lower()
            ):
                patterns.append(
                    f"Pattern: Holding/buying in bear market. "
                    f"Similar failure on {failure.date}: lost {-failure.actual_return:.1%}"
                )

            # RSI extremes
            if (
                rsi and rsi > 70 and
                deterministic_action == "buy" and
                failure.failure_type in ["late_entry", "bad_hold"]
            ):
                patterns.append(
                    f"Pattern: Buying when RSI overbought ({rsi:.0f}). "
                    f"Past late entries often reversed."
                )

        return list(set(patterns))[:3]  # Limit to top 3 patterns

    def review(
        self,
        deterministic_action: str,
        deterministic_asset: str | None,
        strategy_signals: dict[str, Any],
        market_context: dict[str, Any],
        prices: pd.DataFrame,
        current_holding: str | None = None,
        target_date: date | None = None,
    ) -> AIAdvice:
        """Review a deterministic decision and provide AI advice.

        Args:
            deterministic_action: The deterministic signal (buy/sell/hold).
            deterministic_asset: The recommended asset from deterministic system.
            strategy_signals: Signals from all strategies.
            market_context: Market context from MCP server.
            prices: Price data.
            current_holding: Current portfolio holding.
            target_date: Decision date (defaults to today).

        Returns:
            AIAdvice with the AI's recommendation.
        """
        if target_date is None:
            target_date = date.today()

        logger.info(
            "ai_advisor_reviewing",
            deterministic_action=deterministic_action,
            deterministic_asset=deterministic_asset,
            date=str(target_date),
        )

        # Detect known failure patterns
        failure_patterns = self._detect_failure_patterns(
            deterministic_action=deterministic_action,
            deterministic_asset=deterministic_asset,
            market_context=market_context,
            target_date=target_date,
        )

        if failure_patterns:
            logger.info(
                "failure_patterns_detected",
                num_patterns=len(failure_patterns),
                patterns=failure_patterns,
            )

        # Build context for AI
        context_text = self._build_context(
            prices=prices,
            market_context=market_context,
            target_date=target_date,
        )

        # Transform strategy signals for AI evaluator
        signals_for_ai = {}
        for name, signal in strategy_signals.items():
            if "error" not in signal:
                signals_for_ai[name] = {
                    "action": signal.get("action", "hold"),
                    "asset_symbol": signal.get("asset_symbol") or signal.get("asset"),
                    "confidence": signal.get("confidence", 0.5),
                    "reasoning": signal.get("reason") or signal.get("reasoning", ""),
                }

        deterministic_decision = {
            "action": deterministic_action.lower(),
            "asset": deterministic_asset,
        }

        # Check if AI should be skipped due to repeated failures
        should_skip, skip_reason = self._should_skip_ai()
        if should_skip:
            logger.warning("ai_evaluation_skipped", reason=skip_reason)
            return AIAdvice(
                agrees_with_deterministic=True,
                recommended_action=deterministic_action,
                recommended_asset=deterministic_asset,
                confidence=0.5,
                reasoning=f"AI skipped ({skip_reason}), using deterministic decision",
                failure_patterns_detected=failure_patterns,
                deterministic_action=deterministic_action,
                deterministic_asset=deterministic_asset,
            )

        # Get AI evaluation
        try:
            self._init_evaluator()
            ai_decision = self.ai_evaluator.evaluate(
                signals=signals_for_ai,
                context_text=context_text,
                current_holding=current_holding,
                deterministic_decision=deterministic_decision,
            )

            # Check for failure indicators in the response
            failure_keywords = ["failed", "timed out", "error", "exception"]
            is_failure = ai_decision.confidence == 0.0 and any(
                kw in ai_decision.reasoning.lower() for kw in failure_keywords
            )
            if is_failure:
                # AI returned but with an error
                self._record_ai_failure(ai_decision.reasoning)
                return AIAdvice(
                    agrees_with_deterministic=True,
                    recommended_action=deterministic_action,
                    recommended_asset=deterministic_asset,
                    confidence=0.5,
                    reasoning=f"AI evaluation error ({ai_decision.reasoning}), using deterministic",
                    failure_patterns_detected=failure_patterns,
                    deterministic_action=deterministic_action,
                    deterministic_asset=deterministic_asset,
                )

            # Success! Record it and reset failure counter
            self._record_ai_success()

            ai_action = ai_decision.action.lower()
            ai_asset = ai_decision.asset
            ai_confidence = ai_decision.confidence
            ai_reasoning = ai_decision.reasoning

        except Exception as e:
            logger.error("ai_evaluation_failed", error=str(e))
            self._record_ai_failure(str(e))
            # Fall back to agreeing with deterministic
            return AIAdvice(
                agrees_with_deterministic=True,
                recommended_action=deterministic_action,
                recommended_asset=deterministic_asset,
                confidence=0.5,
                reasoning=f"AI evaluation failed ({e}), defaulting to deterministic",
                failure_patterns_detected=failure_patterns,
                deterministic_action=deterministic_action,
                deterministic_asset=deterministic_asset,
            )

        # Determine if AI agrees
        # Agreement means same action, and if buying, same asset
        agrees = ai_action == deterministic_action.lower()
        if agrees and ai_action == "buy":
            agrees = ai_asset == deterministic_asset

        logger.info(
            "ai_advisor_result",
            agrees=agrees,
            ai_action=ai_action,
            ai_asset=ai_asset,
            ai_confidence=ai_confidence,
            deterministic_action=deterministic_action,
            deterministic_asset=deterministic_asset,
        )

        return AIAdvice(
            agrees_with_deterministic=agrees,
            recommended_action=ai_action,
            recommended_asset=ai_asset,
            confidence=ai_confidence,
            reasoning=ai_reasoning,
            failure_patterns_detected=failure_patterns,
            deterministic_action=deterministic_action,
            deterministic_asset=deterministic_asset,
        )


def run_advisory_check(
    failure_file: str = DEFAULT_FAILURE_FILE,
    notify: bool = False,
    ntfy_topic: str = "aurel2",
) -> AIAdvice:
    """Run a full advisory check for today.

    This is the main entry point for automated checks.

    Args:
        failure_file: Path to failure learnings.
        notify: Whether to send notification on disagreement.
        ntfy_topic: Ntfy topic for notifications.

    Returns:
        AIAdvice with the result.
    """
    from aurel2.mcp.server import Aurel2MCPServer
    from aurel2.notifications.ntfy import NtfyNotifier

    logger.info("starting_advisory_check")

    # Get data from MCP server
    server = Aurel2MCPServer()
    signals_response = server._get_strategy_signals()
    signals = signals_response.get("signals", {})
    market_context = server._get_market_context()
    prices = server._get_prices()

    # Get current holding
    portfolio = server._get_portfolio()
    current_holding = None
    if portfolio.get("holdings"):
        for h in portfolio["holdings"]:
            if h["symbol"] not in ["CASH", "EUR"]:
                current_holding = h["symbol"]
                break

    # Get deterministic decision (dual momentum is primary)
    dm_signal = signals.get("dual_momentum", {})
    deterministic_action = dm_signal.get("action", "hold")
    deterministic_asset = dm_signal.get("asset")

    # Run AI advisory
    advisor = AIAdvisor(failure_file=failure_file)
    advice = advisor.review(
        deterministic_action=deterministic_action,
        deterministic_asset=deterministic_asset,
        strategy_signals=signals,
        market_context=market_context,
        prices=prices,
        current_holding=current_holding,
    )

    # Send notification if disagreement and requested
    if notify and not advice.agrees_with_deterministic:
        notifier = NtfyNotifier(topic=ntfy_topic)
        notifier.send(
            message=(
                f"🤖 AI Override Alert!\n\n"
                f"Deterministic: {advice.deterministic_action.upper()} {advice.deterministic_asset or ''}\n"
                f"AI suggests: {advice.recommended_action.upper()} {advice.recommended_asset or ''}\n\n"
                f"Confidence: {advice.confidence:.0%}\n\n"
                f"Reasoning: {advice.reasoning[:200]}..."
            ),
            title="Aurel2: AI Disagrees",
            priority="high",
            tags=["warning", "robot", "chart_with_upwards_trend"],
        )
        logger.info("disagreement_notification_sent", ntfy_topic=ntfy_topic)

    return advice
