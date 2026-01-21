"""Backtesting engine for momentum strategies."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pandas as pd
import numpy as np
import structlog

from aurel2.core.models import Asset, AssetClass, Signal, SignalAction, Trade, PortfolioSnapshot
from aurel2.strategies.dual_momentum import DualMomentumStrategy

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

        # Sharpe ratio (annualized, assuming 0% risk-free for simplicity)
        if len(values) > 1:
            returns = pd.Series(values).pct_change().dropna()
            if len(returns) > 0 and returns.std() > 0:
                # Annualize based on rebalance frequency (assume quarterly = 4 per year)
                periods_per_year = 4  # quarterly
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
        print("=" * 60)


class BacktestEngine:
    """Engine for running strategy backtests."""

    def __init__(
        self,
        strategy: DualMomentumStrategy,
        initial_capital: float = 10000.0,
        transaction_cost_pct: float = 0.001,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.transaction_cost_pct = transaction_cost_pct

    def run(
        self,
        prices: pd.DataFrame,
        start_date: date,
        end_date: date,
        frequency: str = "quarterly",
        benchmark_symbol: str | None = None,
    ) -> BacktestResult:
        """
        Run backtest simulation.

        Args:
            prices: Historical price data (date, close, symbol)
            start_date: Backtest start date
            end_date: Backtest end date
            frequency: Rebalance frequency ('monthly' or 'quarterly')
            benchmark_symbol: Optional benchmark to compare against

        Returns:
            BacktestResult with all metrics
        """
        logger.info("starting_backtest", start=str(start_date), end=str(end_date))

        # Generate rebalance dates
        rebalance_dates = self.strategy.get_rebalance_dates(start_date, end_date, frequency)
        logger.info("rebalance_dates", count=len(rebalance_dates))

        # Initialize portfolio
        cash = Decimal(str(self.initial_capital))
        current_holding: AssetClass | None = None
        current_shares = Decimal("0")

        trades: list[Trade] = []
        signals: list[Signal] = []
        snapshots: list[PortfolioSnapshot] = []

        for rebal_date in rebalance_dates:
            # Generate signal
            signal = self.strategy.generate_signal(
                prices=prices,
                calc_date=rebal_date,
                current_holding=current_holding,
            )
            signals.append(signal)

            logger.info(
                "signal_generated",
                date=str(rebal_date),
                action=signal.action.value,
                asset=signal.asset.symbol if signal.asset else "None",
                reason=signal.reason,
            )

            # Get current price for the signal asset
            if signal.asset and signal.asset.yahoo_symbol:
                symbol = signal.asset.yahoo_symbol
                symbol_prices = prices[prices["symbol"] == symbol].copy()
                symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
                current_price_row = symbol_prices[symbol_prices["date"] <= pd.Timestamp(rebal_date)]

                if not current_price_row.empty:
                    current_price = float(current_price_row.iloc[-1]["close"])
                else:
                    current_price = None
            else:
                current_price = 1.0  # Cash

            # Execute trades based on signal
            if signal.action == SignalAction.BUY:
                # Sell current holding if any
                if current_holding and current_shares > 0:
                    # Get sell price
                    old_asset = self.strategy.assets[current_holding]
                    old_symbol = old_asset.yahoo_symbol or old_asset.symbol
                    old_prices = prices[prices["symbol"] == old_symbol].copy()
                    old_prices["date"] = pd.to_datetime(old_prices["date"])
                    old_price_row = old_prices[old_prices["date"] <= pd.Timestamp(rebal_date)]

                    if not old_price_row.empty:
                        sell_price = float(old_price_row.iloc[-1]["close"])
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

                        logger.info(
                            "trade_executed",
                            action="SELL",
                            asset=old_symbol,
                            shares=float(current_shares),
                            price=sell_price,
                            value=sell_value,
                        )

                # Buy new asset
                if signal.asset and current_price:
                    # Determine new asset class
                    new_asset_class = None
                    for ac, asset in self.strategy.assets.items():
                        if asset.symbol == signal.asset.symbol:
                            new_asset_class = ac
                            break

                    if new_asset_class == AssetClass.CASH:
                        # Just hold cash
                        current_holding = AssetClass.CASH
                        current_shares = Decimal("0")
                    else:
                        buy_value = float(cash)
                        commission = buy_value * self.transaction_cost_pct
                        net_value = buy_value - commission
                        shares_to_buy = Decimal(str(net_value / current_price))

                        trades.append(Trade(
                            date=rebal_date,
                            asset=signal.asset,
                            action=SignalAction.BUY,
                            shares=shares_to_buy,
                            price=current_price,
                            commission=commission,
                        ))

                        cash = Decimal("0")
                        current_shares = shares_to_buy
                        current_holding = new_asset_class

                        logger.info(
                            "trade_executed",
                            action="BUY",
                            asset=signal.asset.symbol,
                            shares=float(shares_to_buy),
                            price=current_price,
                            value=net_value,
                        )

            # Record snapshot
            if current_holding and current_holding != AssetClass.CASH and current_shares > 0 and current_price:
                total_value = float(current_shares) * current_price + float(cash)
            else:
                total_value = float(cash)

            snapshots.append(PortfolioSnapshot(
                date=rebal_date,
                cash=cash,
                positions=[],
                total_value=Decimal(str(total_value)),
            ))

        # Calculate final value at end date
        final_value = float(snapshots[-1].total_value) if snapshots else self.initial_capital

        # Calculate benchmark if provided
        benchmark_final = None
        if benchmark_symbol:
            bench_prices = prices[prices["symbol"] == benchmark_symbol].copy()
            if not bench_prices.empty:
                bench_prices["date"] = pd.to_datetime(bench_prices["date"])

                # Get price at start
                start_row = bench_prices[bench_prices["date"] >= pd.Timestamp(start_date)]
                if not start_row.empty:
                    start_price = float(start_row.iloc[0]["close"])

                    # Get price at end
                    end_row = bench_prices[bench_prices["date"] <= pd.Timestamp(end_date)]
                    if not end_row.empty:
                        end_price = float(end_row.iloc[-1]["close"])
                        benchmark_return = end_price / start_price
                        benchmark_final = self.initial_capital * benchmark_return

        result = BacktestResult(
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_value=final_value,
            trades=trades,
            signals=signals,
            snapshots=snapshots,
            benchmark_final=benchmark_final,
        )
        result.calculate_metrics()

        return result
