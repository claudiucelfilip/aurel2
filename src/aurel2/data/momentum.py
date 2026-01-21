"""Momentum calculations."""

from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

import pandas as pd
import structlog

from aurel2.core.models import Asset, AssetClass, MomentumScore

logger = structlog.get_logger()


def calculate_momentum(
    prices: pd.DataFrame,
    calc_date: date,
    lookback_months: int = 12,
) -> dict[str, float]:
    """
    Calculate momentum for each symbol in the price data.

    Momentum = (Current Price / Price N months ago) - 1

    Args:
        prices: DataFrame with columns: date, close, symbol
        calc_date: Date to calculate momentum for
        lookback_months: Number of months to look back

    Returns:
        Dictionary mapping symbol to momentum value
    """
    lookback_date = calc_date - relativedelta(months=lookback_months)

    results = {}

    for symbol in prices["symbol"].unique():
        symbol_prices = prices[prices["symbol"] == symbol].copy()
        symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])

        # Get current price (closest to calc_date)
        current = symbol_prices[symbol_prices["date"] <= pd.Timestamp(calc_date)]
        if current.empty:
            logger.warning("no_current_price", symbol=symbol, date=str(calc_date))
            continue
        current_price = current.iloc[-1]["close"]
        current_date = current.iloc[-1]["date"].date()

        # Get price from lookback period (closest to lookback_date)
        past = symbol_prices[symbol_prices["date"] <= pd.Timestamp(lookback_date)]
        if past.empty:
            logger.warning("no_lookback_price", symbol=symbol, lookback_date=str(lookback_date))
            continue
        past_price = past.iloc[-1]["close"]

        if past_price == 0:
            logger.warning("zero_past_price", symbol=symbol)
            continue

        momentum = (current_price / past_price) - 1
        results[symbol] = momentum

        logger.debug(
            "calculated_momentum",
            symbol=symbol,
            current_price=current_price,
            past_price=past_price,
            momentum=f"{momentum:.2%}",
        )

    return results


def calculate_momentum_scores(
    prices: pd.DataFrame,
    assets: dict[AssetClass, Asset],
    calc_date: date,
    lookback_months: int = 12,
    cash_rate: float = 0.04,
) -> dict[AssetClass, MomentumScore]:
    """
    Calculate MomentumScore objects for all assets.

    Args:
        prices: DataFrame with columns: date, close, symbol
        assets: Mapping of asset class to Asset
        calc_date: Date to calculate momentum for
        lookback_months: Number of months to look back
        cash_rate: Annual cash return rate (for absolute momentum check)

    Returns:
        Dictionary mapping AssetClass to MomentumScore
    """
    lookback_date = calc_date - relativedelta(months=lookback_months)
    results = {}

    for asset_class, asset in assets.items():
        if asset_class == AssetClass.CASH:
            # Cash has fixed return
            results[asset_class] = MomentumScore(
                asset=asset,
                date=calc_date,
                momentum_12m=cash_rate,
                price=1.0,
                price_12m_ago=1.0 / (1 + cash_rate),
            )
            continue

        symbol = asset.yahoo_symbol or asset.symbol
        symbol_prices = prices[prices["symbol"] == symbol].copy()

        if symbol_prices.empty:
            logger.warning("no_prices_for_asset", asset_class=asset_class.value, symbol=symbol)
            continue

        symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])

        # Get current price
        current = symbol_prices[symbol_prices["date"] <= pd.Timestamp(calc_date)]
        if current.empty:
            continue
        current_price = float(current.iloc[-1]["close"])

        # Get lookback price
        past = symbol_prices[symbol_prices["date"] <= pd.Timestamp(lookback_date)]
        if past.empty:
            continue
        past_price = float(past.iloc[-1]["close"])

        if past_price == 0:
            continue

        momentum = (current_price / past_price) - 1

        results[asset_class] = MomentumScore(
            asset=asset,
            date=calc_date,
            momentum_12m=momentum,
            price=current_price,
            price_12m_ago=past_price,
        )

    return results
