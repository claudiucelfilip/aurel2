"""Dual Momentum Strategy implementation."""

from datetime import date

import pandas as pd
import structlog

from aurel2.core.models import Asset, AssetClass, MomentumScore, Signal, SignalAction
from aurel2.data.momentum import calculate_momentum_scores

logger = structlog.get_logger()


class DualMomentumStrategy:
    """
    Tax-Optimized Dual Momentum Strategy.

    Rules:
    1. Calculate 12-month momentum for each asset
    2. Rank assets by momentum
    3. Only switch if new winner beats current by >threshold OR current has negative momentum
    4. 100% allocation to winner
    """

    def __init__(
        self,
        assets: dict[AssetClass, Asset],
        lookback_months: int = 12,
        switch_threshold: float = 0.10,
        cash_rate: float = 0.04,
    ):
        self.assets = assets
        self.lookback_months = lookback_months
        self.switch_threshold = switch_threshold
        self.cash_rate = cash_rate
        self.current_holding: AssetClass | None = None

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> Signal:
        """
        Generate trading signal based on momentum scores.

        Args:
            prices: Historical price data
            calc_date: Date to generate signal for
            current_holding: Currently held asset class (if any)

        Returns:
            Signal with action and reasoning
        """
        if current_holding is not None:
            self.current_holding = current_holding

        # Calculate momentum scores for all assets
        scores = calculate_momentum_scores(
            prices=prices,
            assets=self.assets,
            calc_date=calc_date,
            lookback_months=self.lookback_months,
            cash_rate=self.cash_rate,
        )

        if not scores:
            logger.error("no_momentum_scores_calculated", date=str(calc_date))
            return Signal(
                date=calc_date,
                action=SignalAction.HOLD,
                asset=self.assets.get(self.current_holding or AssetClass.CASH),
                reason="No momentum data available",
                momentum_scores=scores,
            )

        # Find the winner (highest momentum, excluding cash for now)
        risky_scores = {k: v for k, v in scores.items() if k != AssetClass.CASH}
        if not risky_scores:
            winner_class = AssetClass.CASH
        else:
            winner_class = max(risky_scores.keys(), key=lambda k: risky_scores[k].momentum_12m)

        winner_score = scores[winner_class]
        cash_score = scores.get(AssetClass.CASH)

        # Log momentum scores
        for asset_class, score in sorted(scores.items(), key=lambda x: x[1].momentum_12m, reverse=True):
            logger.info(
                "momentum_score",
                date=str(calc_date),
                asset=asset_class.value,
                momentum=f"{score.momentum_12m:.2%}",
            )

        # Decision logic
        if self.current_holding is None:
            # No current position - buy the winner if it beats cash
            if cash_score and winner_score.momentum_12m <= cash_score.momentum_12m:
                return Signal(
                    date=calc_date,
                    action=SignalAction.BUY,
                    asset=self.assets[AssetClass.CASH],
                    reason=f"Winner ({winner_class.value}: {winner_score.momentum_12m:.2%}) doesn't beat cash ({cash_score.momentum_12m:.2%})",
                    momentum_scores=scores,
                )
            else:
                self.current_holding = winner_class
                return Signal(
                    date=calc_date,
                    action=SignalAction.BUY,
                    asset=winner_score.asset,
                    reason=f"Initial buy: {winner_class.value} has highest momentum ({winner_score.momentum_12m:.2%})",
                    momentum_scores=scores,
                )

        current_score = scores.get(self.current_holding)
        if current_score is None:
            # Can't find current holding data, switch to winner
            self.current_holding = winner_class
            return Signal(
                date=calc_date,
                action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"No data for current holding, switching to {winner_class.value}",
                momentum_scores=scores,
            )

        # Check absolute momentum - if current holding is negative, consider cash
        if cash_score and current_score.momentum_12m < cash_score.momentum_12m:
            # Current holding has negative relative momentum
            if winner_score.momentum_12m < cash_score.momentum_12m:
                # Even winner is below cash - go to cash
                self.current_holding = AssetClass.CASH
                return Signal(
                    date=calc_date,
                    action=SignalAction.BUY,
                    asset=self.assets[AssetClass.CASH],
                    reason=f"All assets below cash. Current: {current_score.momentum_12m:.2%}, Cash: {cash_score.momentum_12m:.2%}",
                    momentum_scores=scores,
                )

        # Check if winner beats current holding by threshold
        momentum_diff = winner_score.momentum_12m - current_score.momentum_12m

        if winner_class != self.current_holding and momentum_diff > self.switch_threshold:
            # Switch to new winner
            old_holding = self.current_holding
            self.current_holding = winner_class
            return Signal(
                date=calc_date,
                action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"Switching from {old_holding.value} ({current_score.momentum_12m:.2%}) to {winner_class.value} ({winner_score.momentum_12m:.2%}). Diff: {momentum_diff:.2%} > {self.switch_threshold:.2%}",
                momentum_scores=scores,
            )

        # Hold current position
        return Signal(
            date=calc_date,
            action=SignalAction.HOLD,
            asset=current_score.asset,
            reason=f"Holding {self.current_holding.value} ({current_score.momentum_12m:.2%}). Winner {winner_class.value} ({winner_score.momentum_12m:.2%}) diff {momentum_diff:.2%} < threshold {self.switch_threshold:.2%}",
            momentum_scores=scores,
        )

    def get_rebalance_dates(
        self,
        start_date: date,
        end_date: date,
        frequency: str = "quarterly",
    ) -> list[date]:
        """
        Generate rebalance dates based on frequency.

        Args:
            start_date: Start of period
            end_date: End of period
            frequency: 'monthly' or 'quarterly'

        Returns:
            List of rebalance dates
        """
        dates = pd.date_range(start=start_date, end=end_date, freq="ME")  # Month End

        if frequency == "quarterly":
            # Filter to quarter ends (March, June, September, December)
            dates = [d for d in dates if d.month in [3, 6, 9, 12]]
        elif frequency == "monthly":
            pass  # Keep all month ends
        else:
            raise ValueError(f"Unknown frequency: {frequency}")

        return [d.date() for d in dates]
