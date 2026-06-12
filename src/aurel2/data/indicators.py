"""Technical indicators for trading strategies."""

import pandas as pd
import structlog

logger = structlog.get_logger()


def calculate_rsi(prices: pd.DataFrame, symbol: str, period: int = 14) -> float | None:
    """
    Calculate the Relative Strength Index (RSI) for a symbol.

    RSI = 100 - (100 / (1 + RS))
    where RS = Average Gain / Average Loss over the period

    Args:
        prices: DataFrame with columns: date, close, symbol
        symbol: The symbol to calculate RSI for
        period: Number of periods for RSI calculation (default 14)

    Returns:
        RSI value between 0 and 100, or None if insufficient data
    """
    symbol_prices = prices[prices["symbol"] == symbol].copy()

    if symbol_prices.empty:
        logger.warning("no_prices_for_symbol", symbol=symbol)
        return None

    # Ensure sorted by date
    symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
    symbol_prices = symbol_prices.sort_values("date")

    # Need at least period + 1 data points to calculate RSI
    if len(symbol_prices) < period + 1:
        logger.warning(
            "insufficient_data_for_rsi",
            symbol=symbol,
            data_points=len(symbol_prices),
            required=period + 1,
        )
        return None

    # Calculate price changes
    close_prices = symbol_prices["close"].values
    deltas = pd.Series(close_prices).diff()

    # Separate gains and losses
    gains = deltas.where(deltas > 0, 0.0)
    losses = (-deltas).where(deltas < 0, 0.0)

    # Calculate average gains and losses using exponential moving average (Wilder's smoothing)
    # First value is simple average
    avg_gain = gains.iloc[1 : period + 1].mean()
    avg_loss = losses.iloc[1 : period + 1].mean()

    # Apply Wilder's smoothing for remaining values
    for i in range(period + 1, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains.iloc[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses.iloc[i]) / period

    # Handle edge cases
    if avg_loss == 0:
        if avg_gain == 0:
            # No movement at all - undefined RSI
            logger.debug("no_price_movement", symbol=symbol)
            return None
        # All gains, no losses - RSI = 100
        return 100.0

    # Calculate RS and RSI
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    logger.debug(
        "calculated_rsi",
        symbol=symbol,
        rsi=round(rsi, 2),
        avg_gain=round(avg_gain, 4),
        avg_loss=round(avg_loss, 4),
    )

    return rsi


def calculate_drawdown(
    prices: pd.DataFrame, symbol: str, lookback_days: int = 252
) -> float | None:
    """
    Calculate the current drawdown from peak for a symbol.

    Drawdown = (Peak - Current) / Peak

    Args:
        prices: DataFrame with columns: date, close, symbol
        symbol: The symbol to calculate drawdown for
        lookback_days: Number of days to look back for peak (default 252 = ~1 year)

    Returns:
        Drawdown as decimal (0.10 = 10% drawdown), or None if insufficient data
    """
    symbol_prices = prices[prices["symbol"] == symbol].copy()

    if symbol_prices.empty:
        logger.warning("no_prices_for_symbol", symbol=symbol)
        return None

    # Ensure sorted by date
    symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
    symbol_prices = symbol_prices.sort_values("date")

    # Get data within lookback window
    if len(symbol_prices) > lookback_days:
        symbol_prices = symbol_prices.iloc[-lookback_days:]

    close_prices = symbol_prices["close"].values
    current_price = close_prices[-1]
    peak_price = close_prices.max()

    if peak_price == 0:
        logger.warning("zero_peak_price", symbol=symbol)
        return None

    drawdown = (peak_price - current_price) / peak_price

    logger.debug(
        "calculated_drawdown",
        symbol=symbol,
        drawdown=round(drawdown, 4),
        current_price=current_price,
        peak_price=peak_price,
    )

    return drawdown


def calculate_moving_average(
    prices: pd.DataFrame, symbol: str, period: int = 200
) -> float | None:
    """
    Calculate the simple moving average for a symbol.

    Args:
        prices: DataFrame with columns: date, close, symbol
        symbol: The symbol to calculate MA for
        period: Number of periods for MA calculation (default 200)

    Returns:
        Moving average value, or None if insufficient data
    """
    symbol_prices = prices[prices["symbol"] == symbol].copy()

    if symbol_prices.empty:
        logger.warning("no_prices_for_symbol", symbol=symbol)
        return None

    # Ensure sorted by date
    symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
    symbol_prices = symbol_prices.sort_values("date")

    if len(symbol_prices) < period:
        logger.warning(
            "insufficient_data_for_ma",
            symbol=symbol,
            data_points=len(symbol_prices),
            required=period,
        )
        return None

    # Calculate simple moving average of last 'period' prices
    close_prices = symbol_prices["close"].values
    ma = float(close_prices[-period:].mean())

    logger.debug(
        "calculated_moving_average",
        symbol=symbol,
        period=period,
        ma=round(ma, 2),
    )

    return ma


def get_current_price(prices: pd.DataFrame, symbol: str) -> float | None:
    """
    Get the most recent price for a symbol.

    Args:
        prices: DataFrame with columns: date, close, symbol
        symbol: The symbol to get price for

    Returns:
        Most recent closing price, or None if symbol not found
    """
    symbol_prices = prices[prices["symbol"] == symbol].copy()

    if symbol_prices.empty:
        logger.warning("no_prices_for_symbol", symbol=symbol)
        return None

    # Ensure sorted by date
    symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])
    symbol_prices = symbol_prices.sort_values("date")

    current_price = float(symbol_prices["close"].iloc[-1])

    logger.debug(
        "got_current_price",
        symbol=symbol,
        price=current_price,
    )

    return current_price
