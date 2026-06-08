"""Single check logic - runs strategy analysis and produces decisions."""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import structlog

from aurel2.agent.orchestrator import AgentOrchestrator, AgentDecision, DecisionType
from aurel2.agent.advisor import AIAdvisor, AIAdvice
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import AssetClass
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy
from aurel2.live.connection import AlpacaConnection
from aurel2.live.executor import Executor, ExecutionResult
from aurel2.live.journal import TradeJournal, journal_path_for_mode
from aurel2.live.pending import PendingManager, PendingDecision, DecisionUrgency
from aurel2.live.trade_recorder import TradeRecorder
from aurel2.notifications.ntfy import NtfyNotifier

logger = structlog.get_logger()


def _trading_days_since(prices: pd.DataFrame, since: date) -> int:
    """Count distinct trading days in ``prices`` strictly after ``since``.

    Used for the min-hold cadence throttle. The latest price date is "today",
    so this is the number of trading days elapsed since the last switch.
    """
    try:
        dates = {d.date() for d in pd.to_datetime(prices["date"])}
    except (KeyError, TypeError, ValueError):
        return 0
    return sum(1 for d in dates if d > since)


def _decision_display_asset(decision: AgentDecision, current_holding: Optional[str]) -> Optional[str]:
    """Human-facing asset label for a decision.

    HOLD means "keep current position" from an operator's perspective, so the
    display asset should be the actual broker holding when available, not the
    orchestrator's internal candidate symbol.
    """
    if decision.action.value == "hold":
        return current_holding or "cash"
    return decision.asset_symbol


