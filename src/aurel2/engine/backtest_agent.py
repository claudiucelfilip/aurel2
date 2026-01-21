"""Agent backtesting engine for simulating AI agent decision-making.

This module provides the AgentBacktestEngine class that backtests the AI agent's
decision-making over historical data, simulating all 3 strategies generating signals
and the agent's weighted voting to select actions.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import structlog

from aurel2.agent.orchestrator import AgentOrchestrator, DecisionType
from aurel2.core.assets import ASSET_REGISTRY, get_asset
from aurel2.core.models import Asset, AssetClass, Signal, SignalAction, Trade
from aurel2.strategies.base import StrategySignal
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy

logger = structlog.get_logger()


@dataclass
class AgentDecisionRecord:
    """Record of a decision made during the agent backtest.

    Attributes:
        date: Date the decision was made.
        decision_type: Classification (ROUTINE, NON_ROUTINE, URGENT).
        action: The action taken (BUY, SELL, HOLD).
        asset_class: The asset class traded, or None if no specific asset.
        confidence: Confidence level from 0.0 to 1.0.
        reasoning: Human-readable explanation for the decision.
        strategy_signals: What each strategy recommended.
        market_regime: Description of the market regime at decision time.
        executed: Whether the trade was executed.
    """

    date: date
    decision_type: DecisionType
    action: SignalAction
    asset_class: AssetClass | None
    confidence: float
    reasoning: str
    strategy_signals: dict[str, Any]
    market_regime: str
    executed: bool


@dataclass
class AgentBacktestResult:
    """Results of an agent backtest run.

    Attributes:
        start_date: Start date of the backtest period.
        end_date: End date of the backtest period.
        initial_capital: Starting capital amount.
        final_value: Final portfolio value.
        total_return: Total return as a decimal (0.10 = 10%).
        decisions: All decisions made during the backtest.
        trades: All trades executed.
        equity_curve: DataFrame with date and portfolio value columns.
        routine_count: Number of ROUTINE decisions.
        non_routine_count: Number of NON_ROUTINE decisions.
        urgent_count: Number of URGENT decisions.
        strategy_agreement_rate: Percentage of time all strategies agreed.
        benchmark_return: Buy-and-hold benchmark return (if computed).
        vs_dual_momentum_only: Alpha vs dual momentum only strategy.
        vs_mean_reversion_only: Alpha vs mean reversion only strategy.
        vs_multi_timeframe_only: Alpha vs multi-timeframe only strategy.
    """

    start_date: date
    end_date: date
    initial_capital: float
    final_value: float
    total_return: float
    decisions: list[AgentDecisionRecord]
    trades: list[Trade]
    equity_curve: pd.DataFrame

    # Decision analysis
    routine_count: int = 0
    non_routine_count: int = 0
    urgent_count: int = 0
    strategy_agreement_rate: float = 0.0

    # Comparison metrics
    benchmark_return: float | None = None
    vs_dual_momentum_only: float | None = None
    vs_mean_reversion_only: float | None = None
    vs_multi_timeframe_only: float | None = None

    # Performance metrics
    cagr: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    num_trades: int = 0

    def calculate_metrics(self) -> None:
        """Calculate performance metrics from equity curve."""
        if self.equity_curve.empty:
            return

        values = self.equity_curve["value"].values

        # Total return
        self.total_return = (self.final_value / self.initial_capital) - 1

        # CAGR
        years = (self.end_date - self.start_date).days / 365.25
        if years > 0:
            self.cagr = (self.final_value / self.initial_capital) ** (1 / years) - 1

        # Max drawdown
        peak = values[0]
        max_dd = 0.0
        for v in values:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        self.max_drawdown = max_dd

        # Sharpe ratio (annualized, assuming 0% risk-free for simplicity)
        if len(values) > 1:
            returns = pd.Series(values).pct_change().dropna()
            if len(returns) > 0 and returns.std() > 0:
                # Annualize assuming daily returns
                periods_per_year = 252
                self.sharpe_ratio = (
                    returns.mean() * periods_per_year
                ) / (returns.std() * np.sqrt(periods_per_year))

        # Trade stats
        self.num_trades = len([t for t in self.trades if t.action == SignalAction.BUY])

        # Decision analysis
        self.routine_count = sum(
            1 for d in self.decisions if d.decision_type == DecisionType.ROUTINE
        )
        self.non_routine_count = sum(
            1 for d in self.decisions if d.decision_type == DecisionType.NON_ROUTINE
        )
        self.urgent_count = sum(
            1 for d in self.decisions if d.decision_type == DecisionType.URGENT
        )

        # Strategy agreement rate
        total_decisions = len(self.decisions)
        if total_decisions > 0:
            self.strategy_agreement_rate = self.routine_count / total_decisions

    def print_summary(self) -> None:
        """Print a summary of agent backtest results."""
        print("\n" + "=" * 70)
        print("AGENT BACKTEST RESULTS")
        print("=" * 70)
        print(f"Period: {self.start_date} to {self.end_date}")
        print(f"Initial Capital: ${self.initial_capital:,.2f}")
        print(f"Final Value: ${self.final_value:,.2f}")
        print("-" * 70)
        print(f"Total Return: {self.total_return:.2%}")
        print(f"CAGR: {self.cagr:.2%}")
        print(f"Max Drawdown: {self.max_drawdown:.2%}")
        print(f"Sharpe Ratio: {self.sharpe_ratio:.2f}")
        print(f"Number of Trades: {self.num_trades}")
        print("-" * 70)
        print("DECISION ANALYSIS")
        print(f"  Total Decisions: {len(self.decisions)}")
        print(f"  Routine: {self.routine_count}")
        print(f"  Non-Routine: {self.non_routine_count}")
        print(f"  Urgent: {self.urgent_count}")
        print(f"  Strategy Agreement Rate: {self.strategy_agreement_rate:.1%}")
        print("-" * 70)
        print("COMPARISON VS BENCHMARKS")
        if self.benchmark_return is not None:
            alpha = self.total_return - self.benchmark_return
            print(f"  Benchmark (SPY B&H) Return: {self.benchmark_return:.2%}")
            print(f"  Alpha vs Benchmark: {alpha:+.2%}")
        if self.vs_dual_momentum_only is not None:
            print(f"  vs Dual Momentum Only: {self.vs_dual_momentum_only:+.2%}")
        if self.vs_mean_reversion_only is not None:
            print(f"  vs Mean Reversion Only: {self.vs_mean_reversion_only:+.2%}")
        if self.vs_multi_timeframe_only is not None:
            print(f"  vs Multi-Timeframe Only: {self.vs_multi_timeframe_only:+.2%}")
        print("=" * 70)


class AgentBacktestEngine:
    """Backtest the AI agent's decision-making over historical data.

    Simulates:
    - All 3 strategies generating signals
    - Agent's weighted voting to select action
    - Decision classification (ROUTINE/NON_ROUTINE/URGENT)
    - Trade execution (assumes all decisions approved)

    Attributes:
        initial_capital: Starting capital for the backtest.
        transaction_cost_pct: Transaction cost as a percentage of trade value.
    """

    def __init__(
        self,
        initial_capital: float = 10000.0,
        transaction_cost_pct: float = 0.001,
    ) -> None:
        """Initialize the agent backtest engine.

        Args:
            initial_capital: Starting capital (default $10,000).
            transaction_cost_pct: Transaction cost percentage (default 0.1%).
        """
        self.initial_capital = initial_capital
        self.transaction_cost_pct = transaction_cost_pct

        # Initialize strategies
        self.dual_momentum = DualMomentumStrategy(
            assets=ASSET_REGISTRY,
            lookback_months=12,
            switch_threshold=0.10,
        )
        self.mean_reversion = MeanReversionStrategy(
            rsi_oversold=30,
            rsi_overbought=70,
            rsi_period=14,
        )
        self.multi_timeframe = MultiTimeframeTrendStrategy(
            lookback_months=[3, 6, 12],
            weights=[0.4, 0.35, 0.25],
            switch_threshold=0.05,
        )

        # Initialize orchestrator
        self.orchestrator = AgentOrchestrator()

    def _get_check_dates(
        self,
        start_date: date,
        end_date: date,
        frequency: str,
    ) -> list[date]:
        """Generate check dates based on frequency.

        Args:
            start_date: Start of the period.
            end_date: End of the period.
            frequency: Check frequency ('daily', 'weekly', 'monthly').

        Returns:
            List of dates to check for signals.
        """
        if frequency == "daily":
            dates = pd.bdate_range(start=start_date, end=end_date)
        elif frequency == "weekly":
            dates = pd.date_range(start=start_date, end=end_date, freq="W-FRI")
        elif frequency == "monthly":
            dates = pd.date_range(start=start_date, end=end_date, freq="ME")
        else:
            raise ValueError(f"Unknown frequency: {frequency}")

        return [d.date() for d in dates]

    def _get_price(
        self,
        prices: pd.DataFrame,
        symbol: str,
        target_date: date,
    ) -> float | None:
        """Get the price for a symbol on or before a date.

        Args:
            prices: DataFrame with date, close, symbol columns.
            symbol: The symbol to get price for.
            target_date: The date to get price for.

        Returns:
            The closing price, or None if not found.
        """
        symbol_prices = prices[prices["symbol"] == symbol].copy()
        if symbol_prices.empty:
            return None

        symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
        symbol_prices = symbol_prices[symbol_prices["date"] <= pd.Timestamp(target_date)]

        if symbol_prices.empty:
            return None

        return float(symbol_prices.iloc[-1]["close"])

    def _convert_dual_momentum_signal(
        self,
        signal: Signal,
        calc_date: date,
    ) -> dict[str, Any]:
        """Convert DualMomentumStrategy Signal to orchestrator format.

        Args:
            signal: The Signal from DualMomentumStrategy.
            calc_date: The date of the signal.

        Returns:
            Dictionary in orchestrator format.
        """
        return {
            "action": signal.action.value,
            "confidence": 0.7 if signal.action != SignalAction.HOLD else 0.5,
            "asset_symbol": signal.asset.symbol if signal.asset else None,
            "asset_class": signal.asset.asset_class.value if signal.asset else None,
            "reasoning": signal.reason,
        }

    def _convert_strategy_signal(
        self,
        signal: StrategySignal,
    ) -> dict[str, Any]:
        """Convert StrategySignal to orchestrator format.

        Args:
            signal: The StrategySignal from BaseStrategy subclasses.

        Returns:
            Dictionary in orchestrator format.
        """
        asset_symbol = None
        if signal.asset_class:
            try:
                asset = get_asset(signal.asset_class)
                asset_symbol = asset.symbol
            except KeyError:
                pass

        return {
            "action": signal.action.value,
            "confidence": signal.confidence,
            "asset_symbol": asset_symbol,
            "asset_class": signal.asset_class.value if signal.asset_class else None,
            "reasoning": signal.reasoning,
        }

    def _determine_market_regime(
        self,
        prices: pd.DataFrame,
        calc_date: date,
    ) -> str:
        """Determine the market regime based on price data.

        Args:
            prices: DataFrame with price data.
            calc_date: The date to analyze.

        Returns:
            A string describing the market regime.
        """
        # Simple regime detection based on SPY performance
        spy_prices = prices[prices["symbol"] == "SPY"].copy()
        if spy_prices.empty:
            return "unknown"

        spy_prices["date"] = pd.to_datetime(spy_prices["date"])
        spy_prices = spy_prices[spy_prices["date"] <= pd.Timestamp(calc_date)]
        spy_prices = spy_prices.sort_values("date")

        if len(spy_prices) < 50:
            return "insufficient_data"

        # Calculate 50-day return
        recent_50 = spy_prices.tail(50)
        start_price = float(recent_50.iloc[0]["close"])
        end_price = float(recent_50.iloc[-1]["close"])

        if start_price == 0:
            return "unknown"

        return_50d = (end_price / start_price) - 1

        # Calculate volatility (std of daily returns)
        daily_returns = recent_50["close"].pct_change().dropna()
        volatility = float(daily_returns.std())

        # Classify regime
        if return_50d > 0.10:
            regime = "strong_bull"
        elif return_50d > 0.02:
            regime = "bull"
        elif return_50d < -0.10:
            regime = "strong_bear"
        elif return_50d < -0.02:
            regime = "bear"
        else:
            regime = "sideways"

        if volatility > 0.02:
            regime = f"volatile_{regime}"

        return regime

    def _calculate_drawdown(
        self,
        prices: pd.DataFrame,
        calc_date: date,
    ) -> float:
        """Calculate current drawdown from peak for SPY.

        Args:
            prices: DataFrame with price data.
            calc_date: The date to analyze.

        Returns:
            Drawdown as decimal (0.10 = 10% drawdown).
        """
        spy_prices = prices[prices["symbol"] == "SPY"].copy()
        if spy_prices.empty:
            return 0.0

        spy_prices["date"] = pd.to_datetime(spy_prices["date"])
        spy_prices = spy_prices[spy_prices["date"] <= pd.Timestamp(calc_date)]
        spy_prices = spy_prices.sort_values("date")

        if len(spy_prices) < 252:
            return 0.0

        # Get last year of data
        recent = spy_prices.tail(252)
        peak = float(recent["close"].max())
        current = float(recent.iloc[-1]["close"])

        if peak == 0:
            return 0.0

        return (peak - current) / peak

    def run(
        self,
        prices: pd.DataFrame,
        start_date: date,
        end_date: date,
        check_frequency: str = "daily",
    ) -> AgentBacktestResult:
        """Run the agent backtest.

        Args:
            prices: Historical price data with columns: date, close, symbol.
            start_date: Start date for the backtest.
            end_date: End date for the backtest.
            check_frequency: How often to check for signals ('daily', 'weekly', 'monthly').

        Returns:
            AgentBacktestResult with all metrics and decision records.
        """
        logger.info(
            "starting_agent_backtest",
            start=str(start_date),
            end=str(end_date),
            frequency=check_frequency,
        )

        # Generate check dates
        check_dates = self._get_check_dates(start_date, end_date, check_frequency)
        logger.info("check_dates_generated", count=len(check_dates))

        # Initialize portfolio state
        cash = Decimal(str(self.initial_capital))
        current_holding: AssetClass | None = None
        current_shares = Decimal("0")
        last_action: SignalAction | None = None

        decisions: list[AgentDecisionRecord] = []
        trades: list[Trade] = []
        equity_curve_data: list[dict[str, Any]] = []

        for check_date in check_dates:
            # Get signals from all 3 strategies
            dm_signal = self.dual_momentum.generate_signal(
                prices=prices,
                calc_date=check_date,
                current_holding=current_holding,
            )

            mr_signal = self.mean_reversion.generate_signal(
                prices=prices,
                calc_date=check_date,
                current_holding=current_holding,
            )

            mtf_signal = self.multi_timeframe.generate_signal(
                prices=prices,
                calc_date=check_date,
                current_holding=current_holding,
            )

            # Convert signals to orchestrator format
            signals = {
                "dual_momentum": self._convert_dual_momentum_signal(dm_signal, check_date),
                "mean_reversion": self._convert_strategy_signal(mr_signal),
                "multi_timeframe": self._convert_strategy_signal(mtf_signal),
            }

            # Get market context
            drawdown = self._calculate_drawdown(prices, check_date)
            market_regime = self._determine_market_regime(prices, check_date)
            market_context = {
                "drawdown": drawdown,
                "volatility": "extreme" if "volatile" in market_regime else "normal",
            }

            # Use orchestrator to analyze and decide
            decision = self.orchestrator.analyze(signals, market_context)

            # Only act on signal changes (not continuous HOLD)
            action_changed = decision.action != last_action
            should_execute = (
                decision.action != SignalAction.HOLD
                and action_changed
            )

            # Record the decision
            decision_record = AgentDecisionRecord(
                date=check_date,
                decision_type=decision.decision_type,
                action=decision.action,
                asset_class=self._get_target_asset_class(decision, signals),
                confidence=decision.confidence,
                reasoning=decision.reasoning,
                strategy_signals=signals,
                market_regime=market_regime,
                executed=should_execute,
            )
            decisions.append(decision_record)

            if should_execute:
                logger.info(
                    "agent_decision",
                    date=str(check_date),
                    decision_type=decision.decision_type.value,
                    action=decision.action.value,
                    confidence=decision.confidence,
                    executed=should_execute,
                )

                # Execute trade
                if decision.action == SignalAction.BUY:
                    target_asset_class = self._get_target_asset_class(decision, signals)

                    # Sell current holding if any
                    if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
                        sell_asset = get_asset(current_holding)
                        sell_symbol = sell_asset.yahoo_symbol or sell_asset.symbol
                        sell_price = self._get_price(prices, sell_symbol, check_date)

                        if sell_price:
                            sell_value = float(current_shares) * sell_price
                            commission = sell_value * self.transaction_cost_pct

                            trades.append(
                                Trade(
                                    date=check_date,
                                    asset=sell_asset,
                                    action=SignalAction.SELL,
                                    shares=current_shares,
                                    price=sell_price,
                                    commission=commission,
                                )
                            )

                            cash += Decimal(str(sell_value - commission))
                            current_shares = Decimal("0")

                    # Buy new asset
                    if target_asset_class and target_asset_class != AssetClass.CASH:
                        buy_asset = get_asset(target_asset_class)
                        buy_symbol = buy_asset.yahoo_symbol or buy_asset.symbol
                        buy_price = self._get_price(prices, buy_symbol, check_date)

                        if buy_price and buy_price > 0:
                            buy_value = float(cash)
                            commission = buy_value * self.transaction_cost_pct
                            net_value = buy_value - commission
                            shares_to_buy = Decimal(str(net_value / buy_price))

                            trades.append(
                                Trade(
                                    date=check_date,
                                    asset=buy_asset,
                                    action=SignalAction.BUY,
                                    shares=shares_to_buy,
                                    price=buy_price,
                                    commission=commission,
                                )
                            )

                            cash = Decimal("0")
                            current_shares = shares_to_buy
                            current_holding = target_asset_class
                    else:
                        # Moving to cash
                        current_holding = AssetClass.CASH
                        current_shares = Decimal("0")

                elif decision.action == SignalAction.SELL:
                    # Sell all and move to cash
                    if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
                        sell_asset = get_asset(current_holding)
                        sell_symbol = sell_asset.yahoo_symbol or sell_asset.symbol
                        sell_price = self._get_price(prices, sell_symbol, check_date)

                        if sell_price:
                            sell_value = float(current_shares) * sell_price
                            commission = sell_value * self.transaction_cost_pct

                            trades.append(
                                Trade(
                                    date=check_date,
                                    asset=sell_asset,
                                    action=SignalAction.SELL,
                                    shares=current_shares,
                                    price=sell_price,
                                    commission=commission,
                                )
                            )

                            cash += Decimal(str(sell_value - commission))
                            current_shares = Decimal("0")
                            current_holding = AssetClass.CASH

                last_action = decision.action

            # Record equity curve point
            current_value = float(cash)
            if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
                holding_asset = get_asset(current_holding)
                holding_symbol = holding_asset.yahoo_symbol or holding_asset.symbol
                holding_price = self._get_price(prices, holding_symbol, check_date)
                if holding_price:
                    current_value += float(current_shares) * holding_price

            equity_curve_data.append({"date": check_date, "value": current_value})

        # Create equity curve DataFrame
        equity_curve = pd.DataFrame(equity_curve_data)

        # Calculate final value
        final_value = equity_curve["value"].iloc[-1] if not equity_curve.empty else self.initial_capital

        # Calculate benchmark return (SPY buy and hold)
        benchmark_return = self._calculate_benchmark_return(prices, start_date, end_date)

        # Create result
        result = AgentBacktestResult(
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_value=final_value,
            total_return=0.0,  # Will be calculated
            decisions=decisions,
            trades=trades,
            equity_curve=equity_curve,
            benchmark_return=benchmark_return,
        )

        result.calculate_metrics()

        # Calculate alpha vs individual strategies
        result.vs_dual_momentum_only = self._calculate_strategy_alpha(
            prices, start_date, end_date, "dual_momentum", result.total_return
        )
        result.vs_mean_reversion_only = self._calculate_strategy_alpha(
            prices, start_date, end_date, "mean_reversion", result.total_return
        )
        result.vs_multi_timeframe_only = self._calculate_strategy_alpha(
            prices, start_date, end_date, "multi_timeframe", result.total_return
        )

        return result

    def _get_target_asset_class(
        self,
        decision: Any,
        signals: dict[str, dict[str, Any]],
    ) -> AssetClass | None:
        """Get the target asset class from the decision or signals.

        Args:
            decision: The AgentDecision from the orchestrator.
            signals: The strategy signals.

        Returns:
            The target AssetClass, or None.
        """
        # First check the decision
        if decision.asset_symbol:
            for asset_class, asset in ASSET_REGISTRY.items():
                if asset.symbol == decision.asset_symbol:
                    return asset_class

        # Fall back to the winning strategy's recommendation
        best_confidence = 0.0
        best_asset_class = None

        for strategy_name, signal in signals.items():
            if signal.get("action") == decision.action.value:
                confidence = signal.get("confidence", 0.5)
                if confidence > best_confidence:
                    best_confidence = confidence
                    asset_class_str = signal.get("asset_class")
                    if asset_class_str:
                        try:
                            best_asset_class = AssetClass(asset_class_str)
                        except ValueError:
                            pass

        return best_asset_class

    def _calculate_benchmark_return(
        self,
        prices: pd.DataFrame,
        start_date: date,
        end_date: date,
    ) -> float | None:
        """Calculate SPY buy-and-hold return for the period.

        Args:
            prices: Price DataFrame.
            start_date: Start of period.
            end_date: End of period.

        Returns:
            Total return as decimal, or None if insufficient data.
        """
        start_price = self._get_price(prices, "SPY", start_date)
        end_price = self._get_price(prices, "SPY", end_date)

        if start_price is None or end_price is None or start_price == 0:
            return None

        return (end_price / start_price) - 1

    def _calculate_strategy_alpha(
        self,
        prices: pd.DataFrame,
        start_date: date,
        end_date: date,
        strategy_name: str,
        agent_return: float,
    ) -> float | None:
        """Calculate alpha of agent vs a single strategy.

        This is a simplified calculation - actual implementation would
        run a full backtest of the individual strategy.

        Args:
            prices: Price DataFrame.
            start_date: Start of period.
            end_date: End of period.
            strategy_name: Name of the strategy to compare against.
            agent_return: The agent's total return.

        Returns:
            Alpha (agent return - strategy return), or None.
        """
        # For now, return a placeholder
        # In a full implementation, you would run each strategy's backtest
        # and compare the returns
        return None
