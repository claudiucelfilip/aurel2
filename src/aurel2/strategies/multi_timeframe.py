"""Multi-Timeframe Trend Strategy for faster momentum reaction with reduced whipsaws.

This strategy blends momentum from multiple timeframes (default: 3, 6, 12 months)
using configurable weights to provide faster reaction than pure 12-month momentum
while reducing whipsaw signals through the averaging effect.
"""

from datetime import date

import pandas as pd
import structlog
from dateutil.relativedelta import relativedelta

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, SignalAction
from aurel2.strategies.base import BaseStrategy, StrategySignal

logger = structlog.get_logger()

# Default asset-to-symbol mapping
ASSET_SYMBOL_MAP = {
    AssetClass.US_STOCKS: "SPY",
    AssetClass.INTL_DEVELOPED: "EFA",
    AssetClass.EMERGING_MARKETS: "EEM",
    AssetClass.TECH_SECTOR: "XLK",
    AssetClass.FINANCIAL_SECTOR: "XLF",
    AssetClass.ENERGY_SECTOR: "XLE",
    AssetClass.HEALTHCARE_SECTOR: "XLV",
    AssetClass.BONDS_AGGREGATE: "AGG",
    AssetClass.BONDS_TREASURY: "TLT",
    AssetClass.BONDS_SHORT_TERM: "SHY",
    AssetClass.BONDS_INTERMEDIATE: "IEF",
    AssetClass.TIPS: "TIP",
    AssetClass.REITS: "VNQ",
    AssetClass.GOLD: "GLD",
    AssetClass.COMMODITIES: "DBC",
    AssetClass.SMALL_CAP_VALUE: "IJS",
}