def _build_hold_notification_message(
    decision: AgentDecision,
    current_holding: Optional[str],
    account_value: Optional[float],
    market_context: dict,
    signals: dict,
) -> str:
    """Build explicit HOLD notification text.

    Separates current broker state from the orchestrator's candidate/preferred
    asset so operators don't confuse "hold current position" with
    "already holding the candidate asset".
    """
    regime_str = market_context.get("regime", "unknown").upper()
    acct_str = f"Account: ${account_value:,.0f}\n" if account_value else ""
    signals_str = " / ".join(
        f"{name.replace('_', ' ').title()}: {sig.get('action', '?').upper()}"
        for name, sig in signals.items() if "error" not in sig
    )
    current_label = current_holding or "cash"
    candidate_label = decision.asset_symbol or "—"
    action_label = "HOLD current position"

    return (
        f"Current holding: {current_label}\n"
        f"Candidate asset: {candidate_label}\n"
        f"Action: {action_label}\n"
        f"{acct_str}"
        f"Regime: {regime_str}\n"
        f"Signals: {signals_str}\n\n"
        f"{decision.reasoning}"
    )


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
    1. Sync positions from broker
    2. Fetch market prices
    3. Run all 3 strategies
    4. Orchestrator produces decision
    5. Execute or create pending approval

    Used by both the daemon (scheduled) and manual check command.
    """

    def __init__(
        self,
        connection: AlpacaConnection,
        pending_manager: PendingManager,
        ntfy_topic: str = "aurel2",
        dry_run: bool = False,
        use_ai_advisor: bool = False,
        failure_learnings_file: str = "data/failure_learnings.json",
        ai_model: str = "haiku",
        ai_lookback_years: int = 3,
        mode: str = "paper",
    ):
        self.connection = connection
        self.pending_manager = pending_manager
        self.executor = Executor(connection)
        self.notifier = NtfyNotifier(topic=ntfy_topic)
        self.dry_run = dry_run
        self.use_ai_advisor = use_ai_advisor

        # Initialize strategies.
        # 2026-05-07: Switched from RobustQuarterlyStrategy (quarterly gate) to plain
        # DualMomentumStrategy. Daily-cadence backtests showed gate + calm-hold gave
        # up +5.9% CAGR over 5y vs no filters (same max DD). See
        # data/cadence_filter_revalidation_may2026.json.
        no_tlt_assets = {ac: a for ac, a in ASSET_REGISTRY.items() if ac != AssetClass.BONDS_TREASURY}
        self.strategies = {
            "dual_momentum": DualMomentumStrategy(
                assets=no_tlt_assets,
                lookback_months=12,
                switch_threshold=0.02,
                cash_rate=0.0,
                pilot_entry_enabled=False,
            ),
            "mean_reversion": MeanReversionStrategy(),
            # Give multi-timeframe the SAME universe as dual_momentum. Its default
            # universe was only {US_STOCKS, INTL_DEVELOPED, BONDS_AGGREGATE}, so it
            # had no data for sector holdings like XLK and always voted "switch to
            # SPY" — a permanently dead/divergent vote that blocked any majority.
            "multi_timeframe": MultiTimeframeTrendStrategy(
                target_assets=[ac for ac in no_tlt_assets if ac != AssetClass.CASH],
            ),
        }

        self.orchestrator = AgentOrchestrator(calm_market_hold_threshold=0.0)
        self.provider = YahooFinanceProvider()

        # Initialize AI advisor with failure learnings (uses Claude Code CLI)
        self.ai_advisor: Optional[AIAdvisor] = None
        if use_ai_advisor:
            self.ai_advisor = AIAdvisor(
                failure_file=failure_learnings_file,
                model=ai_model,
                lookback_years=ai_lookback_years,
            )

        # Initialize trade journal for audit trail
        self.journal = TradeJournal(filepath=journal_path_for_mode(mode))

        # Shared execution pipeline
        self.trade_recorder = TradeRecorder(
            executor=self.executor,
            connection=self.connection,
            journal=self.journal,
            notifier=self.notifier,
        )

    async def run(self, previous_regime: str | None = None) -> CheckResult:
        """Run a single check cycle.

        Args:
            previous_regime: The regime value from the last check cycle,
                used to detect regime transitions and send notifications.
        """
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
                message="Could not connect to broker",
            )

        # 2. Sync positions from broker
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

        # Min-hold cadence: trading days since the last executed switch, so the
        # orchestrator can throttle churn (daily monitoring, monthly execution).
        last_switch = self.journal.last_switch_date()
        if last_switch is not None:
            market_context["days_since_last_switch"] = _trading_days_since(prices, last_switch)

        # 6. Orchestrator analysis (deterministic)
        decision = self.orchestrator.analyze(
            signals=signals,
            market_context=market_context,
            current_holding=current_holding,
        )

        display_asset = _decision_display_asset(decision, current_holding)

        logger.info(
            "checker_deterministic_decision",
            decision_type=decision.decision_type.value,
            action=decision.action.value,
            asset=display_asset,
            decision_asset=decision.asset_symbol,
            current_holding=current_holding,
            confidence=decision.confidence,
            requires_approval=decision.requires_approval,
        )

        # 6b. Detect regime change and notify
        current_regime = decision.regime.value if decision.regime else None
        if (
            previous_regime is not None
            and current_regime is not None
            and current_regime != previous_regime
        ):
            logger.info(
                "regime_change_detected",
                old_regime=previous_regime,
                new_regime=current_regime,
            )
            explanation = _explain_regime_change(
                previous_regime, current_regime, market_context
            )
            # Determine if conditions are improving or deteriorating
            REGIME_RANK = {"bear": 0, "volatile_bear": 1, "sideways": 2, "volatile_bull": 3, "bull": 4}
            improving = REGIME_RANK.get(current_regime, 2) > REGIME_RANK.get(previous_regime, 2)
            self.notifier.send(
                message=explanation,
                title=f"Regime Change: {previous_regime.upper()} → {current_regime.upper()}",
                tags=["chart_with_upwards_trend" if improving else "chart_with_downwards_trend"],
                priority="default",
            )

        # 7. AI Advisor review (provides risk commentary for all decisions)
        ai_advice: Optional[AIAdvice] = None
        if self.ai_advisor and self.use_ai_advisor:
            # Reload failure learnings if stale
            if self.ai_advisor.is_failure_data_stale(max_age_hours=24):
                logger.warning(
                    "failure_data_stale",
                    file=self.ai_advisor.failure_file,
                )

            # Always reload to get latest
            self.ai_advisor.reload_failure_analysis()

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
                # Asymmetric thresholds: lower bar for safety, higher for opportunity
                # Keep AI overrides aligned with the live universe (NO_TLT).
                SAFETY_ASSETS = {"AGG", "IEF", "SHY", "TIP", "GLD", "CASH"}
                ai_asset = ai_advice.recommended_asset
                override_type = "to_safety" if ai_asset in SAFETY_ASSETS else "to_opportunity"
                OVERRIDE_THRESHOLDS = {"to_safety": 0.65, "to_opportunity": 0.85}
                threshold = OVERRIDE_THRESHOLDS[override_type]
                if not ai_advice.agrees_with_deterministic and ai_advice.confidence > threshold:
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

        # 7b. Record decision in trade journal
        decision_id = str(uuid.uuid4())[:8]

        # Convert strategy signals to serializable format
        signals_for_journal = {}
        for name, signal in signals.items():
            if hasattr(signal, 'to_dict'):
                signals_for_journal[name] = signal.to_dict()
            elif isinstance(signal, dict):
                signals_for_journal[name] = signal
            else:
                signals_for_journal[name] = str(signal)

        self.journal.record_decision(
            decision_id=decision_id,
            action=decision.action.value,
            symbol=display_asset,
            decision_symbol=decision.asset_symbol,
            current_holding_symbol=current_holding,
            confidence=decision.confidence,
            decision_type=decision.decision_type.value,
            strategy_signals=signals_for_journal,
            ai_agrees=ai_advice.agrees_with_deterministic if ai_advice else True,
            ai_action=ai_advice.recommended_action if ai_advice else None,
            ai_asset=ai_advice.recommended_asset if ai_advice else None,
            ai_reasoning=ai_advice.reasoning[:500] if ai_advice and ai_advice.reasoning else None,
            ai_confidence=ai_advice.confidence if ai_advice else 0.0,
            ai_commentary=ai_advice.risk_commentary[:300] if ai_advice and ai_advice.risk_commentary else None,
            failure_patterns=ai_advice.failure_patterns_detected if ai_advice else [],
            market_regime=market_context.get("regime") if market_context else None,
            account_value=account_value,
            current_holding=current_holding,
        )

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
            # No action needed, but notify
            current_label = current_holding or "cash"

            self.notifier.send(
                message=_build_hold_notification_message(
                    decision=decision,
                    current_holding=current_holding,
                    account_value=account_value,
                    market_context=market_context,
                    signals=signals,
                ),
                title=f"Aurel2: HOLD {current_label}",
                tags=["white_check_mark"],
                priority="low",
            )

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
                decision, ai_advice, current_holding, account_value, decision_id
            )
        else:
            # NON_ROUTINE or URGENT - create pending approval
            return await self._create_pending_decision(
                decision, ai_advice, signals, market_context, current_holding, account_value, decision_id
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

                # Include per-asset momentum scores for orchestrator calm-hold escape hatch
                mom_dict = {}
                if hasattr(signal, 'momentum_scores') and signal.momentum_scores:
                    for ac, ms in signal.momentum_scores.items():
                        mom_dict[ms.asset.symbol] = ms.momentum_12m

                signals[name] = {
                    "action": action,
                    "confidence": confidence,
                    "asset_symbol": asset_symbol,
                    "reasoning": reasoning,
                    "momentum_scores": mom_dict,
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

                # SPY-AGG correlation (60-day rolling)
                agg_prices = prices[prices["symbol"] == "AGG"].copy()
                if not agg_prices.empty:
                    agg_prices = agg_prices.sort_values("date")
                    spy_ret = spy_prices.set_index("date")["close"].pct_change()
                    agg_ret = agg_prices.set_index("date")["close"].pct_change()
                    merged = pd.concat([spy_ret.rename("spy"), agg_ret.rename("agg")], axis=1).dropna()
                    if len(merged) >= 60:
                        corr = merged["spy"].tail(120).rolling(60).corr(merged["agg"].tail(120)).iloc[-1]
                        if pd.notna(corr):
                            context["spy_agg_correlation"] = float(corr)

        except Exception as e:
            logger.warning("checker_market_context_error", error=str(e))

        return context

    async def _execute_decision(
        self,
        decision: AgentDecision,
        ai_advice: Optional[AIAdvice],
        current_holding: Optional[str],
        account_value: Optional[float],
        decision_id: str,
    ) -> CheckResult:
        """Execute a ROUTINE decision immediately."""
        logger.info(
            "checker_executing_routine",
            action=decision.action.value,
            asset=decision.asset_symbol,
        )

        result = await self.trade_recorder.execute_and_record(
            action=decision.action.value,
            symbol=decision.asset_symbol,
            decision_id=decision_id,
            current_holding=current_holding,
            position_size_pct=decision.position_size_pct,
            notify_title=f"Aurel2: {decision.action.value.upper()} {decision.asset_symbol or ''}",
            notify_context=f"Auto-executed: {decision.action.value.upper()} {decision.asset_symbol or ''}",
            notify_priority="low",
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
        journal_decision_id: str,
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

        # Create pending decision
        pending = self.pending_manager.create_decision(
            urgency=urgency,
            action=decision.action.value,
            symbol=decision.asset_symbol,
            reasoning=decision.reasoning,
            confidence=decision.confidence,
            deterministic_action=decision.action.value,
            deterministic_asset=decision.asset_symbol,
            strategies=strategy_context,
            market_regime=market_context.get("regime"),
            current_holding=current_holding,
            original_price=original_price,
            position_size_pct=decision.position_size_pct,
            journal_decision_id=journal_decision_id,
        )

        # Post to Vercel endpoint
        await self.pending_manager.post_to_approval_endpoint(pending)

        # Send notification
        priority = "high" if urgency == DecisionUrgency.URGENT else "default"
        tags = ["warning", "chart_with_upwards_trend"] if urgency == DecisionUrgency.URGENT else ["question", "chart_with_upwards_trend"]

        timeout_mins = pending.timeout_seconds() // 60

        # Build strategy signals summary
        signals_str = " / ".join(
            f"{s['name']}: {s['action']}" for s in strategy_context
        )
        # Price and account info
        price_str = f"Price: ${original_price:,.2f}\n" if original_price else ""
        acct_str = f"Account: ${account_value:,.0f}\n" if account_value else ""
        holding_str = f"Current: {current_holding}\n" if current_holding else "Current: cash\n"
        regime_str = market_context.get("regime", "unknown").upper()

        self.notifier.send(
            message=(
                f"{decision.action.value.upper()} {decision.asset_symbol or ''}\n"
                f"{price_str}"
                f"{acct_str}"
                f"{holding_str}"
                f"Regime: {regime_str}\n"
                f"Signals: {signals_str}\n"
                f"Confidence: {decision.confidence:.0%} | "
                f"Position: {decision.position_size_pct:.0%}\n"
                f"Timeout: {timeout_mins}min\n\n"
                f"{decision.reasoning}\n\n"
                f"Approve/Reject: {pending.approval_url}"
            ),
            title=f"Aurel2: {decision.action.value.upper()} {decision.asset_symbol or ''}",
            tags=tags,
            priority=priority,
            click_url=pending.approval_url,
            actions=[
                f"view, Approve, {pending.approval_url}?action=approve",
                f"view, Reject, {pending.approval_url}?action=reject",
            ],
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


def _explain_regime_change(old: str, new: str, market_context: dict) -> str:
    """Build a layman explanation for a market regime transition.

    Uses concrete numbers from market_context (SPY price, drawdown, MA-200)
    to make the notification immediately useful.
    """
    spy_price = market_context.get("spy_price")
    drawdown = market_context.get("drawdown", 0)
    ma_200 = market_context.get("ma_200")

    price_note = f" SPY is at ${spy_price:,.0f}." if spy_price else ""
    ma_note = ""
    if spy_price and ma_200:
        pct_vs_ma = ((spy_price / ma_200) - 1) * 100
        direction = "above" if pct_vs_ma > 0 else "below"
        ma_note = f" Price is {abs(pct_vs_ma):.1f}% {direction} the 200-day moving average."

    transition = (old, new)

    explanations = {
        # Deteriorating
        ("bull", "sideways"): (
            f"Markets are getting choppy.{price_note} SPY has pulled back"
            f" {drawdown:.1%} from its 52-week high.{ma_note}"
            f" The system will be more responsive to momentum shifts."
        ),
        ("bull", "bear"): (
            f"Significant downturn detected.{price_note} SPY is down"
            f" {drawdown:.1%} from its peak.{ma_note}"
            f" The system is actively looking to rotate into safer assets like bonds or gold."
        ),
        ("bull", "volatile_bull"): (
            f"Volatility is picking up in a still-rising market.{price_note}"
            f"{ma_note} The system will tighten its momentum filters."
        ),
        ("bull", "volatile_bear"): (
            f"Sharp sell-off detected.{price_note} SPY is down {drawdown:.1%} from its peak.{ma_note}"
            f" The system is looking to move to safety quickly."
        ),
        ("sideways", "bear"): (
            f"The choppy market has turned into a proper downturn.{price_note}"
            f" SPY is now down {drawdown:.1%} from its peak.{ma_note}"
            f" Defensive positioning is the priority."
        ),
        ("sideways", "bull"): (
            f"Markets are breaking out of the choppy range.{price_note}"
            f"{ma_note} The system will hold positions more patiently."
        ),
        # Recovering
        ("bear", "sideways"): (
            f"The downturn may be stabilizing.{price_note} Drawdown has narrowed"
            f" to {drawdown:.1%}.{ma_note} The system is cautiously watching for a recovery."
        ),
        ("bear", "bull"): (
            f"Market recovery underway.{price_note} SPY is near its highs again.{ma_note}"
            f" The system will hold current positions more patiently."
        ),
        ("bear", "volatile_bull"): (
            f"Strong bounce from the lows.{price_note}{ma_note}"
            f" The system is cautiously re-entering risk assets."
        ),
        ("volatile_bear", "bear"): (
            f"Volatility is subsiding but markets remain weak.{price_note}"
            f" SPY is down {drawdown:.1%} from its peak.{ma_note}"
        ),
        ("volatile_bear", "sideways"): (
            f"Selling pressure is easing.{price_note} Drawdown has narrowed"
            f" to {drawdown:.1%}.{ma_note} The system is watching for trend confirmation."
        ),
        ("volatile_bear", "bull"): (
            f"Sharp recovery from volatile sell-off.{price_note}{ma_note}"
            f" The system will hold positions more patiently."
        ),
        ("volatile_bull", "bull"): (
            f"Volatility is calming down while the uptrend continues.{price_note}{ma_note}"
            f" Steady conditions ahead."
        ),
        ("volatile_bull", "sideways"): (
            f"The volatile rally has stalled.{price_note}"
            f" SPY has pulled back {drawdown:.1%} from its high.{ma_note}"
        ),
        ("volatile_bull", "bear"): (
            f"The volatile rally has failed.{price_note}"
            f" SPY is now down {drawdown:.1%} from its peak.{ma_note}"
            f" The system is rotating to defensive assets."
        ),
        ("sideways", "volatile_bull"): (
            f"Markets are breaking out with increased volatility.{price_note}{ma_note}"
            f" The system is cautiously adding risk."
        ),
        ("sideways", "volatile_bear"): (
            f"The choppy market is turning ugly.{price_note}"
            f" SPY is down {drawdown:.1%} from its peak.{ma_note}"
            f" The system is moving towards safety."
        ),
    }

    explanation = explanations.get(transition)
    if explanation:
        return explanation

    # Fallback for any unmapped transition
    return (
        f"Market regime changed from {old.replace('_', ' ')} to {new.replace('_', ' ')}."
        f"{price_note}{ma_note}"
    )
