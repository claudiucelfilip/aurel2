"""Evaluation runner for comparing deterministic vs AI decisions.

This module provides the EvalRunner class that orchestrates the evaluation
process: fetching context, getting strategy signals, running both
deterministic and AI evaluations, and tracking results.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd
import structlog

from aurel2.agent.ai_evaluator import AIDecision, AIEvaluator, ClaudeCodeEvaluator, ClaudeCodeExpertEvaluator, ExpertAIEvaluator, MockAIEvaluator
from aurel2.agent.context_fetcher import ContextFetcher, MarketContext
from aurel2.agent.eval_cache import DecisionRecord, EvalCache
from aurel2.agent.failure_analyzer import FailureAnalysis, run_failure_analysis
from aurel2.agent.orchestrator import AgentOrchestrator
from aurel2.core.assets import ASSET_REGISTRY, get_asset
from aurel2.core.models import AssetClass, SignalAction
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy

logger = structlog.get_logger()


@dataclass
class EvalResult:
    """Result of evaluating a single decision point.

    Attributes:
        date: Date of the decision.
        deterministic_action: What deterministic approach decided.
        deterministic_asset: Target asset for deterministic.
        ai_action: What AI decided.
        ai_asset: Target asset for AI.
        ai_reasoning: AI's explanation.
        agreed: Whether they agreed.
        context: Market context used.
    """

    date: date
    deterministic_action: str
    deterministic_asset: str | None
    ai_action: str
    ai_asset: str | None
    ai_reasoning: str
    agreed: bool
    context: MarketContext


class EvalRunner:
    """Runs evaluation comparing deterministic vs AI decisions.

    Orchestrates the full evaluation flow:
    1. Fetch price data
    2. Get strategy signals
    3. Get market context
    4. Run deterministic evaluation
    5. Run AI evaluation
    6. Cache and compare results
    """

    def __init__(
        self,
        use_mock_ai: bool = False,
        use_claude_code: bool = False,
        use_expert_mode: bool = False,
        claude_model: str = "sonnet",
        cache_dir: str | None = None,
        use_failure_context: bool = True,
        failure_lookback_years: int = 5,
    ):
        """Initialize the evaluation runner.

        Args:
            use_mock_ai: If True, use mock AI evaluator (no API calls).
            use_claude_code: If True, use Claude Code CLI instead of Anthropic API.
            use_expert_mode: If True, use ExpertAIEvaluator with extended thinking.
            claude_model: Model for Claude Code (sonnet, opus, haiku).
            cache_dir: Directory for caching. If None, uses default.
            use_failure_context: If True, load historical failure analysis for AI context.
            failure_lookback_years: Years of history to analyze for failures.
        """
        # Initialize components
        self.price_provider = YahooFinanceProvider()
        self.context_fetcher = ContextFetcher()
        self.eval_cache = EvalCache()

        # Load historical failure analysis (used for AI context)
        self.failure_analysis: FailureAnalysis | None = None
        self.use_failure_context = use_failure_context
        if use_failure_context and use_expert_mode:
            try:
                from datetime import date as date_type
                failure_start = (date_type.today() - timedelta(days=365 * failure_lookback_years)).isoformat()
                logger.info("loading_failure_analysis", lookback_years=failure_lookback_years)
                self.failure_analysis = run_failure_analysis(start_date=failure_start)
                logger.info("failure_analysis_loaded",
                           num_failures=len(self.failure_analysis.failure_events),
                           total_cost=f"{self.failure_analysis.total_opportunity_cost:.1%}")
            except Exception as e:
                logger.warning("failure_analysis_load_failed", error=str(e))
                self.failure_analysis = None

        # Initialize strategies with tuned parameters
        self.dual_momentum = DualMomentumStrategy(
            assets=ASSET_REGISTRY,
            lookback_months=12,
            switch_threshold=0.04,
        )
        self.mean_reversion = MeanReversionStrategy(
            rsi_oversold=25,
            rsi_overbought=75,
            rsi_extreme_oversold=20,
            rsi_period=14,
        )
        self.multi_timeframe = MultiTimeframeTrendStrategy(
            lookback_months=[1, 3, 6, 12],
            weights=[0.30, 0.30, 0.25, 0.15],
            switch_threshold=0.03,
        )

        # Initialize orchestrator (deterministic)
        self.orchestrator = AgentOrchestrator(
            use_dynamic_weights=False,
            use_position_sizing=False,
            use_regime_selection=False,
        )

        # Initialize AI evaluator
        if use_mock_ai:
            self.ai_evaluator = MockAIEvaluator()
        elif use_expert_mode:
            # Expert mode: prefer API-based evaluator if API key available, else Claude CLI
            import os
            if os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    self.ai_evaluator = ExpertAIEvaluator(use_extended_thinking=False)
                    logger.info("using_api_expert_evaluator")
                except (ImportError, ValueError) as e:
                    logger.warning("api_expert_init_failed", error=str(e))
                    self.ai_evaluator = ClaudeCodeExpertEvaluator(model=claude_model)
            else:
                self.ai_evaluator = ClaudeCodeExpertEvaluator(model=claude_model)
        elif use_claude_code:
            self.ai_evaluator = ClaudeCodeEvaluator(model=claude_model)
        else:
            try:
                self.ai_evaluator = AIEvaluator()
            except (ImportError, ValueError) as e:
                logger.warning("ai_evaluator_init_failed", error=str(e))
                logger.info("falling_back_to_mock_ai")
                self.ai_evaluator = MockAIEvaluator()

    def _fetch_prices(self, start_date: date, end_date: date) -> pd.DataFrame:
        """Fetch price data for all symbols."""
        symbols = ["SPY", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "AGG", "TLT", "GLD", "DBC"]

        # Need extra history for momentum calculations
        extended_start = start_date - timedelta(days=400)

        all_prices = []
        for symbol in symbols:
            try:
                prices = self.price_provider.get_prices(symbol, extended_start, end_date)
                all_prices.append(prices)
            except Exception as e:
                logger.warning("price_fetch_failed", symbol=symbol, error=str(e))

        if not all_prices:
            raise ValueError("No price data fetched")

        return pd.concat(all_prices, ignore_index=True)

    def _get_strategy_signals(
        self,
        prices: pd.DataFrame,
        target_date: date,
        current_holding: AssetClass | None,
    ) -> dict[str, Any]:
        """Get signals from all strategies."""
        # Dual Momentum
        dm_signal = self.dual_momentum.generate_signal(
            prices=prices,
            calc_date=target_date,
            current_holding=current_holding,
        )

        # Mean Reversion
        mr_signal = self.mean_reversion.generate_signal(
            prices=prices,
            calc_date=target_date,
            current_holding=current_holding,
        )

        # Multi-Timeframe
        mtf_signal = self.multi_timeframe.generate_signal(
            prices=prices,
            calc_date=target_date,
            current_holding=current_holding,
        )

        return {
            "dual_momentum": {
                "action": dm_signal.action.value,
                "asset_symbol": dm_signal.asset.symbol if dm_signal.asset else None,
                "asset_class": dm_signal.asset.asset_class.value if dm_signal.asset else None,
                "confidence": 0.7 if dm_signal.action != SignalAction.HOLD else 0.5,
                "reasoning": dm_signal.reason,
            },
            "mean_reversion": {
                "action": mr_signal.action.value,
                "asset_symbol": get_asset(mr_signal.asset_class).symbol if mr_signal.asset_class else None,
                "asset_class": mr_signal.asset_class.value if mr_signal.asset_class else None,
                "confidence": mr_signal.confidence,
                "reasoning": mr_signal.reasoning,
            },
            "multi_timeframe": {
                "action": mtf_signal.action.value,
                "asset_symbol": get_asset(mtf_signal.asset_class).symbol if mtf_signal.asset_class else None,
                "asset_class": mtf_signal.asset_class.value if mtf_signal.asset_class else None,
                "confidence": mtf_signal.confidence,
                "reasoning": mtf_signal.reasoning,
            },
        }

    def _get_deterministic_decision(
        self,
        signals: dict[str, Any],
        market_context: dict[str, Any],
    ) -> tuple[str, str | None]:
        """Get the deterministic decision using weighted voting."""
        decision = self.orchestrator.analyze(signals, market_context)

        action = decision.action.value
        asset = decision.asset_symbol

        return action, asset

    def evaluate_date(
        self,
        target_date: date,
        prices: pd.DataFrame,
        current_holding: AssetClass | None,
        use_cache: bool = True,
    ) -> EvalResult:
        """Evaluate a single date comparing deterministic vs AI.

        Args:
            target_date: Date to evaluate.
            prices: Price data DataFrame.
            current_holding: Current holding, or None if cash.
            use_cache: Whether to use cached decisions.

        Returns:
            EvalResult with comparison.
        """
        logger.info("evaluating_date", date=str(target_date))

        # Get strategy signals
        signals = self._get_strategy_signals(prices, target_date, current_holding)

        # Get market context
        context = self.context_fetcher.fetch(target_date, use_cache=use_cache)
        context_text = context.to_prompt_text()

        # Compute input hash for caching
        current_holding_str = current_holding.value if current_holding else None
        input_hash = self.eval_cache.compute_input_hash(
            signals, context_text, current_holding_str
        )

        # Check cache for existing AI decision
        cached = self.eval_cache.get_cached_decision(target_date, input_hash)
        if cached and cached.ai_decision and use_cache:
            logger.info("using_cached_decision", date=str(target_date))
            return EvalResult(
                date=target_date,
                deterministic_action=cached.deterministic_decision["action"],
                deterministic_asset=cached.deterministic_decision.get("asset"),
                ai_action=cached.ai_decision["action"],
                ai_asset=cached.ai_decision.get("asset"),
                ai_reasoning=cached.ai_reasoning or "",
                agreed=cached.agreed or False,
                context=context,
            )

        # Get deterministic decision
        det_action, det_asset = self._get_deterministic_decision(
            signals,
            {
                "drawdown": context.spy_drawdown / 100 if context.spy_drawdown else 0,
                "volatility": "extreme" if context.vix and context.vix > 25 else "normal",
            },
        )

        deterministic_decision = {"action": det_action, "asset": det_asset}

        # Build context for AI with historical failure analysis (POINT-IN-TIME SAFE)
        # Only include failures from BEFORE the target_date to avoid lookahead bias
        full_context_text = context_text
        if self.failure_analysis and self.use_failure_context:
            # Get failure context only from dates BEFORE target_date (no future leakage!)
            failure_context = self.failure_analysis.to_prompt_text(as_of_date=target_date)
            full_context_text = f"{failure_context}\n\n---\n\n{context_text}"
            logger.info("added_failure_context", as_of_date=str(target_date))

        # Get AI decision
        # Expert evaluators accept deterministic_decision parameter
        if isinstance(self.ai_evaluator, (ExpertAIEvaluator, ClaudeCodeExpertEvaluator)):
            ai_decision: AIDecision = self.ai_evaluator.evaluate(
                signals, full_context_text, current_holding_str, deterministic_decision
            )
        else:
            ai_decision: AIDecision = self.ai_evaluator.evaluate(
                signals, full_context_text, current_holding_str
            )

        # Check if they agreed
        agreed = (
            det_action == ai_decision.action
            and (det_action != "buy" or det_asset == ai_decision.asset)
        )

        # Create and cache decision record
        record = DecisionRecord(
            date=target_date,
            input_hash=input_hash,
            strategy_signals=signals,
            market_context_summary=context_text,
            current_holding=current_holding_str,
            deterministic_decision={"action": det_action, "asset": det_asset},
            ai_decision=ai_decision.to_dict(),
            ai_reasoning=ai_decision.reasoning,
            ai_model=self.ai_evaluator.model,
            agreed=agreed,
        )
        self.eval_cache.save_decision(record)

        return EvalResult(
            date=target_date,
            deterministic_action=det_action,
            deterministic_asset=det_asset,
            ai_action=ai_decision.action,
            ai_asset=ai_decision.asset,
            ai_reasoning=ai_decision.reasoning,
            agreed=agreed,
            context=context,
        )

    def run_evaluation(
        self,
        weeks: int = 4,
        end_date: date | None = None,
        use_cache: bool = True,
    ) -> list[EvalResult]:
        """Run evaluation for the specified number of weeks.

        Args:
            weeks: Number of weeks to evaluate.
            end_date: End date (defaults to today).
            use_cache: Whether to use cached data.

        Returns:
            List of EvalResult for each week.
        """
        if end_date is None:
            end_date = date.today()

        # Calculate start date (go back N weeks)
        start_date = end_date - timedelta(weeks=weeks)

        logger.info(
            "starting_evaluation",
            start=str(start_date),
            end=str(end_date),
            weeks=weeks,
        )

        # Fetch prices
        prices = self._fetch_prices(start_date, end_date)
        logger.info("fetched_prices", rows=len(prices))

        # Generate weekly evaluation dates (Fridays)
        eval_dates = pd.date_range(start=start_date, end=end_date, freq="W-FRI")
        eval_dates = [d.date() for d in eval_dates]

        results = []
        current_holding: AssetClass | None = None  # Start in cash

        for eval_date in eval_dates:
            try:
                result = self.evaluate_date(
                    eval_date, prices, current_holding, use_cache
                )
                results.append(result)

                # Update current holding based on deterministic decision
                # (This simulates what we'd actually be holding)
                if result.deterministic_action == "buy" and result.deterministic_asset:
                    for ac, asset in ASSET_REGISTRY.items():
                        if asset.symbol == result.deterministic_asset:
                            current_holding = ac
                            break
                elif result.deterministic_action == "sell":
                    current_holding = None

            except Exception as e:
                logger.error("evaluation_failed", date=str(eval_date), error=str(e))

        return results

    def print_results(self, results: list[EvalResult]) -> None:
        """Print evaluation results summary."""
        if not results:
            print("No results to display.")
            return

        print("\n" + "=" * 80)
        print("AI vs DETERMINISTIC EVALUATION RESULTS")
        print("=" * 80)

        agreements = sum(1 for r in results if r.agreed)
        disagreements = len(results) - agreements

        print(f"\nPeriod: {results[0].date} to {results[-1].date}")
        print(f"Total Decisions: {len(results)}")
        print(f"Agreements: {agreements} ({agreements/len(results)*100:.0f}%)")
        print(f"Disagreements: {disagreements} ({disagreements/len(results)*100:.0f}%)")

        print("\n" + "-" * 80)
        print("DECISION DETAILS:")
        print("-" * 80)

        for r in results:
            status = "✓ AGREE" if r.agreed else "✗ DIFFER"
            det = f"{r.deterministic_action.upper()}"
            if r.deterministic_asset:
                det += f" {r.deterministic_asset}"
            ai = f"{r.ai_action.upper()}"
            if r.ai_asset:
                ai += f" {r.ai_asset}"

            print(f"\n{r.date} [{status}]")
            print(f"  Deterministic: {det}")
            print(f"  AI:            {ai}")
            if not r.agreed:
                print(f"  AI Reasoning:  {r.ai_reasoning[:100]}...")

        print("\n" + "=" * 80)

    def update_outcomes(
        self,
        results: list[EvalResult],
        prices: pd.DataFrame,
    ) -> None:
        """Update decision records with 1-week forward outcomes.

        Call this after 1 week has passed to measure which approach was better.

        Args:
            results: List of EvalResult to update.
            prices: Updated price data including the outcome period.
        """
        for result in results:
            outcome_date = result.date + timedelta(days=7)

            # Get prices
            def get_price(symbol: str, target_date: date) -> float | None:
                sym_prices = prices[prices["symbol"] == symbol].copy()
                sym_prices["date"] = pd.to_datetime(sym_prices["date"])
                sym_prices = sym_prices[sym_prices["date"] <= pd.Timestamp(target_date)]
                if sym_prices.empty:
                    return None
                return float(sym_prices.iloc[-1]["close"])

            # Calculate outcomes for both approaches
            det_return = None
            ai_return = None

            if result.deterministic_asset:
                entry_price = get_price(result.deterministic_asset, result.date)
                exit_price = get_price(result.deterministic_asset, outcome_date)
                if entry_price and exit_price:
                    det_return = (exit_price / entry_price - 1) * 100

            if result.ai_asset:
                entry_price = get_price(result.ai_asset, result.date)
                exit_price = get_price(result.ai_asset, outcome_date)
                if entry_price and exit_price:
                    ai_return = (exit_price / entry_price - 1) * 100

            # Find and update the cached decision
            # We need to recompute the hash
            context = self.context_fetcher.fetch(result.date, use_cache=True)
            signals = self._get_strategy_signals(
                prices, result.date, None  # We don't track holding state here
            )
            current_holding_str = None  # Simplified
            input_hash = self.eval_cache.compute_input_hash(
                signals, context.to_prompt_text(), current_holding_str
            )

            self.eval_cache.update_outcome(
                result.date,
                input_hash,
                outcome_1w=det_return or 0,
                outcome_deterministic_1w=det_return,
                outcome_ai_1w=ai_return,
            )

            logger.info(
                "updated_outcome",
                date=str(result.date),
                det_return=f"{det_return:.2f}%" if det_return else "N/A",
                ai_return=f"{ai_return:.2f}%" if ai_return else "N/A",
            )

    def calculate_all_outcomes(self) -> dict[str, Any]:
        """Calculate outcomes for all cached decisions using historical prices.

        Returns a dictionary with:
        - total_decisions: Number of decisions analyzed
        - decisions_with_outcomes: Number with calculable outcomes
        - agreement_stats: Stats when AI and deterministic agreed
        - disagreement_stats: Stats when they disagreed
        - ai_total_return: Cumulative return following AI
        - det_total_return: Cumulative return following deterministic
        - ai_wins: Number of times AI was better on disagreements
        - det_wins: Number of times deterministic was better on disagreements
        """
        # Get all cached decisions
        decisions = self.eval_cache.get_all_decisions()
        if not decisions:
            return {"error": "No cached decisions found"}

        # Sort by date
        decisions.sort(key=lambda d: d.date)

        # Get date range
        start_date = decisions[0].date
        end_date = decisions[-1].date + timedelta(days=14)  # Extra for outcomes

        logger.info(
            "calculating_outcomes",
            start=str(start_date),
            end=str(end_date),
            num_decisions=len(decisions),
        )

        # Fetch prices for the full range
        prices = self._fetch_prices(start_date, end_date)

        def get_price(symbol: str, target_date: date) -> float | None:
            """Get closing price for symbol on or before target date."""
            sym_prices = prices[prices["symbol"] == symbol].copy()
            sym_prices["date"] = pd.to_datetime(sym_prices["date"])
            sym_prices = sym_prices[sym_prices["date"] <= pd.Timestamp(target_date)]
            if sym_prices.empty:
                return None
            return float(sym_prices.iloc[-1]["close"])

        def calculate_return(symbol: str | None, decision_date: date) -> float | None:
            """Calculate 1-week return for a symbol from decision date."""
            if not symbol:
                return 0.0  # Cash/hold = 0 return
            outcome_date = decision_date + timedelta(days=7)
            entry_price = get_price(symbol, decision_date)
            exit_price = get_price(symbol, outcome_date)
            if entry_price and exit_price:
                return (exit_price / entry_price - 1) * 100
            return None

        # Track results
        results = {
            "total_decisions": len(decisions),
            "decisions_with_outcomes": 0,
            "agreements": 0,
            "disagreements": 0,
            "ai_wins": 0,
            "det_wins": 0,
            "ties": 0,
            "ai_total_return": 0.0,
            "det_total_return": 0.0,
            "ai_returns": [],
            "det_returns": [],
            "disagreement_details": [],
        }

        for decision in decisions:
            if not decision.ai_decision:
                continue

            det_action = decision.deterministic_decision.get("action", "hold")
            det_asset = decision.deterministic_decision.get("asset")
            ai_action = decision.ai_decision.get("action", "hold")
            ai_asset = decision.ai_decision.get("asset")

            # Calculate returns
            # For HOLD, we assume holding previous position (simplified: 0 return)
            # For BUY, we calculate return on the bought asset
            # For SELL, we assume going to cash (0 return)

            det_return = None
            ai_return = None

            if det_action == "buy" and det_asset:
                det_return = calculate_return(det_asset, decision.date)
            elif det_action == "hold":
                # If holding, check if we have a position from context
                if decision.current_holding:
                    # Find symbol for current holding
                    for ac, asset in ASSET_REGISTRY.items():
                        if ac.value == decision.current_holding:
                            det_return = calculate_return(asset.symbol, decision.date)
                            break
                else:
                    det_return = 0.0  # Holding cash
            else:  # sell
                det_return = 0.0

            if ai_action == "buy" and ai_asset:
                ai_return = calculate_return(ai_asset, decision.date)
            elif ai_action == "hold":
                if decision.current_holding:
                    for ac, asset in ASSET_REGISTRY.items():
                        if ac.value == decision.current_holding:
                            ai_return = calculate_return(asset.symbol, decision.date)
                            break
                else:
                    ai_return = 0.0
            else:  # sell
                ai_return = 0.0

            if det_return is None or ai_return is None:
                continue

            results["decisions_with_outcomes"] += 1
            results["ai_total_return"] += ai_return
            results["det_total_return"] += det_return
            results["ai_returns"].append(ai_return)
            results["det_returns"].append(det_return)

            # Track agreement vs disagreement
            agreed = decision.agreed
            if agreed:
                results["agreements"] += 1
            else:
                results["disagreements"] += 1

                # Who won?
                if ai_return > det_return + 0.1:  # AI better by >0.1%
                    results["ai_wins"] += 1
                    winner = "AI"
                elif det_return > ai_return + 0.1:  # Det better by >0.1%
                    results["det_wins"] += 1
                    winner = "DET"
                else:
                    results["ties"] += 1
                    winner = "TIE"

                results["disagreement_details"].append({
                    "date": str(decision.date),
                    "det_action": f"{det_action} {det_asset or ''}".strip(),
                    "ai_action": f"{ai_action} {ai_asset or ''}".strip(),
                    "det_return": det_return,
                    "ai_return": ai_return,
                    "winner": winner,
                    "ai_reasoning": decision.ai_reasoning[:100] if decision.ai_reasoning else "",
                })

            # Update cache with outcomes
            self.eval_cache.update_outcome(
                decision.date,
                decision.input_hash,
                outcome_1w=det_return,
                outcome_deterministic_1w=det_return,
                outcome_ai_1w=ai_return,
            )

        # Calculate summary stats
        if results["decisions_with_outcomes"] > 0:
            results["ai_avg_return"] = results["ai_total_return"] / results["decisions_with_outcomes"]
            results["det_avg_return"] = results["det_total_return"] / results["decisions_with_outcomes"]
        else:
            results["ai_avg_return"] = 0
            results["det_avg_return"] = 0

        if results["disagreements"] > 0:
            results["ai_win_rate"] = results["ai_wins"] / results["disagreements"]
            results["det_win_rate"] = results["det_wins"] / results["disagreements"]
        else:
            results["ai_win_rate"] = 0
            results["det_win_rate"] = 0

        return results
