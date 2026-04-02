"""Robust quarterly strategy with selective off-cycle early-switch logic."""

from __future__ import annotations

from datetime import date

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import Asset, AssetClass, Signal, SignalAction
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.robust_quarterly import RobustQuarterlyStrategy


class RobustQuarterlyEarlySwitchStrategy(RobustQuarterlyStrategy):
    """Quarterly-first strategy with narrow off-cycle leadership-break switches."""

    def __init__(
        self,
        assets: dict[AssetClass, Asset] | None = None,
        early_switch_spread: float = 0.12,
        breakdown_momentum: float = 0.0,
        min_target_momentum: float = 0.08,
    ) -> None:
        super().__init__()
        # Allow experiments to swap the asset universe without mutating ASSET_REGISTRY.
        if assets is not None:
            self.assets = assets
        self.early_switch_spread = early_switch_spread
        self.breakdown_momentum = breakdown_momentum
        self.min_target_momentum = min_target_momentum

    def get_rebalance_dates(self, start_date: date, end_date: date, frequency: str = "quarterly") -> list[date]:
        return super().get_rebalance_dates(start_date, end_date, "monthly")

    def generate_signal(
        self,
        prices,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> Signal:
        # Compute the raw dual-momentum decision first (ignoring quarterly cadence),
        # then decide whether to allow an off-cycle switch.
        raw_signal = DualMomentumStrategy.generate_signal(
            self,
            prices=prices,
            calc_date=calc_date,
            current_holding=current_holding,
        )

        # Quarter ends behave exactly like Robust_Quarterly (normal rebalance).
        if self._is_quarter_end_rebalance_date(prices, calc_date):
            return raw_signal

        # Off-cycle: allow initial entry (don't force waiting for quarter-end).
        if self.current_holding is None or self.current_holding == AssetClass.CASH:
            return raw_signal if raw_signal.action == SignalAction.BUY else raw_signal

        # Off-cycle: only allow BUY switches on strong leadership breaks.
        if raw_signal.action != SignalAction.BUY or raw_signal.asset is None:
            return raw_signal

        target_class = raw_signal.asset.asset_class
        if target_class == self.current_holding:
            return raw_signal

        scores = raw_signal.momentum_scores
        current_score = scores.get(self.current_holding)
        target_score = scores.get(target_class)
        if current_score is None or target_score is None:
            return raw_signal

        momentum_diff = target_score.momentum_12m - current_score.momentum_12m
        strong_leadership_break = (
            momentum_diff >= self.early_switch_spread
            and target_score.momentum_12m >= self.min_target_momentum
        )
        current_breakdown = current_score.momentum_12m <= self.breakdown_momentum and momentum_diff >= self.switch_threshold

        if strong_leadership_break or current_breakdown:
            raw_signal.reason = (
                f"Early switch override: {raw_signal.reason}; "
                f"spread {momentum_diff:.2%}, current {current_score.momentum_12m:.2%}, "
                f"target {target_score.momentum_12m:.2%}"
            )
            return raw_signal

        held_asset = self.assets.get(self.current_holding, self.assets[AssetClass.CASH])
        return Signal(
            date=calc_date,
            action=SignalAction.HOLD,
            asset=held_asset,
            reason=(
                "Quarterly cadence hold: no leadership-break override; "
                f"spread {momentum_diff:.2%} below {self.early_switch_spread:.2%}"
            ),
            momentum_scores=scores,
        )


def build_robust_quarterly_early_switch_strategy(**kwargs) -> RobustQuarterlyEarlySwitchStrategy:
    return RobustQuarterlyEarlySwitchStrategy(**kwargs)
