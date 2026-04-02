"""Selected production candidate: robust long-horizon dual momentum with quarterly cadence."""

from __future__ import annotations

from datetime import date

import pandas as pd

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, Signal, SignalAction
from aurel2.strategies.dual_momentum import DualMomentumStrategy


class RobustQuarterlyStrategy(DualMomentumStrategy):
    """Robust dual momentum with quarterly rebalancing behavior.

    Selection logic:
    - full asset universe
    - 12m lookback
    - 2% switch threshold
    - 0% cash hurdle
    - pilot entry disabled

    Cadence logic:
    - only allow trades on the last available trading date of quarter-end months
    - otherwise hold the current position (or stay in cash before first entry)
    """

    def __init__(self, *, exclude_from_selection: set[AssetClass] | None = None) -> None:
        super().__init__(
            assets=ASSET_REGISTRY,
            lookback_months=12,
            switch_threshold=0.02,
            cash_rate=0.0,
            pilot_entry_enabled=False,
            exclude_from_selection=exclude_from_selection,
        )

    def _is_quarter_end_rebalance_date(self, prices: pd.DataFrame, calc_date: date) -> bool:
        if calc_date.month not in {3, 6, 9, 12}:
            return False

        spy = prices[prices["symbol"] == "SPY"].copy()
        if spy.empty:
            return False

        spy["date"] = pd.to_datetime(spy["date"])
        month_rows = spy[
            (spy["date"] <= pd.Timestamp(calc_date))
            & (spy["date"].dt.year == calc_date.year)
            & (spy["date"].dt.month == calc_date.month)
        ]
        if month_rows.empty:
            return False

        last_trade_date = month_rows["date"].max().date()
        return calc_date == last_trade_date

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> Signal:
        base_signal = super().generate_signal(prices=prices, calc_date=calc_date, current_holding=current_holding)

        if self._is_quarter_end_rebalance_date(prices, calc_date):
            return base_signal

        held_asset = self.assets.get(self.current_holding or AssetClass.CASH, self.assets[AssetClass.CASH])
        reason = "Quarterly cadence hold: next rebalance on quarter-end trading date"
        return Signal(
            date=calc_date,
            action=SignalAction.HOLD,
            asset=held_asset,
            reason=reason,
            momentum_scores=base_signal.momentum_scores,
        )


def build_robust_quarterly_strategy() -> RobustQuarterlyStrategy:
    """Factory for the selected production trading strategy."""
    return RobustQuarterlyStrategy()


def build_robust_quarterly_no_tlt_strategy() -> RobustQuarterlyStrategy:
    """Variant that bans long-duration treasuries (TLT) from being selected as the winner."""
    return RobustQuarterlyStrategy(exclude_from_selection={AssetClass.BONDS_TREASURY})
