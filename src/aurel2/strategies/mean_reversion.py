"""Mean Reversion Strategy using RSI for oversold/overbought detection.

This strategy catches oversold bounces that momentum strategies typically miss.
It uses the Relative Strength Index (RSI) to detect when assets are oversold
(potential buy) or overbought (potential sell).
"""

from datetime import date

import pandas as pd
import structlog

from aurel2.core.models import AssetClass, SignalAction
from aurel2.data.indicators import calculate_rsi
from aurel2.strategies.base import BaseStrategy, StrategySignal

logger = structlog.get_logger()


class MeanReversionStrategy(BaseStrategy):
    """Mean reversion strategy based on RSI indicators.

    This strategy:
    - Signals BUY when RSI drops below the oversold threshold
    - Signals SELL when RSI rises above the overbought threshold AND has a position
    - Signals HOLD otherwise

    Attributes:
        name: Strategy identifier ("mean_reversion")
        rsi_oversold: RSI threshold for oversold conditions (default 30)
        rsi_overbought: RSI threshold for overbought conditions (default 70)
        rsi_period: Number of periods for RSI calculation (default 14)
        drawdown_threshold: Maximum drawdown before reducing exposure (default 0.10)
        target_assets: Asset classes this strategy trades
    """

    name = "mean_reversion"

    def __init__(
        self,
        rsi_oversold: float = 30,
        rsi_overbought: float = 70,
        rsi_period: int = 14,
        drawdown_threshold: float = 0.10,
        target_assets: list[AssetClass] | None = None,
    ):
        """Initialize the mean reversion strategy.

        Args:
            rsi_oversold: RSI level below which to signal BUY (default 30)
            rsi_overbought: RSI level above which to signal SELL (default 70)
            rsi_period: Number of periods for RSI calculation (default 14)
            drawdown_threshold: Maximum acceptable drawdown (default 0.10)
            target_assets: List of asset classes to trade. Defaults to major equities.
        """
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.rsi_period = rsi_period
        self.drawdown_threshold = drawdown_threshold
        self.target_assets = target_assets or [
            AssetClass.US_STOCKS,
            AssetClass.INTL_DEVELOPED,
            AssetClass.EMERGING_MARKETS,
        ]

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None,
    ) -> StrategySignal:
        """Generate a trading signal based on RSI levels.

        The strategy evaluates RSI for the primary target asset (US_STOCKS/SPY)
        and generates signals based on oversold/overbought conditions.

        Args:
            prices: DataFrame with columns: date, close, symbol
            calc_date: Date to generate signal for
            current_holding: Currently held asset class, or None if no position

        Returns:
            StrategySignal with BUY/SELL/HOLD action and confidence level
        """
        # Use SPY as the primary symbol for RSI calculation
        symbol = "SPY"

        # Calculate RSI
        rsi = calculate_rsi(prices, symbol, period=self.rsi_period)

        # Handle insufficient data
        if rsi is None:
            logger.warning(
                "insufficient_data_for_rsi",
                symbol=symbol,
                calc_date=str(calc_date),
            )
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.HOLD,
                asset_class=current_holding,
                confidence=0.0,
                reasoning="Insufficient data to calculate RSI",
                metadata={"rsi": None},
            )

        logger.info(
            "mean_reversion_rsi",
            symbol=symbol,
            calc_date=str(calc_date),
            rsi=round(rsi, 2),
            oversold_threshold=self.rsi_oversold,
            overbought_threshold=self.rsi_overbought,
        )

        # Determine signal based on RSI
        if rsi < self.rsi_oversold:
            # Oversold - BUY signal
            # Confidence scales with how oversold (lower RSI = higher confidence)
            confidence = min(1.0, (self.rsi_oversold - rsi) / self.rsi_oversold + 0.5)
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.BUY,
                asset_class=AssetClass.US_STOCKS,
                confidence=confidence,
                reasoning=f"RSI {rsi:.1f} below oversold threshold {self.rsi_oversold}",
                metadata={"rsi": rsi, "threshold": self.rsi_oversold},
            )

        elif rsi > self.rsi_overbought and current_holding is not None:
            # Overbought with position - SELL signal
            # Confidence scales with how overbought (higher RSI = higher confidence)
            confidence = min(
                1.0, (rsi - self.rsi_overbought) / (100 - self.rsi_overbought) + 0.5
            )
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.SELL,
                asset_class=current_holding,
                confidence=confidence,
                reasoning=f"RSI {rsi:.1f} above overbought threshold {self.rsi_overbought}",
                metadata={"rsi": rsi, "threshold": self.rsi_overbought},
            )

        else:
            # RSI in normal range or overbought without position - HOLD
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.HOLD,
                asset_class=current_holding,
                confidence=0.5,
                reasoning=f"RSI {rsi:.1f} in normal range [{self.rsi_oversold}-{self.rsi_overbought}]",
                metadata={"rsi": rsi},
            )

    def get_rebalance_dates(
        self,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        """Get daily business day rebalance dates.

        Mean reversion is an event-driven strategy that should be evaluated
        daily to catch oversold bounces as they occur.

        Args:
            start_date: Start of the period
            end_date: End of the period

        Returns:
            List of business days between start and end dates
        """
        # Generate all business days (Monday-Friday) between start and end
        dates = pd.bdate_range(start=start_date, end=end_date)
        return [d.date() for d in dates]
