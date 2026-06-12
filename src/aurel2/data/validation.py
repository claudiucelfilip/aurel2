"""Data validation for price data from any provider."""

from datetime import date, timedelta

import pandas as pd
import structlog

logger = structlog.get_logger()


def validate_prices(
    df: pd.DataFrame,
    symbol: str,
    min_rows: int = 200,
    max_spike_pct: float = 0.20,
    max_stale_days: int = 5,
) -> pd.DataFrame:
    """Validate and clean price data.

    Args:
        df: DataFrame with columns: date, close, symbol
        symbol: Symbol being validated (for logging)
        min_rows: Minimum required data points for lookback
        max_spike_pct: Flag single-day changes exceeding this (e.g. 0.20 = 20%)
        max_stale_days: Warn if last data point older than this many business days

    Returns:
        Cleaned DataFrame with invalid rows removed
    """
    if df.empty:
        return df

    original_len = len(df)

    # Drop NaN, zero, and negative prices
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].copy()

    dropped = original_len - len(df)
    if dropped > 0:
        logger.warning("dropped_invalid_prices", symbol=symbol, dropped=dropped)

    if df.empty:
        logger.warning("all_prices_invalid", symbol=symbol)
        return df

    # Staleness check
    df["date"] = pd.to_datetime(df["date"])
    last_date = df["date"].max()
    today = pd.Timestamp(date.today())
    bdays_since = len(pd.bdate_range(last_date, today)) - 1  # exclude start
    if bdays_since > max_stale_days:
        logger.warning(
            "stale_price_data",
            symbol=symbol,
            last_date=str(last_date.date()),
            business_days_old=bdays_since,
        )

    # Spike detection (flag but don't remove — could be legitimate)
    df_sorted = df.sort_values("date")
    pct_change = df_sorted["close"].pct_change()
    spikes = pct_change.abs() > max_spike_pct
    if spikes.any():
        spike_dates = df_sorted.loc[spikes, "date"].dt.date.tolist()
        logger.warning(
            "price_spikes_detected",
            symbol=symbol,
            spike_dates=[str(d) for d in spike_dates[:5]],
            count=int(spikes.sum()),
        )

    # Minimum data point check
    if len(df) < min_rows:
        logger.warning(
            "insufficient_data_points",
            symbol=symbol,
            rows=len(df),
            required=min_rows,
        )

    # Convert date column back to date type for consistency
    df["date"] = df["date"].dt.date

    return df