class MultiTimeframeTrendStrategy(BaseStrategy):
    """Multi-timeframe trend strategy using blended momentum.

    This strategy:
    - Calculates momentum over multiple lookback periods (default: 1, 3, 6, 12 months)
    - Computes a weighted average (blended momentum) using configurable weights
    - Signals BUY for the asset with highest blended momentum
    - Uses a switch threshold to reduce unnecessary trading

    TUNED PARAMETERS (v2):
    - Added 1-month lookback for faster reaction
    - Increased short-term weights for faster trend detection
    - Reduced switch threshold for quicker adaptation

    Attributes:
        name: Strategy identifier ("multi_timeframe_trend")
        lookback_months: List of lookback periods in months (default [1, 3, 6, 12])
        weights: Corresponding weights for each lookback (default [0.30, 0.30, 0.25, 0.15])
        switch_threshold: Minimum momentum difference to trigger a switch (default 0.03)
        target_assets: Asset classes this strategy trades
    """

    name = "multi_timeframe_trend"

    def __init__(
        self,
        lookback_months: list[int] | None = None,
        weights: list[float] | None = None,
        switch_threshold: float = 0.03,  # Reduced from 0.05 for faster adaptation
        target_assets: list[AssetClass] | None = None,
    ):
        """Initialize the multi-timeframe trend strategy.

        Args:
            lookback_months: List of lookback periods in months (default [1, 3, 6, 12])
            weights: Weights for each lookback period (default [0.30, 0.30, 0.25, 0.15])
            switch_threshold: Minimum momentum difference to trigger asset switch (default 0.03)
            target_assets: List of asset classes to trade

        Raises:
            ValueError: If weights length doesn't match lookback_months length
        """
        # Added 1-month lookback, increased short-term weights
        self.lookback_months = lookback_months or [1, 3, 6, 12]
        self.weights = weights or [0.30, 0.30, 0.25, 0.15]  # More weight on short-term
        self.switch_threshold = switch_threshold
        self.target_assets = target_assets or [
            AssetClass.US_STOCKS,
            AssetClass.INTL_DEVELOPED,
            AssetClass.BONDS_AGGREGATE,
        ]

        # Validate weights match lookbacks
        if len(self.weights) != len(self.lookback_months):
            raise ValueError(
                f"Number of weights ({len(self.weights)}) must match "
                f"number of lookback periods ({len(self.lookback_months)})"
            )

    def _calculate_momentum(
        self,
        prices: pd.DataFrame,
        symbol: str,
        calc_date: date,
        lookback_months: int,
    ) -> float | None:
        """Calculate momentum for a single lookback period.

        Args:
            prices: DataFrame with date, close, symbol columns
            symbol: The symbol to calculate momentum for
            calc_date: The date to calculate momentum as of
            lookback_months: Number of months to look back

        Returns:
            Momentum as a decimal (e.g., 0.10 for 10%), or None if insufficient data
        """
        # Filter to this symbol
        symbol_prices = prices[prices["symbol"] == symbol].copy()
        if symbol_prices.empty:
            return None

        symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
        symbol_prices = symbol_prices.sort_values("date")

        # Filter to data before or on calc_date
        symbol_prices = symbol_prices[symbol_prices["date"] <= pd.Timestamp(calc_date)]

        if symbol_prices.empty:
            return None

        # Get current price (most recent price on or before calc_date)
        current_price = symbol_prices.iloc[-1]["close"]
        current_date = symbol_prices.iloc[-1]["date"].date()

        # Calculate lookback date
        lookback_date = current_date - relativedelta(months=lookback_months)

        # Find price on or just after the lookback date
        lookback_prices = symbol_prices[
            symbol_prices["date"] >= pd.Timestamp(lookback_date)
        ]

        if lookback_prices.empty or len(lookback_prices) < 5:
            # Need at least some data points after lookback date
            return None

        # Get the earliest price after lookback date
        past_price = lookback_prices.iloc[0]["close"]

        if past_price <= 0:
            return None

        # Calculate momentum as return
        momentum = (current_price - past_price) / past_price
        return momentum

    def _calculate_blended_momentum(
        self,
        prices: pd.DataFrame,
        symbol: str,
        calc_date: date,
    ) -> dict[str, float | None]:
        """Calculate blended momentum from multiple timeframes.

        Args:
            prices: DataFrame with date, close, symbol columns
            symbol: The symbol to calculate momentum for
            calc_date: The date to calculate momentum as of

        Returns:
            Dict with momentum_Nm keys and blended_momentum
        """
        momenta = {}
        valid_momenta = []
        valid_weights = []

        for i, months in enumerate(self.lookback_months):
            momentum = self._calculate_momentum(prices, symbol, calc_date, months)
            momenta[f"momentum_{months}m"] = momentum

            if momentum is not None:
                valid_momenta.append(momentum)
                valid_weights.append(self.weights[i])

        # Calculate blended momentum
        if valid_momenta and valid_weights:
            # Normalize weights if some periods had no data
            weight_sum = sum(valid_weights)
            if weight_sum > 0:
                normalized_weights = [w / weight_sum for w in valid_weights]
                blended = sum(m * w for m, w in zip(valid_momenta, normalized_weights))
                momenta["blended_momentum"] = blended
            else:
                momenta["blended_momentum"] = None
        else:
            momenta["blended_momentum"] = None

        return momenta

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None,
    ) -> StrategySignal:
        """Generate a trading signal based on blended multi-timeframe momentum.

        Args:
            prices: DataFrame with columns: date, close, symbol
            calc_date: Date to generate signal for
            current_holding: Currently held asset class, or None if no position

        Returns:
            StrategySignal with BUY/HOLD action and momentum data in metadata
        """
        # Calculate blended momentum for all target assets
        asset_momenta: dict[AssetClass, dict[str, float | None]] = {}

        for asset_class in self.target_assets:
            symbol = ASSET_SYMBOL_MAP.get(asset_class)
            if symbol is None:
                continue

            momenta = self._calculate_blended_momentum(prices, symbol, calc_date)
            if momenta.get("blended_momentum") is not None:
                asset_momenta[asset_class] = momenta

        # Handle insufficient data
        if not asset_momenta:
            logger.warning(
                "insufficient_data_for_momentum",
                calc_date=str(calc_date),
                target_assets=[a.value for a in self.target_assets],
            )
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.HOLD,
                asset_class=current_holding,
                confidence=0.0,
                reasoning="Insufficient data to calculate momentum",
                metadata={
                    "momentum_1m": None,
                    "momentum_3m": None,
                    "momentum_6m": None,
                    "momentum_12m": None,
                    "blended_momentum": None,
                },
            )

        # Find asset with highest blended momentum
        winner_class = max(
            asset_momenta.keys(),
            key=lambda k: asset_momenta[k]["blended_momentum"],
        )
        winner_momenta = asset_momenta[winner_class]
        winner_blended = winner_momenta["blended_momentum"]

        # Log momentum scores
        for asset_class, momenta in sorted(
            asset_momenta.items(),
            key=lambda x: x[1]["blended_momentum"],
            reverse=True,
        ):
            logger.info(
                "multi_timeframe_momentum",
                date=str(calc_date),
                asset=asset_class.value,
                blended_momentum=f"{momenta['blended_momentum']:.2%}",
                momentum_3m=f"{momenta.get('momentum_3m', 0):.2%}" if momenta.get("momentum_3m") else "N/A",
                momentum_6m=f"{momenta.get('momentum_6m', 0):.2%}" if momenta.get("momentum_6m") else "N/A",
                momentum_12m=f"{momenta.get('momentum_12m', 0):.2%}" if momenta.get("momentum_12m") else "N/A",
            )

        # Build metadata from winner
        metadata = {
            "momentum_1m": winner_momenta.get("momentum_1m"),
            "momentum_3m": winner_momenta.get("momentum_3m"),
            "momentum_6m": winner_momenta.get("momentum_6m"),
            "momentum_12m": winner_momenta.get("momentum_12m"),
            "blended_momentum": winner_blended,
        }

        # Calculate confidence based on momentum strength and consistency
        confidence = self._calculate_confidence(winner_momenta)

        # Decision logic
        if current_holding is None:
            # No current position - buy the winner
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.BUY,
                asset_class=winner_class,
                confidence=confidence,
                reasoning=f"Initial buy: {winner_class.value} has highest blended momentum ({winner_blended:.2%})",
                metadata=metadata,
            )

        # Have a current holding - check if we should switch
        current_momenta = asset_momenta.get(current_holding)

        if current_momenta is None:
            # Current holding not in target assets or no data - switch to winner
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.BUY,
                asset_class=winner_class,
                confidence=confidence,
                reasoning=f"No data for current holding {current_holding.value}, switching to {winner_class.value}",
                metadata=metadata,
            )

        current_blended = current_momenta["blended_momentum"]
        momentum_diff = winner_blended - current_blended

        if winner_class != current_holding and momentum_diff > self.switch_threshold:
            # Switch to new winner
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.BUY,
                asset_class=winner_class,
                confidence=confidence,
                reasoning=(
                    f"Switching from {current_holding.value} ({current_blended:.2%}) "
                    f"to {winner_class.value} ({winner_blended:.2%}). "
                    f"Diff: {momentum_diff:.2%} > threshold {self.switch_threshold:.2%}"
                ),
                metadata=metadata,
            )

        # Hold current position
        return StrategySignal(
            strategy_name=self.name,
            date=calc_date,
            action=SignalAction.HOLD,
            asset_class=current_holding,
            confidence=confidence * 0.8,  # Slightly lower confidence for hold
            reasoning=(
                f"Holding {current_holding.value} ({current_blended:.2%}). "
                f"Winner {winner_class.value} ({winner_blended:.2%}) "
                f"diff {momentum_diff:.2%} < threshold {self.switch_threshold:.2%}"
            ),
            metadata=metadata,
        )

    def _calculate_confidence(self, momenta: dict[str, float | None]) -> float:
        """Calculate confidence based on momentum strength and consistency.

        Args:
            momenta: Dict with momentum values for each timeframe

        Returns:
            Confidence score between 0 and 1
        """
        blended = momenta.get("blended_momentum")
        if blended is None:
            return 0.0

        # Base confidence from blended momentum magnitude
        # >20% momentum = high confidence, <0% = low confidence
        magnitude_score = min(1.0, max(0.0, (blended + 0.1) / 0.3))

        # Consistency bonus: if all timeframes agree on direction
        individual_momenta = [
            m for k, m in momenta.items()
            if k.startswith("momentum_") and m is not None
        ]

        if individual_momenta:
            all_positive = all(m > 0 for m in individual_momenta)
            all_negative = all(m < 0 for m in individual_momenta)
            consistency_bonus = 0.2 if (all_positive or all_negative) else 0.0
        else:
            consistency_bonus = 0.0

        confidence = min(1.0, magnitude_score + consistency_bonus)
        return max(0.0, confidence)

    def get_rebalance_dates(
        self,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        """Get monthly rebalance dates (month-end).

        Args:
            start_date: Start of the period
            end_date: End of the period

        Returns:
            List of month-end dates between start and end
        """
        dates = pd.date_range(start=start_date, end=end_date, freq="ME")
        return [d.date() for d in dates]
