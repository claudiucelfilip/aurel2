"""Robust quarterly strategy with a narrow off-cycle crisis override."""

from __future__ import annotations

from datetime import date

import pandas as pd

from aurel2.core.models import AssetCategory, AssetClass, Signal, SignalAction
from aurel2.strategies.robust_quarterly import RobustQuarterlyStrategy


class RobustQuarterlyCrisisStrategy(RobustQuarterlyStrategy):
    """Quarterly-first policy with early defensive exits during crisis breaks.

    Design:
    - Run the same robust selection logic as the quarterly baseline.
    - Evaluate monthly, but only allow off-cycle trades when market conditions
      are clearly bearish and the model wants to rotate from equity into a
      defensive asset or cash.
    """

    def __init__(self, drawdown_trigger: float = 0.12, vol_percentile_trigger: float = 0.75) -> None:
        super().__init__()
        self.drawdown_trigger = drawdown_trigger
        self.vol_percentile_trigger = vol_percentile_trigger

    def get_rebalance_dates(self, start_date: date, end_date: date, frequency: str = "quarterly") -> list[date]:
        return super().get_rebalance_dates(start_date, end_date, "monthly")

    def _detect_crisis(self, prices: pd.DataFrame, calc_date: date) -> bool:
        spy = prices[prices["symbol"] == "SPY"].copy()
        if spy.empty:
            return False

        spy["date"] = pd.to_datetime(spy["date"])
        spy = spy[spy["date"] <= pd.Timestamp(calc_date)].sort_values("date")
        if len(spy) < 252:
            return False

        close = float(spy.iloc[-1]["close"])
        high_252 = float(spy.tail(252)["close"].max())
        drawdown = (high_252 - close) / high_252 if high_252 else 0.0

        sma_200 = float(spy.tail(200)["close"].mean()) if len(spy) >= 200 else close

        returns = spy["close"].pct_change().dropna()
        vol_vote = False
        if len(returns) >= 252:
            vol_20 = float(returns.tail(20).std()) * (252 ** 0.5)
            rolling = returns.tail(252).rolling(20).std().dropna() * (252 ** 0.5)
            vol_pctl = float((rolling < vol_20).mean()) if not rolling.empty else 0.5
            vol_vote = vol_pctl >= self.vol_percentile_trigger

        drawdown_vote = drawdown >= self.drawdown_trigger
        trend_vote = close < sma_200
        votes = sum([drawdown_vote, trend_vote, vol_vote])
        return votes >= 2

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> Signal:
        base_signal = super().generate_signal(prices=prices, calc_date=calc_date, current_holding=current_holding)

        if self._is_quarter_end_rebalance_date(prices, calc_date):
            return base_signal

        if self.current_holding is None:
            return base_signal

        current_asset = self.assets.get(self.current_holding, self.assets[AssetClass.CASH])
        current_is_equity = current_asset.category == AssetCategory.EQUITY
        target_asset = base_signal.asset
        target_is_defensive = (
            target_asset is not None
            and (target_asset.asset_class == AssetClass.CASH or target_asset.category != AssetCategory.EQUITY)
        )

        if (
            current_is_equity
            and base_signal.action == SignalAction.BUY
            and target_is_defensive
            and self._detect_crisis(prices, calc_date)
        ):
            base_signal.reason = f"Crisis override: {base_signal.reason}"
            return base_signal

        return Signal(
            date=calc_date,
            action=SignalAction.HOLD,
            asset=current_asset,
            reason="Quarterly cadence hold: no crisis override triggered",
            momentum_scores=base_signal.momentum_scores,
        )


def build_robust_quarterly_crisis_strategy() -> RobustQuarterlyCrisisStrategy:
    return RobustQuarterlyCrisisStrategy()
