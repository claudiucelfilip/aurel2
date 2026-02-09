"""Backtesting engine that mirrors the full live trading path.

Runs the same pipeline as production:
  1. All 3 strategies (dual momentum, mean reversion, multi-timeframe)
  2. Orchestrator (weighted voting, regime detection, position sizing)
  3. AI Advisor (failure learning review, potential override)
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import numpy as np
import structlog

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import Asset, AssetClass, Signal, SignalAction, Trade, PortfolioSnapshot
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy
from aurel2.agent.orchestrator import AgentOrchestrator, AgentDecision, DecisionType

logger = structlog.get_logger()


@dataclass
class BacktestResult:
    """Results of a backtest run."""
    start_date: date
    end_date: date
    initial_capital: float
    final_value: float
    trades: list[Trade]
    signals: list[Signal]
    snapshots: list[PortfolioSnapshot]
    benchmark_final: float | None = None

    # AI override tracking
    ai_overrides: list[dict] = field(default_factory=list)
    ai_override_count: int = 0
    ai_override_win_rate: float = 0.0

    # Calculated metrics
    total_return: float = 0.0
    cagr: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    num_trades: int = 0
    win_rate: float = 0.0

    def calculate_metrics(self):
        """Calculate performance metrics from snapshots."""
        if not self.snapshots:
            return

        values = [float(s.total_value) for s in self.snapshots]
        dates = [s.date for s in self.snapshots]

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
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
        self.max_drawdown = max_dd

        # Sharpe ratio (annualized, assuming 0% risk-free)
        if len(values) > 1:
            returns = pd.Series(values).pct_change().dropna()
            if len(returns) > 0 and returns.std() > 0:
                periods_per_year = 12  # monthly rebalance
                self.sharpe_ratio = (returns.mean() * periods_per_year) / (returns.std() * np.sqrt(periods_per_year))

        # Trade stats
        self.num_trades = len([t for t in self.trades if t.action == SignalAction.BUY])

    def print_summary(self):
        """Print a summary of backtest results."""
        print("\n" + "=" * 60)
        print("BACKTEST RESULTS")
        print("=" * 60)
        print(f"Period: {self.start_date} to {self.end_date}")
        print(f"Initial Capital: ${self.initial_capital:,.2f}")
        print(f"Final Value: ${self.final_value:,.2f}")
        print("-" * 60)
        print(f"Total Return: {self.total_return:.2%}")
        print(f"CAGR: {self.cagr:.2%}")
        print(f"Max Drawdown: {self.max_drawdown:.2%}")
        print(f"Sharpe Ratio: {self.sharpe_ratio:.2f}")
        print(f"Number of Trades: {self.num_trades}")
        if self.benchmark_final:
            bench_return = (self.benchmark_final / self.initial_capital) - 1
            print("-" * 60)
            print(f"Benchmark Final: ${self.benchmark_final:,.2f}")
            print(f"Benchmark Return: {bench_return:.2%}")
            print(f"Alpha: {self.total_return - bench_return:.2%}")
        if self.ai_overrides:
            print("-" * 60)
            print(f"AI OVERRIDE ANALYSIS ({self.ai_override_count} overrides)")
            print("-" * 60)
            safety = [o for o in self.ai_overrides if o["override_type"] == "to_safety"]
            opportunity = [o for o in self.ai_overrides if o["override_type"] == "to_opportunity"]
            print(f"  To safety: {len(safety)}  |  To opportunity: {len(opportunity)}")
            print(f"  Overall win rate: {self.ai_override_win_rate:.0%}")
            print()
            for o in self.ai_overrides:
                det_ret = f"{o['det_1m_return']:+.1%}" if o.get("det_1m_return") is not None else "N/A"
                ai_ret = f"{o['ai_1m_return']:+.1%}" if o.get("ai_1m_return") is not None else "N/A"
                winner = "AI" if o.get("ai_won") else ("DET" if o.get("ai_won") is False else "?")
                print(f"  {o['date']}  {o['override_type']:<15}  "
                      f"DET: {o['det_action']} {o['det_asset'] or 'CASH':<5} ({det_ret})  →  "
                      f"AI: {o['ai_action']} {o['ai_asset'] or 'CASH':<5} ({ai_ret})  "
                      f"Winner: {winner}")
        print("=" * 60)


class BacktestEngine:
    """Engine that mirrors the full live trading path for backtesting.

    Creates all components internally (3 strategies, orchestrator, AI advisor)
    and runs the same decision pipeline as production on each rebalance date.
    """

    def __init__(
        self,
        initial_capital: float = 10000.0,
        transaction_cost_pct: float = 0.001,
        use_ai: bool = False,
        ai_model: str = "haiku",
        amnesia: bool = False,
    ):
        self.dual_momentum = DualMomentumStrategy(assets=ASSET_REGISTRY)
        self.mean_reversion = MeanReversionStrategy()
        self.multi_timeframe = MultiTimeframeTrendStrategy()
        self.orchestrator = AgentOrchestrator()

        self.ai_advisor = None
        if use_ai:
            # Deferred import to avoid circular: backtest -> advisor -> failure_analyzer -> backtest
            from aurel2.agent.advisor import AIAdvisor
            self.ai_advisor = AIAdvisor(model=ai_model, amnesia=amnesia)
        self.initial_capital = initial_capital
        self.transaction_cost_pct = transaction_cost_pct

        # Tradeable symbols for AI override validation
        self._tradeable_symbols = {
            a.symbol for a in ASSET_REGISTRY.values() if a.symbol != "CASH"
        }

    def _normalize_signal(self, signal) -> dict:
        """Normalize strategy signals to a common dict format.

        Mirrors checker._run_strategies signal normalization (lines 400-433).
        Handles both Signal (dual momentum) and StrategySignal (other two).
        """
        action = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
        confidence = getattr(signal, 'confidence', 0.8)

        asset_symbol = None
        if hasattr(signal, 'asset') and signal.asset:
            asset_symbol = signal.asset.symbol
        elif hasattr(signal, 'asset_class') and signal.asset_class:
            if signal.asset_class in ASSET_REGISTRY:
                asset_symbol = ASSET_REGISTRY[signal.asset_class].symbol

        reasoning = getattr(signal, 'reasoning', None) or getattr(signal, 'reason', '')

        is_pilot = False
        if hasattr(signal, 'reason') and signal.reason and 'PILOT' in signal.reason:
            is_pilot = True

        # Include per-asset momentum scores for orchestrator calm-hold escape hatch
        mom_dict = {}
        if hasattr(signal, 'momentum_scores') and signal.momentum_scores:
            for ac, ms in signal.momentum_scores.items():
                mom_dict[ms.asset.symbol] = ms.momentum_12m

        return {
            "action": action,
            "confidence": confidence,
            "asset_symbol": asset_symbol,
            "reasoning": reasoning,
            "pilot_position": is_pilot,
            "momentum_scores": mom_dict,
        }

    def _build_market_context(self, prices: pd.DataFrame, calc_date: date) -> dict:
        """Build market context from price data.

        Mirrors checker._build_market_context (lines 474-507).
        Uses only data available up to calc_date for point-in-time correctness.
        """
        context = {}

        try:
            spy_prices = prices[prices["symbol"] == "SPY"].copy()
            if not spy_prices.empty:
                spy_prices["date"] = pd.to_datetime(spy_prices["date"])
                # Only use data up to calc_date
                spy_prices = spy_prices[spy_prices["date"] <= pd.Timestamp(calc_date)]
                if spy_prices.empty:
                    return context

                spy_prices = spy_prices.sort_values("date")
                current_price = float(spy_prices.iloc[-1]["close"])
                context["spy_price"] = current_price

                if len(spy_prices) >= 200:
                    ma_200 = spy_prices.tail(200)["close"].mean()
                    context["ma_200"] = float(ma_200)

                year_high = float(spy_prices.tail(252)["close"].max())
                drawdown = (current_price / year_high) - 1
                context["drawdown"] = abs(drawdown)

                if context["drawdown"] < 0.05:
                    context["regime"] = "bull"
                elif context["drawdown"] < 0.15:
                    context["regime"] = "sideways"
                else:
                    context["regime"] = "bear"

        except Exception as e:
            logger.warning("backtest_market_context_error", error=str(e))

        return context

    def _get_price(self, prices: pd.DataFrame, symbol: str, as_of: date) -> float | None:
        """Get the price of a symbol as of a date."""
        symbol_prices = prices[prices["symbol"] == symbol].copy()
        if symbol_prices.empty:
            return None
        symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
        rows = symbol_prices[symbol_prices["date"] <= pd.Timestamp(as_of)]
        if rows.empty:
            return None
        return float(rows.iloc[-1]["close"])

    def _symbol_to_asset_class(self, symbol: str) -> AssetClass | None:
        """Look up the AssetClass for a given symbol."""
        for ac, asset in ASSET_REGISTRY.items():
            if asset.symbol == symbol or asset.yahoo_symbol == symbol:
                return ac
        return None

    def run(
        self,
        prices: pd.DataFrame,
        start_date: date,
        end_date: date,
        frequency: str = "monthly",
        benchmark_symbol: str | None = None,
    ) -> BacktestResult:
        """Run backtest simulation using the full live trading path.

        Args:
            prices: Historical price data (date, close, symbol)
            start_date: Backtest start date
            end_date: Backtest end date
            frequency: Rebalance frequency ('monthly' or 'quarterly')
            benchmark_symbol: Optional benchmark to compare against

        Returns:
            BacktestResult with all metrics
        """
        logger.info("starting_backtest", start=str(start_date), end=str(end_date),
                     mode="full_live_path", use_ai=self.ai_advisor is not None)

        # Generate rebalance dates using dual momentum's schedule
        rebalance_dates = self.dual_momentum.get_rebalance_dates(start_date, end_date, frequency)
        logger.info("rebalance_dates", count=len(rebalance_dates))

        # Initialize portfolio
        cash = Decimal(str(self.initial_capital))
        current_holding: AssetClass | None = None
        current_holding_symbol: str | None = None
        current_shares = Decimal("0")

        trades: list[Trade] = []
        all_signals: list[Signal] = []
        snapshots: list[PortfolioSnapshot] = []
        ai_overrides: list[dict] = []

        total_dates = len(rebalance_dates)
        for i, rebal_date in enumerate(rebalance_dates, 1):
            print(f"\r  [{i}/{total_dates}] {rebal_date}", end="", flush=True)

            # ================================================================
            # Step 1: Run all 3 strategies (mirrors checker._run_strategies)
            # ================================================================
            signals = {}
            for name, strategy in [
                ("dual_momentum", self.dual_momentum),
                ("mean_reversion", self.mean_reversion),
                ("multi_timeframe", self.multi_timeframe),
            ]:
                try:
                    signal = strategy.generate_signal(
                        prices=prices,
                        calc_date=rebal_date,
                        current_holding=current_holding,
                    )
                    signals[name] = self._normalize_signal(signal)

                    # Keep raw dual momentum signal for the signals log
                    if name == "dual_momentum" and hasattr(signal, 'momentum_scores'):
                        all_signals.append(signal)

                except Exception as e:
                    logger.error("backtest_strategy_error", strategy=name,
                                 date=str(rebal_date), error=str(e))
                    signals[name] = {
                        "action": "hold",
                        "confidence": 0.0,
                        "error": str(e),
                    }

            logger.info(
                "backtest_signals",
                date=str(rebal_date),
                dm=signals.get("dual_momentum", {}).get("action"),
                mr=signals.get("mean_reversion", {}).get("action"),
                mtf=signals.get("multi_timeframe", {}).get("action"),
            )

            # ================================================================
            # Step 2: Market context (mirrors checker._build_market_context)
            # ================================================================
            market_context = self._build_market_context(prices, rebal_date)

            # ================================================================
            # Step 3: Orchestrator analysis (mirrors checker step 6)
            # ================================================================
            decision = self.orchestrator.analyze(
                signals=signals,
                market_context=market_context,
                current_holding=current_holding_symbol,
            )

            logger.info(
                "backtest_decision",
                date=str(rebal_date),
                type=decision.decision_type.value,
                action=decision.action.value,
                asset=decision.asset_symbol,
                confidence=f"{decision.confidence:.2f}",
                position_size=f"{decision.position_size_pct:.0%}",
                regime=decision.regime.value if decision.regime else None,
            )

            # ================================================================
            # Step 4: AI Advisor review (mirrors checker step 7)
            # Skip AI on HOLD — it churns the portfolio by overriding holds
            # ================================================================
            if self.ai_advisor and decision.action != SignalAction.HOLD:
                try:
                    ai_advice = self.ai_advisor.review(
                        deterministic_action=decision.action.value,
                        deterministic_asset=decision.asset_symbol,
                        strategy_signals=signals,
                        market_context=market_context,
                        prices=prices,
                        current_holding=current_holding_symbol,
                        target_date=rebal_date,
                    )

                    logger.info(
                        "backtest_ai_response",
                        date=str(rebal_date),
                        agrees=ai_advice.agrees_with_deterministic,
                        ai_action=ai_advice.recommended_action,
                        ai_asset=ai_advice.recommended_asset,
                        confidence=f"{ai_advice.confidence:.2f}",
                        det_action=decision.action.value,
                        det_asset=decision.asset_symbol,
                    )

                    # Override logic (mirrors checker lines 225-249)
                    ai_asset_tradeable = (
                        ai_advice.recommended_asset is None
                        or ai_advice.recommended_asset in self._tradeable_symbols
                    )
                    SAFETY_ASSETS = {"AGG", "TLT", "GLD", "CASH"}
                    ai_asset = ai_advice.recommended_asset
                    override_type = "to_safety" if ai_asset in SAFETY_ASSETS else "to_opportunity"
                    OVERRIDE_THRESHOLDS = {"to_safety": 0.65, "to_opportunity": 0.85}
                    threshold = OVERRIDE_THRESHOLDS[override_type]
                    if (not ai_advice.agrees_with_deterministic
                            and ai_advice.confidence > threshold
                            and ai_asset_tradeable):
                        logger.info(
                            "backtest_ai_override",
                            date=str(rebal_date),
                            old_action=decision.action.value,
                            old_asset=decision.asset_symbol,
                            new_action=ai_advice.recommended_action,
                            new_asset=ai_advice.recommended_asset,
                        )
                        ai_overrides.append({
                            "date": rebal_date,
                            "det_action": decision.action.value,
                            "det_asset": decision.asset_symbol,
                            "ai_action": ai_advice.recommended_action,
                            "ai_asset": ai_advice.recommended_asset,
                            "override_type": override_type,
                            "confidence": ai_advice.confidence,
                        })
                        decision = AgentDecision(
                            decision_type=DecisionType.NON_ROUTINE,
                            action=SignalAction(ai_advice.recommended_action),
                            asset_symbol=ai_advice.recommended_asset,
                            reasoning=f"AI Override: {ai_advice.reasoning}",
                            confidence=ai_advice.confidence,
                            strategy_signals=signals,
                            requires_approval=True,
                            timeout_hours=1.0,
                            urgency=decision.urgency,
                            market_context=market_context,
                            position_size_pct=decision.position_size_pct,
                            regime=decision.regime,
                        )

                except Exception as e:
                    logger.error("backtest_ai_error", date=str(rebal_date), error=str(e))

            # ================================================================
            # Step 5: Execute trade using decision
            # ================================================================
            action = decision.action
            target_symbol = decision.asset_symbol
            position_size_pct = decision.position_size_pct

            if action == SignalAction.BUY and target_symbol:
                target_asset_class = self._symbol_to_asset_class(target_symbol)
                target_asset = ASSET_REGISTRY.get(target_asset_class) if target_asset_class else None

                if target_asset_class == AssetClass.CASH:
                    # Move to cash
                    if current_holding and current_shares > 0:
                        sell_price = self._get_price(
                            prices,
                            ASSET_REGISTRY[current_holding].yahoo_symbol or ASSET_REGISTRY[current_holding].symbol,
                            rebal_date,
                        )
                        if sell_price:
                            sell_value = float(current_shares) * sell_price
                            commission = sell_value * self.transaction_cost_pct
                            trades.append(Trade(
                                date=rebal_date,
                                asset=ASSET_REGISTRY[current_holding],
                                action=SignalAction.SELL,
                                shares=current_shares,
                                price=sell_price,
                                commission=commission,
                            ))
                            cash += Decimal(str(sell_value - commission))
                            current_shares = Decimal("0")

                    current_holding = AssetClass.CASH
                    current_holding_symbol = "CASH"

                elif target_asset and target_asset.yahoo_symbol:
                    buy_price = self._get_price(prices, target_asset.yahoo_symbol, rebal_date)
                    if buy_price:
                        # Sell current holding first
                        if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
                            old_asset = ASSET_REGISTRY[current_holding]
                            sell_price = self._get_price(
                                prices,
                                old_asset.yahoo_symbol or old_asset.symbol,
                                rebal_date,
                            )
                            if sell_price:
                                sell_value = float(current_shares) * sell_price
                                commission = sell_value * self.transaction_cost_pct
                                trades.append(Trade(
                                    date=rebal_date,
                                    asset=old_asset,
                                    action=SignalAction.SELL,
                                    shares=current_shares,
                                    price=sell_price,
                                    commission=commission,
                                ))
                                cash += Decimal(str(sell_value - commission))
                                current_shares = Decimal("0")

                        # Buy new asset with position sizing from orchestrator
                        buy_value = float(cash) * position_size_pct
                        remaining_cash = float(cash) - buy_value
                        commission = buy_value * self.transaction_cost_pct
                        net_value = buy_value - commission
                        shares_to_buy = Decimal(str(net_value / buy_price))

                        trades.append(Trade(
                            date=rebal_date,
                            asset=target_asset,
                            action=SignalAction.BUY,
                            shares=shares_to_buy,
                            price=buy_price,
                            commission=commission,
                        ))

                        cash = Decimal(str(remaining_cash))
                        current_shares = shares_to_buy
                        current_holding = target_asset_class
                        current_holding_symbol = target_symbol

                        logger.info(
                            "backtest_trade",
                            date=str(rebal_date),
                            action="BUY",
                            asset=target_symbol,
                            shares=float(shares_to_buy),
                            price=buy_price,
                            position_size=f"{position_size_pct:.0%}",
                        )

            elif action == SignalAction.SELL:
                # Sell current holding, go to cash
                if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
                    old_asset = ASSET_REGISTRY[current_holding]
                    sell_price = self._get_price(
                        prices,
                        old_asset.yahoo_symbol or old_asset.symbol,
                        rebal_date,
                    )
                    if sell_price:
                        sell_value = float(current_shares) * sell_price
                        commission = sell_value * self.transaction_cost_pct
                        trades.append(Trade(
                            date=rebal_date,
                            asset=old_asset,
                            action=SignalAction.SELL,
                            shares=current_shares,
                            price=sell_price,
                            commission=commission,
                        ))
                        cash += Decimal(str(sell_value - commission))
                        current_shares = Decimal("0")
                        current_holding = AssetClass.CASH
                        current_holding_symbol = "CASH"

                        logger.info(
                            "backtest_trade",
                            date=str(rebal_date),
                            action="SELL",
                            asset=old_asset.symbol,
                            shares=float(current_shares),
                            price=sell_price,
                        )

            # Record snapshot
            current_price = None
            if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
                held_asset = ASSET_REGISTRY.get(current_holding)
                if held_asset and held_asset.yahoo_symbol:
                    current_price = self._get_price(prices, held_asset.yahoo_symbol, rebal_date)

            if current_price and current_shares > 0:
                total_value = float(current_shares) * current_price + float(cash)
            else:
                total_value = float(cash)

            snapshots.append(PortfolioSnapshot(
                date=rebal_date,
                cash=cash,
                positions=[],
                total_value=Decimal(str(total_value)),
            ))

        print()  # newline after progress

        # Calculate final value at end date
        final_value = float(snapshots[-1].total_value) if snapshots else self.initial_capital

        # Calculate benchmark if provided
        benchmark_final = None
        if benchmark_symbol:
            bench_prices = prices[prices["symbol"] == benchmark_symbol].copy()
            if not bench_prices.empty:
                bench_prices["date"] = pd.to_datetime(bench_prices["date"])

                start_row = bench_prices[bench_prices["date"] >= pd.Timestamp(start_date)]
                if not start_row.empty:
                    start_price = float(start_row.iloc[0]["close"])

                    end_row = bench_prices[bench_prices["date"] <= pd.Timestamp(end_date)]
                    if not end_row.empty:
                        end_price = float(end_row.iloc[-1]["close"])
                        benchmark_return = end_price / start_price
                        benchmark_final = self.initial_capital * benchmark_return

        # Evaluate AI overrides: look forward 1 month for each override
        ai_override_wins = 0
        for override in ai_overrides:
            forward_date = override["date"] + timedelta(days=30)

            def _get_return(symbol: str) -> float | None:
                if not symbol or symbol == "CASH":
                    return 0.0
                asset_class = self._symbol_to_asset_class(symbol)
                asset = ASSET_REGISTRY.get(asset_class) if asset_class else None
                yahoo = (asset.yahoo_symbol or asset.symbol) if asset else symbol
                start_p = self._get_price(prices, yahoo, override["date"])
                end_p = self._get_price(prices, yahoo, forward_date)
                if start_p and end_p:
                    return (end_p / start_p) - 1
                return None

            det_return = _get_return(override["det_asset"])
            ai_return = _get_return(override["ai_asset"])
            override["det_1m_return"] = det_return
            override["ai_1m_return"] = ai_return
            if det_return is not None and ai_return is not None:
                override["ai_won"] = ai_return > det_return
                if override["ai_won"]:
                    ai_override_wins += 1
            else:
                override["ai_won"] = None

        scoreable = [o for o in ai_overrides if o.get("ai_won") is not None]
        override_win_rate = ai_override_wins / len(scoreable) if scoreable else 0.0

        result = BacktestResult(
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_value=final_value,
            trades=trades,
            signals=all_signals,
            snapshots=snapshots,
            benchmark_final=benchmark_final,
            ai_overrides=ai_overrides,
            ai_override_count=len(ai_overrides),
            ai_override_win_rate=override_win_rate,
        )
        result.calculate_metrics()

        return result


def generate_comparison_json(output_path: str = "data/backtest_comparison.json"):
    """Run 5y and 10y backtests and save results for the dashboard.

    Usage:
        python -m aurel2.engine.backtest
    """
    import json
    from pathlib import Path
    from aurel2.data.providers.yahoo import YahooFinanceProvider
    from aurel2.core.assets import get_all_yahoo_symbols

    end_date = date.today()
    capital = 10000
    provider = YahooFinanceProvider()

    # Fetch prices once (10y covers both periods)
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")
    extended_start = end_date - timedelta(days=10 * 365 + 600)

    print(f"Fetching prices for {len(symbols)} symbols...")
    prices = provider.get_multi_prices(symbols, extended_start, end_date + timedelta(days=5))
    if prices.empty:
        print("ERROR: No price data available")
        return

    print(f"Total: {len(prices)} price records\n")

    results = {}
    for label, years in [("10y", 10), ("5y", 5)]:
        start_date = end_date - timedelta(days=years * 365)
        print(f"Running {label} backtest ({start_date} -> {end_date})...")

        engine = BacktestEngine(initial_capital=capital, use_ai=False)
        result = engine.run(
            prices=prices,
            start_date=start_date,
            end_date=end_date,
            benchmark_symbol="SPY",
        )

        # Build portfolio series
        portfolio_data = [
            {"date": s.date.isoformat(), "value": round(float(s.total_value), 0)}
            for s in result.snapshots
        ]

        # Build benchmark series
        spy_df = prices[prices["symbol"] == "SPY"].copy()
        benchmark_data = []
        if not spy_df.empty:
            spy_df["date"] = pd.to_datetime(spy_df["date"])
            spy_df = spy_df.sort_values("date")
            start_rows = spy_df[spy_df["date"] >= pd.Timestamp(start_date)]
            if not start_rows.empty:
                spy_start_p = float(start_rows.iloc[0]["close"])
                for snap in result.snapshots:
                    row = spy_df[spy_df["date"] <= pd.Timestamp(snap.date)]
                    if not row.empty:
                        spy_p = float(row.iloc[-1]["close"])
                        benchmark_data.append({
                            "date": snap.date.isoformat(),
                            "value": round(capital * (spy_p / spy_start_p), 0),
                        })

        bench_return = ((result.benchmark_final / capital) - 1) * 100 if result.benchmark_final else 0
        bench_years = (end_date - start_date).days / 365.25
        bench_cagr = ((result.benchmark_final / capital) ** (1 / bench_years) - 1) * 100 if result.benchmark_final and bench_years > 0 else 0

        results[label] = {
            "metrics": {
                "total_return": round(result.total_return * 100, 1),
                "cagr": round(result.cagr * 100, 1),
                "max_drawdown": round(result.max_drawdown * 100, 1),
                "sharpe_ratio": round(result.sharpe_ratio, 2),
                "num_trades": result.num_trades,
                "benchmark_return": round(bench_return, 1),
                "benchmark_cagr": round(bench_cagr, 1),
                "alpha": round(result.total_return * 100 - bench_return, 1),
            },
            "portfolio": portfolio_data,
            "benchmark": benchmark_data,
        }

        result.print_summary()

    # Save
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nSaved to {out.resolve()}")


if __name__ == "__main__":
    generate_comparison_json()
