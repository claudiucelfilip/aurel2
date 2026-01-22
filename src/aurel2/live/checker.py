"""Single check logic - runs strategy analysis and produces decisions."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import structlog

from aurel2.agent.orchestrator import AgentOrchestrator, AgentDecision, DecisionType
from aurel2.agent.advisor import AIAdvisor, AIAdvice
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy
from aurel2.live.connection import IBKRConnection
from aurel2.live.executor import Executor, ExecutionResult
from aurel2.live.pending import PendingManager, PendingDecision, DecisionUrgency
from aurel2.notifications.ntfy import NtfyNotifier

logger = structlog.get_logger()


@dataclass
class CheckResult:
    """Result of a single check cycle."""

    success: bool
    decision: Optional[AgentDecision] = None
    ai_advice: Optional[AIAdvice] = None
    execution_result: Optional[ExecutionResult] = None
    pending_decision: Optional[PendingDecision] = None
    message: str = ""
    current_holding: Optional[str] = None
    account_value: Optional[float] = None


class Checker:
    """
    Runs a single market check cycle.

    Steps:
    1. Sync positions from IBKR
    2. Fetch market prices
    3. Run all 3 strategies
    4. Orchestrator produces decision
    5. Execute or create pending approval

    Used by both the daemon (scheduled) and manual check command.
    """

    def __init__(
        self,
        connection: IBKRConnection,
        pending_manager: PendingManager,
        ntfy_topic: str = "aurel2",
        dry_run: bool = False,
        use_ai_advisor: bool = True,
        failure_learnings_file: str = "data/failure_learnings.json",
    ):
        self.connection = connection
        self.pending_manager = pending_manager
        self.executor = Executor(connection)
        self.notifier = NtfyNotifier(topic=ntfy_topic)
        self.dry_run = dry_run
        self.use_ai_advisor = use_ai_advisor

        # Initialize strategies
        self.strategies = {
            "dual_momentum": DualMomentumStrategy(assets=ASSET_REGISTRY),
            "mean_reversion": MeanReversionStrategy(),
            "multi_timeframe": MultiTimeframeTrendStrategy(),
        }

        self.orchestrator = AgentOrchestrator()
        self.provider = YahooFinanceProvider()

        # Initialize AI advisor with failure learnings (uses Claude Code CLI)
        self.ai_advisor: Optional[AIAdvisor] = None
        if use_ai_advisor:
            self.ai_advisor = AIAdvisor(
                failure_file=failure_learnings_file,
                model="sonnet",  # Uses Claude Code CLI, not API
            )

    async def run(self) -> CheckResult:
        """Run a single check cycle."""
        logger.info("checker_run_start")

        # Check circuit breaker before execution
        if not self.connection.circuit_breaker.can_execute():
            status = self.connection.circuit_breaker.get_status()
            logger.error("execution_blocked_circuit_open", status=status)

            if self.notifier:
                self.notifier.send(
                    message=f"Trading paused - circuit breaker open after {status['failure_count']} failures",
                    title="Aurel2: Circuit Breaker Open",
                    priority="urgent",
                    tags=["warning", "stop_sign"],
                )

            return CheckResult(
                success=False,
                message=f"Circuit breaker open: {status['failure_count']} consecutive failures",
            )

        # 1. Ensure connection
        if not await self.connection.ensure_connected():
            return CheckResult(
                success=False,
                message="Could not connect to IBKR",
            )

        # 2. Sync positions from IBKR
        positions = await self.connection.get_positions()
        account_summary = await self.connection.get_account_summary()

        current_holding = None
        if positions:
            # Find largest position
            largest = max(positions, key=lambda p: p.market_value)
            if largest.market_value > 100:
                current_holding = largest.symbol

        account_value = account_summary.total_value if account_summary else None

        logger.info(
            "checker_positions_synced",
            current_holding=current_holding,
            account_value=account_value,
            num_positions=len(positions),
        )

        # 3. Fetch market prices
        try:
            prices = await self._fetch_prices()
        except Exception as e:
            logger.error("checker_fetch_prices_error", error=str(e))
            return CheckResult(
                success=False,
                message=f"Failed to fetch prices: {e}",
                current_holding=current_holding,
                account_value=account_value,
            )

        # 4. Run all strategies
        signals = self._run_strategies(prices, current_holding)

        # 5. Get market context
        market_context = self._build_market_context(prices)

        # 6. Orchestrator analysis (deterministic)
        decision = self.orchestrator.analyze(
            signals=signals,
            market_context=market_context,
        )

        logger.info(
            "checker_deterministic_decision",
            decision_type=decision.decision_type.value,
            action=decision.action.value,
            asset=decision.asset_symbol,
            confidence=decision.confidence,
            requires_approval=decision.requires_approval,
        )

        # 7. AI Advisor review (uses failure learnings from past mistakes)
        ai_advice: Optional[AIAdvice] = None
        if self.ai_advisor and self.use_ai_advisor:
            try:
                logger.info("checker_running_ai_advisor")
                ai_advice = self.ai_advisor.review(
                    deterministic_action=decision.action.value,
                    deterministic_asset=decision.asset_symbol,
                    strategy_signals=signals,
                    market_context=market_context,
                    prices=prices,
                    current_holding=current_holding,
                )

                logger.info(
                    "checker_ai_advice",
                    agrees=ai_advice.agrees_with_deterministic,
                    ai_action=ai_advice.recommended_action,
                    ai_asset=ai_advice.recommended_asset,
                    ai_confidence=ai_advice.confidence,
                    failure_patterns=ai_advice.failure_patterns_detected,
                )

                # If AI disagrees and has high confidence, use AI's recommendation
                if not ai_advice.agrees_with_deterministic and ai_advice.confidence > 0.7:
                    logger.info(
                        "checker_ai_override",
                        old_action=decision.action.value,
                        old_asset=decision.asset_symbol,
                        new_action=ai_advice.recommended_action,
                        new_asset=ai_advice.recommended_asset,
                    )
                    # Update decision with AI's recommendation
                    # The decision becomes NON_ROUTINE since AI overrode
                    from aurel2.core.models import SignalAction
                    decision = AgentDecision(
                        decision_type=DecisionType.NON_ROUTINE,
                        action=SignalAction(ai_advice.recommended_action),
                        asset_symbol=ai_advice.recommended_asset,
                        reasoning=f"AI Override: {ai_advice.reasoning}",
                        confidence=ai_advice.confidence,
                        strategy_signals=signals,
                        requires_approval=True,  # Always require approval for AI overrides
                        timeout_hours=1.0,
                        urgency=decision.urgency,
                        market_context=market_context,
                        position_size_pct=decision.position_size_pct,
                        regime=decision.regime,
                    )

            except Exception as e:
                logger.error("checker_ai_advisor_error", error=str(e))
                # Continue with deterministic decision if AI fails

        # 8. Execute or create pending
        if self.dry_run:
            logger.info("checker_dry_run", decision=decision.to_dict())
            return CheckResult(
                success=True,
                decision=decision,
                ai_advice=ai_advice,
                message="Dry run - no action taken",
                current_holding=current_holding,
                account_value=account_value,
            )

        if decision.action.value == "hold":
            # No action needed
            return CheckResult(
                success=True,
                decision=decision,
                ai_advice=ai_advice,
                message="Decision: HOLD - no action taken",
                current_holding=current_holding,
                account_value=account_value,
            )

        if not decision.requires_approval:
            # ROUTINE - auto-execute
            return await self._execute_decision(
                decision, ai_advice, current_holding, account_value
            )
        else:
            # NON_ROUTINE or URGENT - create pending approval
            return await self._create_pending_decision(
                decision, ai_advice, signals, market_context, current_holding, account_value
            )

    async def _fetch_prices(self) -> pd.DataFrame:
        """Fetch price data for all tracked symbols."""
        symbols = get_all_yahoo_symbols()
        end_date = date.today()
        start_date = end_date - timedelta(days=400)  # Need ~13 months for momentum

        logger.info("checker_fetching_prices", symbols=symbols)

        # Run in thread pool since Yahoo Finance is sync
        import asyncio
        loop = asyncio.get_event_loop()
        prices = await loop.run_in_executor(
            None,
            lambda: self.provider.get_multi_prices(symbols, start_date, end_date),
        )

        return prices

    def _run_strategies(
        self, prices: pd.DataFrame, current_holding: Optional[str]
    ) -> dict:
        """Run all strategies and collect signals."""
        signals = {}
        calc_date = date.today()

        # Map current holding to asset class for strategies that need it
        current_asset_class = None
        if current_holding:
            for asset_class, asset in ASSET_REGISTRY.items():
                if asset.symbol == current_holding or asset.yahoo_symbol == current_holding:
                    current_asset_class = asset_class
                    break

        for name, strategy in self.strategies.items():
            try:
                signal = strategy.generate_signal(
                    prices=prices,
                    calc_date=calc_date,
                    current_holding=current_asset_class,
                )

                # Handle both Signal (core/models.py) and StrategySignal (strategies/base.py)
                # Signal has: action, asset, reason, momentum_scores
                # StrategySignal has: action, asset_class, confidence, reasoning

                # Get action
                action = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)

                # Get confidence (StrategySignal has it, Signal doesn't)
                confidence = getattr(signal, 'confidence', 0.8)

                # Get asset symbol - try multiple attribute names
                asset_symbol = None
                if hasattr(signal, 'asset') and signal.asset:
                    asset_symbol = signal.asset.symbol
                elif hasattr(signal, 'asset_class') and signal.asset_class:
                    # Look up symbol from asset class
                    if signal.asset_class in ASSET_REGISTRY:
                        asset_symbol = ASSET_REGISTRY[signal.asset_class].symbol

                # Get reasoning
                reasoning = getattr(signal, 'reasoning', None) or getattr(signal, 'reason', '')

                signals[name] = {
                    "action": action,
                    "confidence": confidence,
                    "asset_symbol": asset_symbol,
                    "reasoning": reasoning,
                }

                logger.info(
                    "checker_strategy_signal",
                    strategy=name,
                    action=action,
                    asset=asset_symbol,
                    confidence=confidence,
                )

            except Exception as e:
                logger.error("checker_strategy_error", strategy=name, error=str(e))
                signals[name] = {
                    "action": "hold",
                    "confidence": 0.0,
                    "error": str(e),
                }

        return signals

    def _build_market_context(self, prices: pd.DataFrame) -> dict:
        """Build market context from price data."""
        context = {}

        try:
            # Get SPY data for market regime detection
            spy_prices = prices[prices["symbol"] == "SPY"].copy()
            if not spy_prices.empty:
                spy_prices = spy_prices.sort_values("date")
                current_price = spy_prices.iloc[-1]["close"]
                context["spy_price"] = current_price

                # Calculate 200-day MA
                if len(spy_prices) >= 200:
                    ma_200 = spy_prices.tail(200)["close"].mean()
                    context["ma_200"] = ma_200

                # Calculate drawdown from 52-week high
                year_high = spy_prices.tail(252)["close"].max()
                drawdown = (current_price / year_high) - 1
                context["drawdown"] = abs(drawdown)

                # Detect regime
                if context["drawdown"] < 0.05:
                    context["regime"] = "bull"
                elif context["drawdown"] < 0.15:
                    context["regime"] = "sideways"
                else:
                    context["regime"] = "bear"

        except Exception as e:
            logger.warning("checker_market_context_error", error=str(e))

        return context

    async def _execute_decision(
        self,
        decision: AgentDecision,
        ai_advice: Optional[AIAdvice],
        current_holding: Optional[str],
        account_value: Optional[float],
    ) -> CheckResult:
        """Execute a ROUTINE decision immediately."""
        logger.info(
            "checker_executing_routine",
            action=decision.action.value,
            asset=decision.asset_symbol,
        )

        result = await self.executor.execute(
            action=decision.action.value,
            symbol=decision.asset_symbol,
            current_holding=current_holding,
        )

        # Send notification
        if result.success:
            self.notifier.send(
                message=(
                    f"Auto-executed: {decision.action.value.upper()} {decision.asset_symbol or ''}\n\n"
                    f"Shares: {result.shares:.2f} @ ${result.fill_price:.2f}\n"
                    f"Reasoning: {decision.reasoning[:150]}"
                ),
                title=f"Aurel2: {decision.action.value.upper()} {decision.asset_symbol or ''}",
                tags=["white_check_mark", "chart_with_upwards_trend"],
                priority="low",
            )
        else:
            self.notifier.send(
                message=(
                    f"Execution FAILED: {decision.action.value.upper()} {decision.asset_symbol or ''}\n\n"
                    f"Error: {result.message}"
                ),
                title="Aurel2: Execution Failed",
                tags=["x", "warning"],
                priority="high",
            )

        return CheckResult(
            success=result.success,
            decision=decision,
            ai_advice=ai_advice,
            execution_result=result,
            message=result.message,
            current_holding=current_holding,
            account_value=account_value,
        )

    async def _create_pending_decision(
        self,
        decision: AgentDecision,
        ai_advice: Optional[AIAdvice],
        signals: dict,
        market_context: dict,
        current_holding: Optional[str],
        account_value: Optional[float],
    ) -> CheckResult:
        """Create a pending decision requiring approval."""
        logger.info(
            "checker_creating_pending",
            decision_type=decision.decision_type.value,
            action=decision.action.value,
            asset=decision.asset_symbol,
        )

        # Map decision type to urgency
        urgency_map = {
            DecisionType.ROUTINE: DecisionUrgency.ROUTINE,
            DecisionType.NON_ROUTINE: DecisionUrgency.NON_ROUTINE,
            DecisionType.URGENT: DecisionUrgency.URGENT,
        }
        urgency = urgency_map.get(decision.decision_type, DecisionUrgency.NON_ROUTINE)

        # Get current price for the decision (for validation before auto-execution)
        original_price = None
        if decision.asset_symbol and self.connection.is_connected:
            try:
                original_price = await self.connection.broker.get_market_price(decision.asset_symbol)
            except Exception:
                pass

        # Build strategy context for display
        strategy_context = []
        for name, sig in signals.items():
            if "error" not in sig:
                strategy_context.append({
                    "name": name.replace("_", " ").title(),
                    "action": sig.get("action", "hold").upper(),
                    "confidence": sig.get("confidence", 0.5),
                })

        # Determine if AI agrees and extract AI info
        ai_agrees = True
        ai_action = None
        ai_asset = None
        ai_reasoning = None
        if ai_advice:
            ai_agrees = ai_advice.agrees_with_deterministic
            ai_action = ai_advice.recommended_action
            ai_asset = ai_advice.recommended_asset
            ai_reasoning = ai_advice.reasoning

        # Create pending decision with AI advice context
        pending = self.pending_manager.create_decision(
            urgency=urgency,
            action=decision.action.value,
            symbol=decision.asset_symbol,
            reasoning=decision.reasoning,
            confidence=decision.confidence,
            deterministic_action=ai_advice.deterministic_action if ai_advice else decision.action.value,
            deterministic_asset=ai_advice.deterministic_asset if ai_advice else decision.asset_symbol,
            ai_agrees=ai_agrees,
            ai_action=ai_action,
            ai_asset=ai_asset,
            ai_reasoning=ai_reasoning,
            strategies=strategy_context,
            market_regime=market_context.get("regime"),
            current_holding=current_holding,
            original_price=original_price,
        )

        # Post to Vercel endpoint
        await self.pending_manager.post_to_approval_endpoint(pending)

        # Send notification with AI context if relevant
        priority = "high" if urgency == DecisionUrgency.URGENT else "default"
        tags = ["warning", "chart_with_upwards_trend"] if urgency == DecisionUrgency.URGENT else ["question", "chart_with_upwards_trend"]

        # Add robot tag if AI is involved
        if ai_advice and not ai_agrees:
            tags.append("robot")

        timeout_mins = pending.timeout_seconds() // 60

        # Build message with AI context
        ai_context = ""
        if ai_advice and not ai_agrees:
            ai_context = f"\n\nAI Override: {ai_advice.recommended_action.upper()} {ai_advice.recommended_asset or ''}\nAI Reasoning: {ai_advice.reasoning[:100]}..."

        self.notifier.send(
            message=(
                f"Approval Required ({urgency.value.upper()})\n\n"
                f"Action: {decision.action.value.upper()} {decision.asset_symbol or ''}\n"
                f"Confidence: {decision.confidence:.0%}\n"
                f"Timeout: {timeout_mins} minutes\n\n"
                f"Reasoning: {decision.reasoning[:150]}..."
                f"{ai_context}\n\n"
                f"Approve/Reject: {pending.approval_url}"
            ),
            title=f"Aurel2: {decision.action.value.upper()} {decision.asset_symbol or ''}",
            tags=tags,
            priority=priority,
            click_url=pending.approval_url,
        )

        return CheckResult(
            success=True,
            decision=decision,
            ai_advice=ai_advice,
            pending_decision=pending,
            message=f"Pending approval: {pending.approval_url}",
            current_holding=current_holding,
            account_value=account_value,
        )
