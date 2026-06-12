"""Decision flow analyzer for agent backtest results.

This module provides the DecisionFlowAnalyzer class that analyzes agent decision
patterns from backtest results, providing insights on strategy agreement,
decision type distribution, urgency triggers, and regime performance.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from aurel2.agent.orchestrator import DecisionType
from aurel2.core.models import SignalAction
from aurel2.engine.backtest_agent import AgentBacktestResult, AgentDecisionRecord


@dataclass
class DecisionAnalysis:
    """Results of decision flow analysis.

    Attributes:
        all_agree_pct: Percentage of decisions where all strategies agreed.
        two_agree_pct: Percentage where exactly two strategies agreed.
        all_disagree_pct: Percentage where all strategies disagreed.
        agreement_by_regime: Agreement rates broken down by market regime.
        strategy_accuracy: Accuracy rate for each strategy.
        best_strategy: The strategy that was correct most often.
        routine_pct: Percentage of ROUTINE decisions.
        non_routine_pct: Percentage of NON_ROUTINE decisions.
        urgent_pct: Percentage of URGENT decisions.
        urgent_decisions: Details of urgent decisions and their outcomes.
        urgent_outcome_positive_pct: Percentage of urgent decisions with positive outcomes.
        regime_returns: Returns broken down by market regime.
    """

    # Agreement patterns
    all_agree_pct: float
    two_agree_pct: float
    all_disagree_pct: float
    agreement_by_regime: dict[str, float]

    # Strategy accuracy (which was right most often)
    strategy_accuracy: dict[str, float]  # {"dual_momentum": 0.55, ...}
    best_strategy: str

    # Decision types
    routine_pct: float
    non_routine_pct: float
    urgent_pct: float

    # Urgency analysis
    urgent_decisions: list[dict]
    urgent_outcome_positive_pct: float

    # Regime performance
    regime_returns: dict[str, float]  # {"bull": 0.15, "bear": -0.05, ...}


class DecisionFlowAnalyzer:
    """Analyze agent decision patterns from backtest results.

    Provides insights on:
    - Strategy agreement patterns
    - Decision type distribution over time
    - Urgency triggers and their outcomes
    - Which strategy was "right" most often
    """

    def __init__(self, result: AgentBacktestResult) -> None:
        """Initialize the analyzer with backtest results.

        Args:
            result: The AgentBacktestResult to analyze.
        """
        self.result = result
        self.decisions = result.decisions
        self.equity_curve = result.equity_curve

    def analyze(self) -> DecisionAnalysis:
        """Run full analysis on the backtest results.

        Returns:
            DecisionAnalysis with all computed metrics.
        """
        agreement = self.strategy_agreement_analysis()
        accuracy = self.strategy_accuracy_analysis()
        distribution = self.decision_type_distribution()
        urgency = self.urgency_trigger_analysis()
        regime_perf = self.regime_performance()

        return DecisionAnalysis(
            # Agreement patterns
            all_agree_pct=agreement["all_agree_pct"],
            two_agree_pct=agreement["two_agree_pct"],
            all_disagree_pct=agreement["all_disagree_pct"],
            agreement_by_regime=agreement["agreement_by_regime"],
            # Strategy accuracy
            strategy_accuracy=accuracy["strategy_accuracy"],
            best_strategy=accuracy["best_strategy"],
            # Decision types
            routine_pct=distribution["routine_pct"],
            non_routine_pct=distribution["non_routine_pct"],
            urgent_pct=distribution["urgent_pct"],
            # Urgency analysis
            urgent_decisions=urgency["urgent_decisions"],
            urgent_outcome_positive_pct=urgency["urgent_outcome_positive_pct"],
            # Regime performance
            regime_returns=regime_perf["regime_returns"],
        )

    def strategy_agreement_analysis(self) -> dict:
        """Analyze how often strategies agreed.

        Returns:
            Dictionary with agreement metrics:
            - all_agree_pct: Percentage when all 3 strategies agreed
            - two_agree_pct: Percentage when exactly 2 strategies agreed
            - all_disagree_pct: Percentage when all strategies disagreed
            - agreement_by_regime: Agreement rates by market regime
        """
        if not self.decisions:
            return {
                "all_agree_pct": 0.0,
                "two_agree_pct": 0.0,
                "all_disagree_pct": 0.0,
                "agreement_by_regime": {},
            }

        all_agree_count = 0
        two_agree_count = 0
        all_disagree_count = 0
        total_decisions = len(self.decisions)

        # Track agreement by regime
        regime_all_agree: dict[str, int] = defaultdict(int)
        regime_total: dict[str, int] = defaultdict(int)

        for decision in self.decisions:
            signals = decision.strategy_signals
            actions = self._extract_actions(signals)

            if len(actions) < 3:
                # Not enough strategies to analyze
                continue

            action_counts = Counter(actions)
            unique_actions = len(action_counts)

            regime = self._normalize_regime(decision.market_regime)
            regime_total[regime] += 1

            if unique_actions == 1:
                # All strategies agree
                all_agree_count += 1
                regime_all_agree[regime] += 1
            elif unique_actions == 2:
                # At least two strategies agree
                two_agree_count += 1
            else:
                # All three strategies disagree
                all_disagree_count += 1

        # Calculate percentages
        all_agree_pct = all_agree_count / total_decisions if total_decisions > 0 else 0.0
        two_agree_pct = two_agree_count / total_decisions if total_decisions > 0 else 0.0
        all_disagree_pct = (
            all_disagree_count / total_decisions if total_decisions > 0 else 0.0
        )

        # Calculate agreement by regime
        agreement_by_regime = {}
        for regime, total in regime_total.items():
            if total > 0:
                agreement_by_regime[regime] = regime_all_agree[regime] / total

        return {
            "all_agree_pct": all_agree_pct,
            "two_agree_pct": two_agree_pct,
            "all_disagree_pct": all_disagree_pct,
            "agreement_by_regime": agreement_by_regime,
        }

    def strategy_accuracy_analysis(self) -> dict:
        """Analyze which strategy was most often correct.

        "Correct" = if we had followed only that strategy,
        would the next period have been profitable?

        Returns:
            Dictionary with accuracy metrics:
            - strategy_accuracy: Dict mapping strategy name to accuracy rate
            - best_strategy: Name of the most accurate strategy
        """
        if not self.decisions or self.equity_curve.empty:
            return {
                "strategy_accuracy": {},
                "best_strategy": "unknown",
            }

        strategy_names = ["dual_momentum", "mean_reversion", "multi_timeframe"]
        strategy_correct: dict[str, int] = defaultdict(int)
        strategy_total: dict[str, int] = defaultdict(int)

        # Build date-to-value mapping for easy lookup
        date_to_value = {}
        for _, row in self.equity_curve.iterrows():
            d = row["date"]
            if hasattr(d, "date"):
                d = d.date()
            date_to_value[d] = row["value"]

        # Get sorted dates for next-period lookup
        sorted_dates = sorted(date_to_value.keys())
        date_to_next = {}
        for i, d in enumerate(sorted_dates[:-1]):
            date_to_next[d] = sorted_dates[i + 1]

        for decision in self.decisions:
            decision_date = decision.date
            if decision_date not in date_to_next:
                continue

            next_date = date_to_next[decision_date]
            current_value = date_to_value.get(decision_date)
            next_value = date_to_value.get(next_date)

            if current_value is None or next_value is None or current_value == 0:
                continue

            # Actual market movement
            actual_return = (next_value - current_value) / current_value
            market_went_up = actual_return > 0

            # Check each strategy's recommendation
            signals = decision.strategy_signals
            for strategy_name in strategy_names:
                if strategy_name not in signals:
                    continue

                signal = signals[strategy_name]
                action = signal.get("action", "hold")

                # Strategy is "correct" if:
                # - Recommended BUY and market went up
                # - Recommended SELL and market went down
                # - Recommended HOLD and market was flat-ish (small moves)
                strategy_total[strategy_name] += 1

                if action == "buy" and market_went_up:
                    strategy_correct[strategy_name] += 1
                elif action == "sell" and not market_went_up:
                    strategy_correct[strategy_name] += 1
                elif action == "hold" and abs(actual_return) < 0.01:
                    # HOLD is correct if market moved less than 1%
                    strategy_correct[strategy_name] += 1

        # Calculate accuracy rates
        strategy_accuracy = {}
        for strategy_name in strategy_names:
            total = strategy_total.get(strategy_name, 0)
            if total > 0:
                strategy_accuracy[strategy_name] = strategy_correct[strategy_name] / total
            else:
                strategy_accuracy[strategy_name] = 0.0

        # Find best strategy
        best_strategy = max(strategy_accuracy, key=strategy_accuracy.get) if strategy_accuracy else "unknown"

        return {
            "strategy_accuracy": strategy_accuracy,
            "best_strategy": best_strategy,
        }

    def decision_type_distribution(self) -> dict:
        """Distribution of ROUTINE/NON_ROUTINE/URGENT over time.

        Returns:
            Dictionary with distribution metrics:
            - routine_pct: Percentage of ROUTINE decisions
            - non_routine_pct: Percentage of NON_ROUTINE decisions
            - urgent_pct: Percentage of URGENT decisions
            - monthly_distribution: Dict mapping month to decision type counts
        """
        if not self.decisions:
            return {
                "routine_pct": 0.0,
                "non_routine_pct": 0.0,
                "urgent_pct": 0.0,
                "monthly_distribution": {},
            }

        total = len(self.decisions)
        routine_count = 0
        non_routine_count = 0
        urgent_count = 0

        monthly_dist: dict[str, dict[str, int]] = defaultdict(
            lambda: {"routine": 0, "non_routine": 0, "urgent": 0}
        )

        for decision in self.decisions:
            month_key = f"{decision.date.year}-{decision.date.month:02d}"

            if decision.decision_type == DecisionType.ROUTINE:
                routine_count += 1
                monthly_dist[month_key]["routine"] += 1
            elif decision.decision_type == DecisionType.NON_ROUTINE:
                non_routine_count += 1
                monthly_dist[month_key]["non_routine"] += 1
            elif decision.decision_type == DecisionType.URGENT:
                urgent_count += 1
                monthly_dist[month_key]["urgent"] += 1

        return {
            "routine_pct": routine_count / total if total > 0 else 0.0,
            "non_routine_pct": non_routine_count / total if total > 0 else 0.0,
            "urgent_pct": urgent_count / total if total > 0 else 0.0,
            "monthly_distribution": dict(monthly_dist),
        }

    def urgency_trigger_analysis(self) -> dict:
        """When were URGENT decisions triggered and what happened after?

        Returns:
            Dictionary with urgency analysis:
            - urgent_decisions: List of urgent decision details with outcomes
            - urgent_outcome_positive_pct: Percentage with positive outcomes
            - urgency_triggers: Common patterns that led to urgent decisions
        """
        if not self.decisions or self.equity_curve.empty:
            return {
                "urgent_decisions": [],
                "urgent_outcome_positive_pct": 0.0,
                "urgency_triggers": {},
            }

        # Build date-to-value mapping
        date_to_value = {}
        for _, row in self.equity_curve.iterrows():
            d = row["date"]
            if hasattr(d, "date"):
                d = d.date()
            date_to_value[d] = row["value"]

        # Get sorted dates for lookups
        sorted_dates = sorted(date_to_value.keys())

        # Find position of each date for lookahead
        date_to_idx = {d: i for i, d in enumerate(sorted_dates)}

        urgent_decisions = []
        positive_outcomes = 0
        total_with_outcome = 0

        # Track urgency triggers
        trigger_counts: dict[str, int] = defaultdict(int)

        for decision in self.decisions:
            if decision.decision_type != DecisionType.URGENT:
                continue

            decision_date = decision.date
            decision_idx = date_to_idx.get(decision_date)

            if decision_idx is None:
                continue

            # Track the trigger (regime at time of urgent decision)
            trigger_counts[decision.market_regime] += 1

            # Calculate outcome: return over next 5 periods (or available periods)
            lookahead = 5
            future_idx = min(decision_idx + lookahead, len(sorted_dates) - 1)

            current_value = date_to_value.get(decision_date)
            future_value = date_to_value.get(sorted_dates[future_idx])

            outcome_return = None
            if current_value and future_value and current_value > 0:
                outcome_return = (future_value - current_value) / current_value
                total_with_outcome += 1
                if outcome_return > 0:
                    positive_outcomes += 1

            urgent_decisions.append({
                "date": str(decision_date),
                "action": decision.action.value,
                "confidence": decision.confidence,
                "market_regime": decision.market_regime,
                "reasoning": decision.reasoning,
                "outcome_return": outcome_return,
                "executed": decision.executed,
            })

        urgent_outcome_positive_pct = (
            positive_outcomes / total_with_outcome if total_with_outcome > 0 else 0.0
        )

        return {
            "urgent_decisions": urgent_decisions,
            "urgent_outcome_positive_pct": urgent_outcome_positive_pct,
            "urgency_triggers": dict(trigger_counts),
        }

    def regime_performance(self) -> dict:
        """How did agent perform in different market regimes?

        Returns:
            Dictionary with regime performance:
            - regime_returns: Dict mapping regime to average return
            - regime_decision_counts: Dict mapping regime to decision count
            - best_regime: Regime with highest returns
            - worst_regime: Regime with lowest returns
        """
        if not self.decisions or self.equity_curve.empty:
            return {
                "regime_returns": {},
                "regime_decision_counts": {},
                "best_regime": "unknown",
                "worst_regime": "unknown",
            }

        # Build date-to-value mapping
        date_to_value = {}
        for _, row in self.equity_curve.iterrows():
            d = row["date"]
            if hasattr(d, "date"):
                d = d.date()
            date_to_value[d] = row["value"]

        # Get sorted dates for next-period lookup
        sorted_dates = sorted(date_to_value.keys())
        date_to_next = {}
        for i, d in enumerate(sorted_dates[:-1]):
            date_to_next[d] = sorted_dates[i + 1]

        # Track returns by regime
        regime_returns_list: dict[str, list[float]] = defaultdict(list)

        for decision in self.decisions:
            decision_date = decision.date
            if decision_date not in date_to_next:
                continue

            next_date = date_to_next[decision_date]
            current_value = date_to_value.get(decision_date)
            next_value = date_to_value.get(next_date)

            if current_value is None or next_value is None or current_value == 0:
                continue

            period_return = (next_value - current_value) / current_value
            regime = self._normalize_regime(decision.market_regime)
            regime_returns_list[regime].append(period_return)

        # Calculate average returns by regime
        regime_returns = {}
        for regime, returns in regime_returns_list.items():
            if returns:
                regime_returns[regime] = sum(returns) / len(returns)

        # Calculate cumulative returns by regime (sum of returns, not average)
        regime_cumulative = {}
        for regime, returns in regime_returns_list.items():
            if returns:
                regime_cumulative[regime] = sum(returns)

        # Decision counts by regime
        regime_decision_counts = {
            regime: len(returns) for regime, returns in regime_returns_list.items()
        }

        # Find best and worst regimes (by cumulative return)
        best_regime = "unknown"
        worst_regime = "unknown"
        if regime_cumulative:
            best_regime = max(regime_cumulative, key=regime_cumulative.get)
            worst_regime = min(regime_cumulative, key=regime_cumulative.get)

        return {
            "regime_returns": regime_returns,
            "regime_cumulative_returns": regime_cumulative,
            "regime_decision_counts": regime_decision_counts,
            "best_regime": best_regime,
            "worst_regime": worst_regime,
        }

    def print_analysis(self) -> None:
        """Print formatted analysis report."""
        analysis = self.analyze()

        print("\n" + "=" * 70)
        print("DECISION FLOW ANALYSIS")
        print("=" * 70)

        # Strategy Agreement Section
        print("\nSTRATEGY AGREEMENT PATTERNS")
        print("-" * 70)
        print(f"  All 3 strategies agree:  {analysis.all_agree_pct:.1%}")
        print(f"  Exactly 2 agree:         {analysis.two_agree_pct:.1%}")
        print(f"  All 3 disagree:          {analysis.all_disagree_pct:.1%}")

        if analysis.agreement_by_regime:
            print("\n  Agreement by Market Regime:")
            for regime, pct in sorted(analysis.agreement_by_regime.items()):
                print(f"    {regime:20s}: {pct:.1%}")

        # Strategy Accuracy Section
        print("\nSTRATEGY ACCURACY (which was 'right' most often)")
        print("-" * 70)
        for strategy, accuracy in sorted(
            analysis.strategy_accuracy.items(), key=lambda x: -x[1]
        ):
            marker = " <-- BEST" if strategy == analysis.best_strategy else ""
            print(f"  {strategy:20s}: {accuracy:.1%}{marker}")

        # Decision Type Distribution Section
        print("\nDECISION TYPE DISTRIBUTION")
        print("-" * 70)
        print(f"  ROUTINE:     {analysis.routine_pct:.1%}")
        print(f"  NON_ROUTINE: {analysis.non_routine_pct:.1%}")
        print(f"  URGENT:      {analysis.urgent_pct:.1%}")

        # Urgency Analysis Section
        print("\nURGENCY TRIGGER ANALYSIS")
        print("-" * 70)
        print(f"  Total urgent decisions: {len(analysis.urgent_decisions)}")
        print(f"  Positive outcome rate:  {analysis.urgent_outcome_positive_pct:.1%}")

        if analysis.urgent_decisions:
            print("\n  Recent Urgent Decisions:")
            for ud in analysis.urgent_decisions[-5:]:  # Last 5
                outcome = (
                    f"{ud['outcome_return']:+.2%}"
                    if ud["outcome_return"] is not None
                    else "N/A"
                )
                print(
                    f"    {ud['date']}: {ud['action'].upper()} "
                    f"(regime: {ud['market_regime']}, outcome: {outcome})"
                )

        # Regime Performance Section
        print("\nREGIME PERFORMANCE")
        print("-" * 70)
        if analysis.regime_returns:
            print("  Average period returns by regime:")
            for regime, ret in sorted(analysis.regime_returns.items(), key=lambda x: -x[1]):
                print(f"    {regime:20s}: {ret:+.3%}")

        print("=" * 70)

    def _extract_actions(self, signals: dict) -> list[str]:
        """Extract action strings from strategy signals.

        Args:
            signals: Dictionary of strategy signals.

        Returns:
            List of action strings.
        """
        actions = []
        for strategy_name, signal in signals.items():
            if isinstance(signal, dict) and "action" in signal:
                actions.append(signal["action"])
        return actions

    def _normalize_regime(self, regime: str) -> str:
        """Normalize regime string for grouping.

        Simplifies regimes like 'volatile_bull' to base categories.

        Args:
            regime: Raw regime string.

        Returns:
            Normalized regime string.
        """
        # Remove 'volatile_' prefix for grouping purposes
        if regime.startswith("volatile_"):
            return regime.replace("volatile_", "")
        return regime
