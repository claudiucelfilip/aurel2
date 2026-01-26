"""Dual Momentum Strategy implementation."""

from datetime import date

import pandas as pd
import structlog

from aurel2.core.models import Asset, AssetClass, MomentumScore, Signal, SignalAction
from aurel2.data.momentum import calculate_momentum_scores

logger = structlog.get_logger()


class DualMomentumStrategy:
    """
    Tax-Optimized Dual Momentum Strategy with Pilot Entry.

    Rules:
    1. Calculate 12-month momentum for each asset
    2. Rank assets by momentum
    3. Only switch if new winner beats current by >threshold OR current has negative momentum
    4. 100% allocation to winner

    Pilot Entry Enhancement:
    - When 3-month momentum is positive but 12-month hasn't triggered, enter with pilot position
    - Scale to full position when 12-month momentum confirms
    - Addresses the "late entry" problem identified in failure analysis
    """

    def __init__(
        self,
        assets: dict[AssetClass, Asset],
        lookback_months: int = 12,
        switch_threshold: float = 0.10,
        cash_rate: float = 0.04,
        pilot_entry_enabled: bool = True,
        pilot_lookback_months: int = 3,
        pilot_position_size: float = 0.30,
    ):
        self.assets = assets
        self.lookback_months = lookback_months
        self.switch_threshold = switch_threshold
        self.cash_rate = cash_rate
        self.current_holding: AssetClass | None = None
        # Pilot entry settings
        self.pilot_entry_enabled = pilot_entry_enabled
        self.pilot_lookback_months = pilot_lookback_months
        self.pilot_position_size = pilot_position_size
        self.is_pilot_position: bool = False  # Track if current position is pilot-sized

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

        # Calculate 12-month momentum scores for all assets
        scores = calculate_momentum_scores(
            prices=prices,
            assets=self.assets,
            calc_date=calc_date,
            lookback_months=self.lookback_months,
            cash_rate=self.cash_rate,
        )

        # Calculate short-term (pilot) momentum scores if enabled
        pilot_scores = None
        if self.pilot_entry_enabled:
            pilot_scores = calculate_momentum_scores(
                prices=prices,
                assets=self.assets,
                calc_date=calc_date,
                lookback_months=self.pilot_lookback_months,
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

        # Find the winner based on 12-month momentum (excluding cash)
        risky_scores = {k: v for k, v in scores.items() if k != AssetClass.CASH}
        if not risky_scores:
            winner_class = AssetClass.CASH
        else:
            winner_class = max(risky_scores.keys(), key=lambda k: risky_scores[k].momentum_12m)

        winner_score = scores[winner_class]
        cash_score = scores.get(AssetClass.CASH)

        # Find short-term winner if pilot entry enabled
        pilot_winner_class = None
        pilot_winner_score = None
        if pilot_scores:
            pilot_risky_scores = {k: v for k, v in pilot_scores.items() if k != AssetClass.CASH}
            if pilot_risky_scores:
                pilot_winner_class = max(pilot_risky_scores.keys(), key=lambda k: pilot_risky_scores[k].momentum_12m)
                pilot_winner_score = pilot_scores[pilot_winner_class]

        # Log momentum scores
        for asset_class, score in sorted(scores.items(), key=lambda x: x[1].momentum_12m, reverse=True):
            pilot_mom = pilot_scores[asset_class].momentum_12m if pilot_scores and asset_class in pilot_scores else None
            logger.info(
                "momentum_score",
                date=str(calc_date),
                asset=asset_class.value,
                momentum_12m=f"{score.momentum_12m:.2%}",
                momentum_3m=f"{pilot_mom:.2%}" if pilot_mom is not None else "N/A",
            )

        # Decision logic
        if self.current_holding is None:
            # No current position - check pilot entry first
            if self.pilot_entry_enabled and pilot_winner_score and pilot_winner_class:
                # Check if short-term momentum is strongly positive but 12-month not yet triggered
                pilot_mom = pilot_winner_score.momentum_12m
                long_mom = scores[pilot_winner_class].momentum_12m if pilot_winner_class in scores else 0

                # Pilot entry: 3-month momentum positive (>5%) but 12-month not clearly dominant
                if pilot_mom > 0.05 and long_mom <= 0.10:
                    self.current_holding = pilot_winner_class
                    self.is_pilot_position = True
                    return Signal(
                        date=calc_date,
                        action=SignalAction.BUY,
                        asset=self.assets[pilot_winner_class],
                        reason=f"PILOT ENTRY: {pilot_winner_class.value} 3m momentum ({pilot_mom:.2%}) strong, 12m ({long_mom:.2%}) emerging. Position size: {self.pilot_position_size:.0%}",
                        momentum_scores=scores,
                    )

            # Standard entry: buy the winner if it beats cash
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
                self.is_pilot_position = False
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
            self.is_pilot_position = False
            return Signal(
                date=calc_date,
                action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"No data for current holding, switching to {winner_class.value}",
                momentum_scores=scores,
            )

        # Check if we have a pilot position that should be scaled up
        if self.is_pilot_position:
            # Scale up to full position if 12-month momentum confirms
            if current_score.momentum_12m > 0.10:  # 12-month now strong
                self.is_pilot_position = False
                return Signal(
                    date=calc_date,
                    action=SignalAction.BUY,
                    asset=current_score.asset,
                    reason=f"SCALE UP: {self.current_holding.value} 12m momentum now confirmed ({current_score.momentum_12m:.2%}). Scaling pilot to full position.",
                    momentum_scores=scores,
                )
            # Check if pilot should be exited (3-month turned negative)
            if pilot_scores and self.current_holding in pilot_scores:
                pilot_current = pilot_scores[self.current_holding].momentum_12m
                if pilot_current < -0.02:  # 3-month turned negative
                    self.current_holding = None
                    self.is_pilot_position = False
                    return Signal(
                        date=calc_date,
                        action=SignalAction.SELL,
                        asset=current_score.asset,
                        reason=f"EXIT PILOT: {current_score.asset.symbol} 3m momentum turned negative ({pilot_current:.2%}). Exiting pilot position.",
                        momentum_scores=scores,
                    )

        # Check absolute momentum - if current holding is negative, consider cash
        if cash_score and current_score.momentum_12m < cash_score.momentum_12m:
            # Current holding has negative relative momentum
            if winner_score.momentum_12m < cash_score.momentum_12m:
                # Even winner is below cash - go to cash
                self.current_holding = AssetClass.CASH
                self.is_pilot_position = False
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
            self.is_pilot_position = False
            return Signal(
                date=calc_date,
                action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"Switching from {old_holding.value} ({current_score.momentum_12m:.2%}) to {winner_class.value} ({winner_score.momentum_12m:.2%}). Diff: {momentum_diff:.2%} > {self.switch_threshold:.2%}",
                momentum_scores=scores,
            )

        # Hold current position
        position_type = "PILOT " if self.is_pilot_position else ""
        return Signal(
            date=calc_date,
            action=SignalAction.HOLD,
            asset=current_score.asset,
            reason=f"Holding {position_type}{self.current_holding.value} ({current_score.momentum_12m:.2%}). Winner {winner_class.value} ({winner_score.momentum_12m:.2%}) diff {momentum_diff:.2%} < threshold {self.switch_threshold:.2%}",
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
