"""Adaptive Momentum Strategy with regime detection."""

from datetime import date
from enum import Enum

import pandas as pd
import structlog

from aurel2.core.models import Asset, AssetClass, MomentumScore, Signal, SignalAction
from aurel2.data.momentum import calculate_momentum_scores

logger = structlog.get_logger()


class MarketRegime(str, Enum):
    """Market regime classification."""
    BULL = "bull"       # Price > 200-day MA, trending up
    BEAR = "bear"       # Price < 200-day MA, trending down
    NEUTRAL = "neutral" # Sideways/uncertain


class AdaptiveMomentumStrategy:
    """
    Adaptive Dual Momentum Strategy with Regime Detection.

    The strategy adapts its behavior based on detected market regime:

    BULL MARKET (SPY > 200-day MA):
        - No cash rule (stay invested)
        - Lower switch threshold (5%) for faster adaptation
        - Favors equities

    BEAR MARKET (SPY < 200-day MA):
        - Cash rule active (flee to safety when all negative)
        - Higher switch threshold (10%) to avoid whipsaws
        - Considers bonds/cash as valid holdings

    This aims to capture upside in bull markets while protecting in bear markets.
    """

    def __init__(
        self,
        assets: dict[AssetClass, Asset],
        lookback_months: int = 12,
        ma_period: int = 200,  # 200-day moving average for regime detection
        bull_switch_threshold: float = 0.05,
        bear_switch_threshold: float = 0.10,
        bull_cash_rate: float = -999.0,  # Effectively disables cash rule in bull
        bear_cash_rate: float = 0.04,
    ):
        self.assets = assets
        self.lookback_months = lookback_months
        self.ma_period = ma_period
        self.bull_switch_threshold = bull_switch_threshold
        self.bear_switch_threshold = bear_switch_threshold
        self.bull_cash_rate = bull_cash_rate
        self.bear_cash_rate = bear_cash_rate
        self.current_holding: AssetClass | None = None
        self.current_regime: MarketRegime = MarketRegime.NEUTRAL

    def detect_regime(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        benchmark_symbol: str = "SPY",
    ) -> MarketRegime:
        """
        Detect market regime using 200-day moving average.

        Bull: Price > 200-day MA AND MA is rising
        Bear: Price < 200-day MA AND MA is falling
        Neutral: Otherwise
        """
        # Get benchmark prices
        benchmark = prices[prices["symbol"] == benchmark_symbol].copy()
        if benchmark.empty:
            return MarketRegime.NEUTRAL

        benchmark["date"] = pd.to_datetime(benchmark["date"])
        benchmark = benchmark.sort_values("date")

        # Filter to data before calc_date
        benchmark = benchmark[benchmark["date"] <= pd.Timestamp(calc_date)]

        if len(benchmark) < self.ma_period:
            return MarketRegime.NEUTRAL

        # Calculate 200-day MA
        benchmark["ma_200"] = benchmark["close"].rolling(window=self.ma_period).mean()

        # Get current values
        current_price = benchmark.iloc[-1]["close"]
        current_ma = benchmark.iloc[-1]["ma_200"]

        # Get MA from 20 days ago to check trend
        if len(benchmark) >= self.ma_period + 20:
            ma_20_days_ago = benchmark.iloc[-21]["ma_200"]
            ma_trend = current_ma > ma_20_days_ago
        else:
            ma_trend = True  # Default to bullish if not enough data

        # Determine regime
        if current_price > current_ma and ma_trend:
            regime = MarketRegime.BULL
        elif current_price < current_ma and not ma_trend:
            regime = MarketRegime.BEAR
        else:
            # Price crossed MA but trend not confirmed - use previous regime
            regime = MarketRegime.NEUTRAL

        logger.info(
            "regime_detected",
            date=str(calc_date),
            regime=regime.value,
            price=f"${current_price:.2f}",
            ma_200=f"${current_ma:.2f}",
            price_vs_ma=f"{(current_price/current_ma - 1)*100:+.1f}%",
        )

        return regime

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> Signal:
        """
        Generate trading signal based on momentum and regime.
        """
        if current_holding is not None:
            self.current_holding = current_holding

        # Detect market regime
        self.current_regime = self.detect_regime(prices, calc_date)

        # Set parameters based on regime
        if self.current_regime == MarketRegime.BULL:
            switch_threshold = self.bull_switch_threshold
            cash_rate = self.bull_cash_rate
        elif self.current_regime == MarketRegime.BEAR:
            switch_threshold = self.bear_switch_threshold
            cash_rate = self.bear_cash_rate
        else:
            # Neutral - use intermediate values
            switch_threshold = (self.bull_switch_threshold + self.bear_switch_threshold) / 2
            cash_rate = self.bear_cash_rate

        # Calculate momentum scores
        scores = calculate_momentum_scores(
            prices=prices,
            assets=self.assets,
            calc_date=calc_date,
            lookback_months=self.lookback_months,
            cash_rate=cash_rate,
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

        # Find the winner (highest momentum, excluding cash)
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
                regime=self.current_regime.value,
            )

        # Add regime info to reason
        regime_prefix = f"[{self.current_regime.value.upper()}] "

        # Decision logic
        if self.current_holding is None:
            # No current position - buy the winner if it beats cash
            if cash_score and winner_score.momentum_12m <= cash_score.momentum_12m:
                return Signal(
                    date=calc_date,
                    action=SignalAction.BUY,
                    asset=self.assets[AssetClass.CASH],
                    reason=f"{regime_prefix}Winner ({winner_class.value}: {winner_score.momentum_12m:.2%}) doesn't beat cash ({cash_score.momentum_12m:.2%})",
                    momentum_scores=scores,
                )
            else:
                self.current_holding = winner_class
                return Signal(
                    date=calc_date,
                    action=SignalAction.BUY,
                    asset=winner_score.asset,
                    reason=f"{regime_prefix}Initial buy: {winner_class.value} has highest momentum ({winner_score.momentum_12m:.2%})",
                    momentum_scores=scores,
                )

        current_score = scores.get(self.current_holding)
        if current_score is None:
            self.current_holding = winner_class
            return Signal(
                date=calc_date,
                action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"{regime_prefix}No data for current holding, switching to {winner_class.value}",
                momentum_scores=scores,
            )

        # Check absolute momentum - only apply cash rule in bear markets
        if self.current_regime != MarketRegime.BULL:
            if cash_score and current_score.momentum_12m < cash_score.momentum_12m:
                if winner_score.momentum_12m < cash_score.momentum_12m:
                    self.current_holding = AssetClass.CASH
                    return Signal(
                        date=calc_date,
                        action=SignalAction.BUY,
                        asset=self.assets[AssetClass.CASH],
                        reason=f"{regime_prefix}All assets below cash. Current: {current_score.momentum_12m:.2%}, Cash: {cash_score.momentum_12m:.2%}",
                        momentum_scores=scores,
                    )

        # Check if winner beats current holding by threshold
        momentum_diff = winner_score.momentum_12m - current_score.momentum_12m

        if winner_class != self.current_holding and momentum_diff > switch_threshold:
            old_holding = self.current_holding
            self.current_holding = winner_class
            return Signal(
                date=calc_date,
                action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"{regime_prefix}Switching from {old_holding.value} ({current_score.momentum_12m:.2%}) to {winner_class.value} ({winner_score.momentum_12m:.2%}). Diff: {momentum_diff:.2%} > {switch_threshold:.2%}",
                momentum_scores=scores,
            )

        # Hold current position
        return Signal(
            date=calc_date,
            action=SignalAction.HOLD,
            asset=current_score.asset,
            reason=f"{regime_prefix}Holding {self.current_holding.value} ({current_score.momentum_12m:.2%}). Winner {winner_class.value} ({winner_score.momentum_12m:.2%}) diff {momentum_diff:.2%} < threshold {switch_threshold:.2%}",
            momentum_scores=scores,
        )

    def get_rebalance_dates(
        self,
        start_date: date,
        end_date: date,
        frequency: str = "quarterly",
    ) -> list[date]:
        """Generate rebalance dates based on frequency."""
        dates = pd.date_range(start=start_date, end=end_date, freq="ME")

        if frequency == "quarterly":
            dates = [d for d in dates if d.month in [3, 6, 9, 12]]
        elif frequency == "monthly":
            pass

        return [d.date() for d in dates]
